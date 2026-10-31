"""Database connections, schemas, and migrations for SQLite or PostgreSQL."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Schema version for the models catalog database
MODEL_SCHEMA_VERSION = 1
# Schema version for the lab experiment database
LAB_SCHEMA_VERSION = 1
# Migration scripts keyed by target version (empty since both are at version 1)
MODEL_MIGRATIONS: dict[int, str] = {}
LAB_MIGRATIONS: dict[int, str] = {}


class _PostgresRow(dict):
    """Mapping row that also preserves SQLite-style numeric indexing."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _postgres_row(cursor: Any, values: tuple[Any, ...]) -> _PostgresRow:
    return _PostgresRow(zip((column.name for column in cursor.description), values))


def _qmark_to_postgres(sql: str) -> str:
    """Convert qmark placeholders outside quoted strings to psycopg placeholders."""
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote:
            result.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    result.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
        elif character in {"'", '"', "`"}:
            quote = character
            result.append(character)
        elif character == "?":
            result.append("%s")
        else:
            result.append(character)
        index += 1
    return "".join(result)


class PostgresConnection:
    """Small psycopg compatibility wrapper used by the existing repositories."""

    is_postgres = True

    def __init__(self, url: str, *, readonly: bool = False, timeout: float = 30.0):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required for PostgreSQL; install requirements.txt"
            ) from exc

        self._connection = psycopg.connect(
            url,
            autocommit=True,
            connect_timeout=max(1, int(timeout)),
            row_factory=_postgres_row,
        )
        self._connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (str(max(1, int(timeout * 1000))),),
        )
        if readonly:
            self._connection.execute("SET default_transaction_read_only = on")

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        return self._connection.execute(_qmark_to_postgres(sql), parameters)

    def executemany(self, sql: str, parameters: Any) -> Any:
        return self._connection.executemany(_qmark_to_postgres(sql), parameters)

    def executescript(self, script: str) -> None:
        statements = [
            statement.strip() for statement in script.split(";") if statement.strip()
        ]
        for statement in statements:
            self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    @property
    def in_transaction(self) -> bool:
        return self._connection.info.transaction_status != 0


def _is_postgres_target(target: str | Path) -> bool:
    return isinstance(target, str) and target.lower().startswith(
        ("postgresql://", "postgres://")
    )


def is_postgres_target(target: str | Path) -> bool:
    """Return whether a database target is a PostgreSQL connection URL."""
    return _is_postgres_target(target)


# --- Models Catalog Schema ---
# Stores normalized Groq model metadata with child tables for modalities, pricing,
# features, and sampling parameters. The raw API response is preserved in raw_json.
MODEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    object TEXT,
    created INTEGER,
    owned_by TEXT,
    active INTEGER NOT NULL DEFAULT 0,
    context_window INTEGER,
    max_completion_tokens INTEGER,
    name TEXT,
    hugging_face_id TEXT,
    context_length INTEGER,
    max_output_length INTEGER,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_modalities (
    model_id TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('input', 'output')),
    modality TEXT NOT NULL,
    PRIMARY KEY (model_id, direction, modality),
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_pricing (
    model_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    price TEXT,
    PRIMARY KEY (model_id, metric),
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_features (
    model_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    PRIMARY KEY (model_id, feature),
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS model_sampling_parameters (
    model_id TEXT NOT NULL,
    parameter TEXT NOT NULL,
    PRIMARY KEY (model_id, parameter),
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_models_active ON models(active);
CREATE INDEX IF NOT EXISTS idx_models_owned_by ON models(owned_by);
CREATE INDEX IF NOT EXISTS idx_model_modalities_modality
    ON model_modalities(modality, direction);
"""

# --- Lab Experiment Schema ---
# Stores all experiment evidence: experiments, sessions, turns, memory events,
# detections, mitigations, metrics, and archive records.
LAB_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    hypothesis TEXT,
    model_id TEXT,
    memory_backend TEXT,
    poison_vector TEXT,
    mitigation_strategy TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    memory_namespace TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (
        phase IN ('baseline', 'poison', 'probe', 'mitigation', 'post_mitigation')
    ),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    memory_snapshot_hash TEXT,
    reset_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    phase TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_input_json TEXT,
    tool_output_json TEXT,
    model_request_json TEXT,
    model_response_json TEXT,
    latency_ms INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    estimated_cost TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE(session_id, sequence)
);

CREATE TABLE IF NOT EXISTS memory_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    operation TEXT NOT NULL CHECK (
        operation IN ('read', 'write', 'update', 'delete', 'quarantine')
    ),
    memory_key TEXT NOT NULL,
    value_json TEXT,
    source_turn_id TEXT,
    provenance TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    expires_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'quarantined', 'expired', 'deleted')
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE SET NULL,
    FOREIGN KEY (source_turn_id) REFERENCES turns(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS detections (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    detector TEXT NOT NULL,
    signal TEXT NOT NULL,
    score REAL NOT NULL,
    label TEXT NOT NULL,
    explanation TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mitigations (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    turn_id TEXT,
    strategy TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    before_memory_hash TEXT,
    after_memory_hash TEXT,
    result TEXT,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    label TEXT,
    details_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS archives (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    path TEXT NOT NULL,
    manifest_hash TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_experiment ON sessions(experiment_id);
CREATE INDEX IF NOT EXISTS idx_sessions_namespace ON sessions(memory_namespace);
CREATE INDEX IF NOT EXISTS idx_turns_session_sequence ON turns(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_turns_phase ON turns(phase);
CREATE INDEX IF NOT EXISTS idx_memory_session ON memory_events(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_namespace
    ON sessions(memory_namespace, id);
CREATE INDEX IF NOT EXISTS idx_detections_turn ON detections(turn_id);
CREATE INDEX IF NOT EXISTS idx_mitigations_experiment ON mitigations(experiment_id);
CREATE INDEX IF NOT EXISTS idx_metrics_experiment ON metrics(experiment_id);
CREATE INDEX IF NOT EXISTS idx_archives_experiment ON archives(experiment_id);
"""


def connect_sqlite(
    path: str | Path,
    *,
    readonly: bool = False,
    timeout: float = 30.0,
) -> Any:
    """Open a configured SQLite or PostgreSQL connection.

    A PostgreSQL URL is routed to the Neon-compatible adapter. The historical
    function name is retained so existing repositories remain compatible.
    """
    if _is_postgres_target(path):
        return PostgresConnection(str(path), readonly=readonly, timeout=timeout)
    # Resolve the path to an absolute path
    database_path = Path(path).expanduser().resolve()
    if readonly:
        # For read-only access, use a URI connection in read-only mode
        if not database_path.exists():
            raise FileNotFoundError(database_path)
        uri = f"file:{database_path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    else:
        # For read-write access, create parent directories if needed
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=timeout)
    # Use Row factory for dict-like access to rows by column name
    connection.row_factory = sqlite3.Row
    # Enable foreign key enforcement (required for cascade deletes to work)
    connection.execute("PRAGMA foreign_keys = ON")
    # Set busy timeout to wait (up to timeout seconds) instead of immediately erroring on locks
    connection.execute(f"PRAGMA busy_timeout = {max(1, int(timeout * 1000))}")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Yield a connection inside a commit/rollback transaction."""
    try:
        # Begin a transaction
        connection.execute("BEGIN")
        yield connection
        # If no exception occurred, commit the transaction
        connection.commit()
    except Exception:
        # On any exception, roll back the transaction and re-raise
        connection.rollback()
        raise


def _apply_schema(
    connection: sqlite3.Connection,
    schema: str,
    name: str,
    version: int,
    migrations: dict[int, str],
) -> None:
    """Apply schema creation and migrations, tracking the applied version."""
    if version < 1:
        raise ValueError("schema version must be positive")
    # Ensure the schema_migrations tracking table exists
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """)
    # Check the currently applied version for this schema
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE name = ?", (name,)
    ).fetchone()
    current = int(row[0]) if row else 0
    # Prevent downgrading to an older schema version
    if current > version:
        raise ValueError(
            f"cannot downgrade {name} schema from version {current} to {version}"
        )
    # If already at the target version, nothing to do
    if current == version:
        return
    # If starting fresh (version 0), apply the full schema
    if current == 0:
        connection.executescript(schema)
        applied_at = (
            "CURRENT_TIMESTAMP"
            if getattr(connection, "is_postgres", False)
            else "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        )
        connection.execute(
            f"""
            INSERT INTO schema_migrations(name, version, applied_at)
            VALUES (?, ?, {applied_at})
            """,
            (name, version),
        )
        if not getattr(connection, "is_postgres", False):
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
        return
    # Otherwise, apply migrations incrementally from current+1 to target version
    for migration_version in range(current + 1, version + 1):
        migration = migrations.get(migration_version)
        if not migration:
            raise ValueError(
                f"no migration registered for {name} version {migration_version}"
            )
        connection.executescript(migration)
        applied_at = (
            "CURRENT_TIMESTAMP"
            if getattr(connection, "is_postgres", False)
            else "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        )
        connection.execute(
            f"""
            INSERT INTO schema_migrations(name, version, applied_at)
            VALUES (?, ?, {applied_at})
            ON CONFLICT(name) DO UPDATE SET
                version = excluded.version,
                applied_at = excluded.applied_at
            """,
            (name, migration_version),
        )
        if not getattr(connection, "is_postgres", False):
            connection.execute(f"PRAGMA user_version = {migration_version}")
        connection.commit()


def initialize_models_db(
    path: str | Path,
    *,
    version: int = MODEL_SCHEMA_VERSION,
) -> Any:
    """Initialize the models catalog database and return an open connection."""
    # Resolve the path and ensure parent directory exists
    database_path = (
        path if _is_postgres_target(path) else Path(path).expanduser().resolve()
    )
    if not _is_postgres_target(path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
    # Open/create the database file
    connection = connect_sqlite(database_path)
    # Ensure schema_migrations table exists in this database
    connection.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """)
    # Apply the models schema (creates tables if they don't exist)
    _apply_schema(connection, MODEL_SCHEMA, "models", version, MODEL_MIGRATIONS)
    return connection


def initialize_lab_db(
    path: str | Path,
    *,
    version: int = LAB_SCHEMA_VERSION,
) -> Any:
    """Initialize the lab experiment database and return an open connection."""
    # Resolve the path and ensure parent directory exists
    database_path = (
        path if _is_postgres_target(path) else Path(path).expanduser().resolve()
    )
    if not _is_postgres_target(path):
        database_path.parent.mkdir(parents=True, exist_ok=True)
    # Open/create the database file
    connection = connect_sqlite(database_path)
    # Apply the lab schema (creates all evidence tables)
    _apply_schema(connection, LAB_SCHEMA, "lab", version, LAB_MIGRATIONS)
    return connection


def initialize_databases(models_path: str | Path, lab_path: str | Path):
    """Initialize both application databases and return their connections."""
    # Initialize models DB and lab DB, returning both open connections
    return initialize_models_db(models_path), initialize_lab_db(lab_path)


def schema_sql() -> str:
    """Return the complete lab schema for archives and diagnostics."""
    return LAB_SCHEMA


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a SQLite Row to a dict, or return None if input is None."""
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    """Convert a list of SQLite Rows to a list of dicts."""
    return [dict(row) for row in rows]


__all__ = [
    "LAB_SCHEMA",
    "LAB_SCHEMA_VERSION",
    "MODEL_SCHEMA",
    "MODEL_SCHEMA_VERSION",
    "connect_sqlite",
    "is_postgres_target",
    "initialize_databases",
    "initialize_lab_db",
    "initialize_models_db",
    "row_to_dict",
    "rows_to_dicts",
    "schema_sql",
    "transaction",
]
