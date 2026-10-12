"""Safe SQLite target access and schema inspection."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from .config import json_dumps

# Set of SQLite action codes that correspond to write or schema-modifying operations.
# These are denied by the SQLite authorizer to enforce a read-only boundary.
# Values use getattr with fallback defaults for cross-version Python compatibility.
WRITE_ACTIONS = {
    getattr(sqlite3, "SQLITE_INSERT", 18),  # INSERT statements
    getattr(sqlite3, "SQLITE_UPDATE", 23),  # UPDATE statements
    getattr(sqlite3, "SQLITE_DELETE", 9),   # DELETE statements
    getattr(sqlite3, "SQLITE_DROP", 10),    # DROP TABLE/VIEW/INDEX
    getattr(sqlite3, "SQLITE_ALTER", 2),    # ALTER TABLE
    getattr(sqlite3, "SQLITE_CREATE", 8),   # CREATE TABLE/VIEW/INDEX
    getattr(sqlite3, "SQLITE_REINDEX", 29),  # REINDEX
    getattr(sqlite3, "SQLITE_TRANSACTION", 22),  # BEGIN/COMMIT/ROLLBACK
    getattr(sqlite3, "SQLITE_ATTACH", 24),  # ATTACH DATABASE
    getattr(sqlite3, "SQLITE_DETACH", 7),   # DETACH DATABASE
    getattr(sqlite3, "SQLITE_PRAGMA", 19),  # PRAGMA statements
}

# Set of SQLite action codes that are permitted (SELECT, READ, FUNCTION calls).
# Only these action codes pass through the authorizer, all others are denied.
ALLOWED_ACTIONS = {
    getattr(sqlite3, "SQLITE_SELECT", 21),  # SELECT statements
    getattr(sqlite3, "SQLITE_READ", 20),    # Reading a table/column
    getattr(sqlite3, "SQLITE_FUNCTION", 31),  # Calling a user-defined function
}


class UnsafeSQLError(ValueError):
    """Raised before or while executing a disallowed target query."""


class QueryLimitError(RuntimeError):
    """Raised when a query exceeds row, byte, or step limits."""



def quote_identifier(identifier: str) -> str:
    """Safely quote a SQLite identifier to prevent SQL injection in dynamic queries."""
    # Validate that the identifier is a string and contains no null bytes
    if not isinstance(identifier, str) or "\x00" in identifier:
        raise ValueError("invalid SQLite identifier")
    # Wrap in double quotes and escape any embedded double quotes by doubling them
    return '"' + identifier.replace('"', '""') + '"'


def describe_schema(db_path: str | Path) -> str:
    """Return a stable, read-only description of user tables and columns."""
    # Open the database in read-only mode using a URI connection
    with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as connection:
        # Query sqlite_master for all user-defined tables (excluding SQLite internal tables)
        tables = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        # Build a list of schema description sections, one per table
        sections = []
        for (table,) in tables:
            # Get column metadata for each table using the quoted identifier
            columns = connection.execute(
                f"PRAGMA table_info({quote_identifier(table)})"
            ).fetchall()
            # Start the section with the table name
            lines = [f"TABLE: {table}"]
            # Format each column: name, type, and constraints (NOT NULL, PRIMARY KEY)
            lines.extend(
                f"- {row['name']} ({row['type']})"
                + (" NOT NULL" if row["notnull"] else "")
                + (" PRIMARY KEY" if row["pk"] else "")
                for row in columns
            )
            # Join column lines into a single section string
            sections.append("\n".join(lines))
        # Join all table sections with blank lines between them
        return "\n\n".join(sections)


def _authorizer(action: int, arg1: Any = None, *_: Any) -> int:
    """SQLite authorizer callback that denies all write/schema actions."""
    # Special handling for function calls: block dangerous built-in functions
    if action == getattr(sqlite3, "SQLITE_FUNCTION", 31):
        function_name = str(arg1 or "").lower()
        # Deny load_extension and sqlite_exec which could execute arbitrary code
        if function_name in {"load_extension", "sqlite_exec"}:
            return sqlite3.SQLITE_DENY
    # Allow SELECT, READ, and function calls (non-blocked)
    if action in ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    # Deny everything else (INSERT, UPDATE, DELETE, PRAGMA, ATTACH, etc.)
    return sqlite3.SQLITE_DENY


def _json_size(value: Any) -> int:
    """Calculate the UTF-8 byte size of a value serialized as JSON."""
    return len(json_dumps(value).encode("utf-8"))


def _contains_internal_semicolon(sql: str) -> bool:
    """Check whether a SQL string contains a semicolon outside of string literals."""
    stripped = sql.strip()
    # Remove a trailing semicolon (single-statement queries often end with one)
    if stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    # Track which quote character we're inside (None = not in a string)
    quote: str | None = None
    index = 0
    # Walk through each character to find semicolons outside string literals
    while index < len(stripped):
        character = stripped[index]
        if quote == "'":
            # Inside single-quoted string: look for closing quote or escaped ''
            if character == "'":
                if index + 1 < len(stripped) and stripped[index + 1] == "'":
                    index += 2  # Skip escaped quote ''
                    continue
                quote = None  # End of single-quoted string
        elif quote == '"':
            # Inside double-quoted string (identifier): look for closing quote or escaped ""
            if character == '"':
                if index + 1 < len(stripped) and stripped[index + 1] == '"':
                    index += 2  # Skip escaped quote ""
                    continue
                quote = None  # End of double-quoted string
        elif character in {"'", '"'}:
            quote = character  # Enter a quoted string
        elif character == ";":
            return True  # Found a semicolon outside any string → multiple statements
        index += 1
    return False


def execute_read_only_sql(
    db_path: str | Path,
    sql: str,
    *,
    max_rows: int = 100,
    max_result_bytes: int = 1024 * 1024,
    max_steps: int = 100000,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Execute one read-only statement under SQLite authorization and limits."""
    # Validate input: SQL must be a non-empty string
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSQLError("SQL must be a non-empty string")
    # Reject queries containing multiple statements (only single statement allowed)
    if _contains_internal_semicolon(sql):
        raise UnsafeSQLError("multiple SQL statements are not allowed")
    # Validate that all limit parameters are positive
    if max_rows < 1 or max_result_bytes < 1 or max_steps < 1:
        raise ValueError("query limits must be positive")
    # Resolve the database path and verify the file exists
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    # Open the database in read-only URI mode (prevents any file changes)
    connection = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, timeout=timeout, isolation_level=None
    )
    # Use Row factory for dict-like row access
    connection.row_factory = sqlite3.Row
    # Install the authorizer to enforce the read-only boundary
    connection.set_authorizer(_authorizer)
    # Counter for query steps (VM operations)
    steps = 0

    def progress() -> int | None:
        """Progress handler that limits total VM steps to prevent resource exhaustion."""
        nonlocal steps
        steps += 1
        # If step count exceeds limit, return 1 to abort the query
        if steps > max_steps:
            return 1
        return None

    # Register the progress handler — called every 100 VM operations
    connection.set_progress_handler(progress, 100)
    try:
        # Execute the SQL query and get a cursor
        cursor = connection.execute(sql)
        # Only allow statements that return a result set (SELECT-like queries)
        if cursor.description is None:
            raise UnsafeSQLError("only read-only SELECT statements are allowed")
        # Extract column names from the cursor description
        columns = [item[0] for item in cursor.description]
        # Accumulate result rows as dicts
        rows: list[dict[str, Any]] = []
        # Track total byte size of accumulated results
        total_bytes = 0
        while True:
            # Fetch up to 25 rows at a time, but stop once we exceed max_rows
            raw_rows = cursor.fetchmany(min(25, max_rows + 1 - len(rows)))
            if not raw_rows:
                break
            for raw in raw_rows:
                # Convert Row to dict for JSON serialization
                row = dict(raw)
                # Calculate byte size of this row when JSON-serialized
                size = _json_size(row)
                # Enforce row count limit
                if len(rows) >= max_rows:
                    raise QueryLimitError(f"result exceeds {max_rows} rows")
                # Enforce total result byte size limit
                if total_bytes + size > max_result_bytes:
                    raise QueryLimitError(
                        f"result exceeds {max_result_bytes} bytes"
                    )
                rows.append(row)
                total_bytes += size
        return rows
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
        # Check if the error was due to the authorizer blocking an operation
        message = str(exc).lower()
        if "not authorized" in message or "query aborted" in message:
            raise UnsafeSQLError("query was blocked by the read-only boundary") from exc
        raise
    finally:
        # Always close the connection, even on error
        connection.close()


def execute_sql(
    db_path: str | Path,
    sql: str,
    **limits: Any,
) -> list[dict[str, Any]]:
    """Compatibility alias for the safe read-only executor."""
    # Delegate to execute_read_only_sql, passing through any limit overrides
    return execute_read_only_sql(db_path, sql, **limits)


def copy_sandbox(
    fixture_path: str | Path,
    sandbox_path: str | Path,
    *,
    overwrite: bool = True,
) -> Path:
    """Copy a known-good fixture into an isolated experiment database."""
    # Resolve source and target paths to absolute form
    source = Path(fixture_path).expanduser().resolve()
    target = Path(sandbox_path).expanduser().resolve()
    # Verify the source fixture database exists
    if not source.exists():
        raise FileNotFoundError(source)
    # Ensure the target directory exists
    target.parent.mkdir(parents=True, exist_ok=True)
    # If target already exists and overwrite is False, return it as-is
    if target.exists() and not overwrite:
        return target
    # Ensure parent directory exists again (redundant but safe)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use a temporary file for the copy to avoid partial writes if copy fails
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        # Copy the fixture to the temporary location (preserves metadata)
        shutil.copy2(source, temporary)
        # Atomically move the temp file to the final target path
        os.replace(temporary, target)
    finally:
        # Clean up temp file if anything went wrong before os.replace
        if temporary.exists():
            temporary.unlink()
    # Open the copied database and enforce read-only + foreign key constraints
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        connection.commit()
    return target


def prepare_sandbox(
    fixture_path: str | Path,
    sandbox_dir: str | Path,
    experiment_id: str,
) -> Path:
    """Copy a fixture database into a per-experiment sandbox location."""
    # Sanitize the experiment ID into a filesystem-safe filename component
    safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in experiment_id)
    # Copy the fixture to sandbox_dir/<safe_id>.db and return the path
    return copy_sandbox(fixture_path, Path(sandbox_dir) / f"{safe_id}.db")


def register_read_only_tool(
    db_path: str | Path,
    *,
    max_rows: int = 100,
    max_result_bytes: int = 1024 * 1024,
    max_steps: int = 100000,
) -> Callable[..., list[dict[str, Any]]]:
    """Create a callable SQL tool that executes read-only queries against a sandbox."""
    def tool(sql: str) -> list[dict[str, Any]]:
        """Execute a read-only SQL query against the sandbox with configured limits."""
        return execute_read_only_sql(
            db_path,
            sql,
            max_rows=max_rows,
            max_result_bytes=max_result_bytes,
            max_steps=max_steps,
        )

    # Set a friendly name on the inner function for identification/debugging
    tool.__name__ = "read_only_sql"
    return tool


__all__ = [
    "QueryLimitError",
    "UnsafeSQLError",
    "copy_sandbox",
    "describe_schema",
    "execute_read_only_sql",
    "execute_sql",
    "prepare_sandbox",
    "quote_identifier",
    "register_read_only_tool",
]
