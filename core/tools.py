"""Safe PostgreSQL sandbox access and schema initialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import json_dumps
from .database import PostgresConnection


# Set of SQLite action codes that correspond to write or schema-modifying operations.
# These are denied by the SQLite authorizer to enforce a read-only boundary.
# Values use getattr with fallback defaults for cross-version Python compatibility.
class UnsafeSQLError(ValueError):
    """Raised before or while executing a disallowed target query."""


class QueryLimitError(RuntimeError):
    """Raised when a query exceeds row, byte, or step limits."""


def _quote_postgres_identifier(identifier: str) -> str:
    if (
        not identifier
        or not identifier.replace("_", "a").isalnum()
        or not identifier[0].isalpha()
    ):
        raise ValueError("invalid PostgreSQL schema name")
    return '"' + identifier.replace('"', '""') + '"'


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


def _validate_postgres_query(sql: str) -> str:
    """Allow one SELECT/WITH query before PostgreSQL read-only enforcement."""
    query = sql.strip()
    if not query:
        raise UnsafeSQLError("SQL must be a non-empty string")
    if _contains_internal_semicolon(query):
        raise UnsafeSQLError("multiple SQL statements are not allowed")
    if "--" in query or "/*" in query or "*/" in query:
        raise UnsafeSQLError("SQL comments are not allowed")
    if any(
        token in query.lower()
        for token in ("pg_catalog.", "information_schema.", "public.")
    ):
        raise UnsafeSQLError("cross-schema queries are not allowed")
    first_word = query.rstrip(";").lstrip().split(None, 1)[0].lower()
    if first_word not in {"select", "with"}:
        raise UnsafeSQLError("only read-only SELECT statements are allowed")
    return query.rstrip(";").rstrip()


def _validate_postgres_schema_access(query: str, schema: str) -> None:
    """Reject structurally qualified tables outside the experiment schema."""
    try:
        from sqlglot import exp, parse_one

        expression = parse_one(query, read="postgres")
    except Exception as exc:
        raise UnsafeSQLError("query could not be parsed safely") from exc
    for table in expression.find_all(exp.Table):
        database = table.args.get("db")
        if database is not None and database.name != schema:
            raise UnsafeSQLError(
                "query references a table outside the experiment schema"
            )


def execute_postgres_read_only_sql(
    database_url: str,
    schema: str,
    sql: str,
    *,
    max_rows: int = 100,
    max_result_bytes: int = 1024 * 1024,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Execute a bounded read-only query against an isolated Neon schema."""
    if max_rows < 1 or max_result_bytes < 1:
        raise ValueError("query limits must be positive")
    query = _validate_postgres_query(sql)
    _validate_postgres_schema_access(query, schema)
    quoted_schema = _quote_postgres_identifier(schema)
    connection = PostgresConnection(database_url, readonly=True, timeout=timeout)
    try:
        connection.execute(f"SET search_path TO {quoted_schema}")
        cursor = connection.execute(query)
        if cursor.description is None:
            raise UnsafeSQLError("only read-only SELECT statements are allowed")
        columns = [item.name for item in cursor.description]
        rows: list[dict[str, Any]] = []
        total_bytes = 0
        for raw in cursor.fetchmany(max_rows + 1):
            if len(rows) >= max_rows:
                raise QueryLimitError(f"result exceeds {max_rows} rows")
            row = dict(raw)
            size = _json_size(row)
            if total_bytes + size > max_result_bytes:
                raise QueryLimitError(f"result exceeds {max_result_bytes} bytes")
            rows.append({column: row.get(column) for column in columns})
            total_bytes += size
        return rows
    finally:
        connection.close()


def _postgres_fixture_sql(fixtures_dir: str | Path) -> str:
    schema = (Path(fixtures_dir) / "ecommerce_schema.sql").read_text(encoding="utf-8")
    seed = (Path(fixtures_dir) / "ecommerce_seed.sql").read_text(encoding="utf-8")
    schema = schema.replace("PRAGMA foreign_keys = ON;", "")
    schema = schema.replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
    )
    schema = schema.replace(" REAL ", " DOUBLE PRECISION ")
    schema = schema.replace(" REAL,", " DOUBLE PRECISION,")
    seed = seed.replace("PRAGMA foreign_keys = ON;", "")
    return schema + "\n" + seed


def prepare_postgres_sandbox(
    database_url: str,
    fixtures_dir: str | Path,
    experiment_id: str,
) -> str:
    """Create or replace an isolated PostgreSQL schema for one experiment."""
    safe_id = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in experiment_id
    )
    schema = f"sandbox_{safe_id}"
    quoted_schema = _quote_postgres_identifier(schema)
    connection = PostgresConnection(database_url)
    try:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")
        connection.execute(f"SET search_path TO {quoted_schema}")
        existing = connection.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s LIMIT 1",
            (schema,),
        ).fetchone()
        if existing is None:
            connection.executescript(_postgres_fixture_sql(fixtures_dir))
    finally:
        connection.close()
    return schema


def register_postgres_read_only_tool(
    database_url: str,
    schema: str,
    *,
    max_rows: int = 100,
    max_result_bytes: int = 1024 * 1024,
) -> Callable[..., list[dict[str, Any]]]:
    """Create a bounded read-only SQL tool for an isolated PostgreSQL schema."""

    def tool(sql: str) -> list[dict[str, Any]]:
        return execute_postgres_read_only_sql(
            database_url,
            schema,
            sql,
            max_rows=max_rows,
            max_result_bytes=max_result_bytes,
        )

    tool.__name__ = "read_only_sql"
    return tool


__all__ = [
    "QueryLimitError",
    "UnsafeSQLError",
    "execute_postgres_read_only_sql",
    "prepare_postgres_sandbox",
    "register_postgres_read_only_tool",
]
