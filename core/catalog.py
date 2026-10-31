"""Groq model catalog persistence and synchronization.

This module manages an SQLite-backed model catalog that mirrors the Groq
model API endpoint. Catalog entries are written through a single upsert path
whether they originate from a remote sync or a local JSON snapshot, ensuring
consistent normalization of all model metadata.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .config import json_dumps, utc_now          # json_dumps serializes safely, utc_now gives a Z-normalized timestamp
from .database import connect_sqlite, initialize_models_db, is_postgres_target, transaction

# Default endpoint for the Groq OpenAI-compatible model listing API
CATALOG_URL = "https://api.groq.com/openai/v1/models"


def _as_list(value: Any) -> list[str]:
    """Coerce a singular or plural value into a list of strings for storage."""
    if value is None:        # null input yields an empty list
        return []
    if isinstance(value, (list, tuple, set)):      # already iterable: stringify each item
        return [str(item) for item in value]
    if isinstance(value, str):                     # single string stays as a one-element list
        return [value]
    return [str(value)]                            # fallback: stringify any other type


def _optional_int(value: Any) -> int | None:
    """Convert a value to int, returning None for empty/None or unconvertible values."""
    if value in (None, ""):       # explicitly empty values are treated as absent
        return None
    try:
        return int(value)         # attempt integer coercion for numeric strings or numbers
    except (TypeError, ValueError):
        return None               # non-numeric values cannot be represented as ints


def _optional_bool(value: Any) -> int:
    """Normalize truthy/falsy values into a SQLite-compatible integer (0 or 1).

    String forms accepted: "1"/"true"/"yes"/"on" (case-insensitive).
    """
    if isinstance(value, str):    # interpret string truthiness with a known set of affirmative tokens
        return 1 if value.strip().lower() in {"1", "true", "yes", "on"} else 0
    return 1 if bool(value) else 0           # fallback: coerce the value to a boolean and then to int


def normalize_model(model: dict[str, Any], fetched_at: str | None = None) -> dict[str, Any]:
    """Normalize a Groq model object, preserving both structured fields and the original raw JSON.

    This ensures every field has a canonical type regardless of minor API
    shape differences (e.g., "context_window" vs "context_length").
    """
    if not isinstance(model, dict):          # guard: only dict-like model objects are accepted
        raise TypeError("model must be an object")
    model_id = model.get("id")
    if not isinstance(model_id, str) or not model_id.strip():  # guard: id is the primary key
        raise ValueError("model object is missing a non-empty id")
    fetched = fetched_at or utc_now()                           # default to current time if not provided
    raw = json_dumps(model)                                     # keep an auditable snapshot of the original payload
    # Pricing may arrive as a string or non-dict; normalize to a dict or default empty
    pricing = model.get("pricing")
    if pricing is None:
        pricing = {}
    if not isinstance(pricing, dict):
        pricing = {"value": pricing}
    return {
        "id": model_id.strip(),                                # trimmed identifier used as the primary key
        "object": model.get("object"),                         # API object type (e.g., "model")
        "created": _optional_int(model.get("created")),        # Unix creation timestamp, if present
        "owned_by": model.get("owned_by"),                    # organization or vendor that owns the model
        "active": _optional_bool(model.get("active", False)), # whether the model is currently usable
        # Prefer context_window but fall back to context_length for alternate API shapes
        "context_window": _optional_int(model.get("context_window")),
        "max_completion_tokens": _optional_int(model.get("max_completion_tokens")),
        "name": model.get("name"),                           # human-readable model name
        "hugging_face_id": model.get("hugging_face_id"),     # optional HF model identifier
        # Secondary context-length and max-output fields for compatibility with other API schemas
        "context_length": _optional_int(model.get("context_length")),
        "max_output_length": _optional_int(model.get("max_output_length")),
        # Modalities are stored as lists to support multivalue comparison
        "input_modalities": _as_list(model.get("input_modalities")),
        "output_modalities": _as_list(model.get("output_modalities")),
        # Pricing metrics are stringified values for storage; None means "unavailable"
        "pricing": {str(key): None if value is None else str(value) for key, value in pricing.items()},
        # Features accept multiple possible key names depending on the API source
        "features": _as_list(
            model.get("supported_features")
            or model.get("features")
            or model.get("capabilities")
        ),
        # Sampling parameters also accept multiple key names
        "sampling_parameters": _as_list(
            model.get("supported_sampling_parameters")
            or model.get("sampling_parameters")
        ),
        "raw_json": raw,           # serialized copy of the original model payload
        "fetched_at": fetched,      # timestamp describing when this record was last synced
        "raw_object": model,       # in-memory copy of the parsed model object (not persisted)
    }


def upsert_model(
    connection: sqlite3.Connection,
    model: dict[str, Any],
    fetched_at: str | None = None,
) -> str:
    """Atomically upsert one normalized model and replace all child metadata tables.

    The main `models` row is inserted with an ON CONFLICT upsert, then all
    related tables (modalities, pricing, features, sampling parameters) are
    cleared and repopulated to reflect the latest snapshot.
    """
    normalized = normalize_model(model, fetched_at)
    connection.execute(
        """
        INSERT INTO models (
            id, object, created, owned_by, active, context_window,
            max_completion_tokens, name, hugging_face_id, context_length,
            max_output_length, raw_json, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            object = excluded.object,
            created = excluded.created,
            owned_by = excluded.owned_by,
            active = excluded.active,
            context_window = excluded.context_window,
            max_completion_tokens = excluded.max_completion_tokens,
            name = excluded.name,
            hugging_face_id = excluded.hugging_face_id,
            context_length = excluded.context_length,
            max_output_length = excluded.max_output_length,
            raw_json = excluded.raw_json,
            fetched_at = excluded.fetched_at
        """,
        (
            normalized["id"],
            normalized["object"],
            normalized["created"],
            normalized["owned_by"],
            normalized["active"],
            normalized["context_window"],
            normalized["max_completion_tokens"],
            normalized["name"],
            normalized["hugging_face_id"],
            normalized["context_length"],
            normalized["max_output_length"],
            normalized["raw_json"],
            normalized["fetched_at"],
        ),
    )
    model_id = normalized["id"]
    # Clear and repopulate child tables to avoid orphaned rows from prior snapshots
    connection.execute("DELETE FROM model_modalities WHERE model_id = ?", (model_id,))
    connection.execute("DELETE FROM model_pricing WHERE model_id = ?", (model_id,))
    connection.execute("DELETE FROM model_features WHERE model_id = ?", (model_id,))
    connection.execute(
        "DELETE FROM model_sampling_parameters WHERE model_id = ?", (model_id,)
    )
    # Re-insert input modalities with direction 'input'
    connection.executemany(
        """
        INSERT INTO model_modalities(model_id, direction, modality)
        VALUES (?, 'input', ?)
        """,
        [(model_id, modality) for modality in normalized["input_modalities"]],
    )
    # Re-insert output modalities with direction 'output'
    connection.executemany(
        """
        INSERT INTO model_modalities(model_id, direction, modality)
        VALUES (?, 'output', ?)
        """,
        [(model_id, modality) for modality in normalized["output_modalities"]],
    )
    # Re-insert pricing entries keyed by metric name and price
    connection.executemany(
        "INSERT INTO model_pricing(model_id, metric, price) VALUES (?, ?, ?)",
        [(model_id, metric, price) for metric, price in normalized["pricing"].items()],
    )
    # Re-insert supported feature flags
    connection.executemany(
        "INSERT INTO model_features(model_id, feature) VALUES (?, ?)",
        [(model_id, feature) for feature in normalized["features"]],
    )
    # Re-insert advertised sampling parameter names
    connection.executemany(
        "INSERT INTO model_sampling_parameters(model_id, parameter) VALUES (?, ?)",
        [(model_id, parameter) for parameter in normalized["sampling_parameters"]],
    )
    return model_id


def import_model_snapshot(
    snapshot_path: str | Path,
    db_path: str | Path | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Import the bundled JSON snapshot through the same upsert path as sync.

    Accepts either a file path or an existing connection. If no connection
    is supplied, one is opened on a database derived from db_path or the
    snapshot's parent directory.
    """
    path = Path(snapshot_path).expanduser().resolve()  # resolve to an absolute Path
    owns_connection = connection is None                # track whether we need to close the connection later
    if connection is None:
        # Default database location is sibling "models.sqlite" to the snapshot
        database = (
            db_path
            if db_path and is_postgres_target(db_path)
            else Path(db_path) if db_path else path.parent / "models.sqlite"
        )
        connection = initialize_models_db(database)     # ensure the schema exists
    try:
        with path.open("r", encoding="utf-8") as handle:  # open the snapshot JSON for reading
            payload = json.load(handle)                  # parse the JSON payload
        # Validate that the snapshot has the expected "data" array structure
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("model snapshot must contain a data array")
        warnings: list[str] = []                         # collect per-model validation errors
        imported = 0                                     # count of successfully upserted models
        with transaction(connection):                    # wrap all upserts in a single atomic transaction
            for index, model in enumerate(payload["data"]):
                try:
                    upsert_model(connection, model)     # normalize and persist each model
                    imported += 1
                except (TypeError, ValueError) as exc:
                    warnings.append(f"model[{index}]: {exc}")  # record the error and continue
        return {"imported": imported, "warnings": warnings, "source": str(path)}
    finally:
        if owns_connection:                             # only close if we opened the connection ourselves
            connection.close()


def sync_models(
    api_key: str,
    db_path: str | Path | None = None,
    *,
    connection: sqlite3.Connection | None = None,
    timeout: float = 30.0,
    url: str | None = None,
) -> dict[str, Any]:
    """Fetch and atomically replace catalog metadata from the Groq API.

    Performs a read-only HTTP GET to the model listing endpoint, then upserts
    every returned model within a single transaction so the catalog always
    reflects a consistent point-in-time snapshot.
    """
    if not api_key or not api_key.strip():               # guard: API key is required for auth
        raise ValueError("API key is required for catalog synchronization")
    # Allow caller override, then env override, then default constant
    endpoint = url or os.getenv("GROQ_BASE_URL") or CATALOG_URL
    try:
        import requests                                   # lazily import to keep the module importable without requests
    except ImportError as exc:
        raise RuntimeError("requests is required for catalog synchronization") from exc
    owns_connection = connection is None                  # track ownership for cleanup
    if connection is None:
        database = (
            db_path
            if db_path and is_postgres_target(db_path)
            else Path(db_path).expanduser().resolve()    # use caller-provided path if given
            if db_path
            else Path(__file__).resolve().parents[1] / "data" / "models.sqlite"  # default to repo data/ dir
        )
        connection = initialize_models_db(database)      # ensure schema exists
    try:
        response = requests.get(                        # issue authenticated GET to the catalog endpoint
            endpoint,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            timeout=timeout,
        )
        response.raise_for_status()                     # raise on 4xx/5xx responses
        payload = response.json()                       # parse JSON response body
        # Validate the response shape matches the expected Groq model listing
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Groq response must contain a data array")
        warnings: list[str] = []                        # collect errors for individual models
        imported = 0                                    # count of successfully upserted models
        fetched_at = utc_now()                          # mark all models in this sync with the same timestamp
        with transaction(connection):                   # atomic upsert of all models from this response
            for index, model in enumerate(payload["data"]):
                try:
                    upsert_model(connection, model, fetched_at=fetched_at)
                    imported += 1
                except (TypeError, ValueError) as exc:
                    warnings.append(f"model[{index}]: {exc}")
        return {"imported": imported, "warnings": warnings, "source": endpoint}
    finally:
        if owns_connection:                             # close the connection if we opened it
            connection.close()


def _model_row(connection: sqlite3.Connection, model_id: str) -> sqlite3.Row | None:
    """Fetch a single model's core row by ID, or None if not found."""
    return connection.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()


def _enrich_model(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """Expand a core model row into a fully populated dict with child metadata.

    Joins child tables (modalities, pricing, features, sampling parameters)
    to produce a complete model representation for external consumers.
    """
    result = dict(row)                                       # copy base columns into a plain dict
    # Normalize the SQLite 0/1 integer into a proper boolean for consumers
    result["active"] = bool(result.get("active"))
    # Parse the raw JSON snapshot back into a dict for programmatic access
    result["raw_json"] = json.loads(result["raw_json"]) if result.get("raw_json") else None
    # Look up input modalities filtered by direction='input', ordered alphabetically
    result["input_modalities"] = [
        item["modality"]
        for item in connection.execute(
            "SELECT modality FROM model_modalities WHERE model_id = ? AND direction = 'input' ORDER BY modality",
            (result["id"],),
        ).fetchall()
    ]
    # Look up output modalities filtered by direction='output'
    result["output_modalities"] = [
        item["modality"]
        for item in connection.execute(
            "SELECT modality FROM model_modalities WHERE model_id = ? AND direction = 'output' ORDER BY modality",
            (result["id"],),
        ).fetchall()
    ]
    # Aggregate pricing entries into a metric-to-price mapping
    result["pricing"] = {
        item["metric"]: item["price"]
        for item in connection.execute(
            "SELECT metric, price FROM model_pricing WHERE model_id = ? ORDER BY metric",
            (result["id"],),
        ).fetchall()
    }
    # Aggregate feature flags into a flat list, ordered alphabetically
    result["features"] = [
        item["feature"]
        for item in connection.execute(
            "SELECT feature FROM model_features WHERE model_id = ? ORDER BY feature",
            (result["id"],),
        ).fetchall()
    ]
    # Aggregate sampling parameter names into a flat list
    result["sampling_parameters"] = [
        item["parameter"]
        for item in connection.execute(
            "SELECT parameter FROM model_sampling_parameters WHERE model_id = ? ORDER BY parameter",
            (result["id"],),
        ).fetchall()
    ]
    return result


def get_model(db_path: str | Path, model_id: str) -> dict[str, Any] | None:
    """Open the database read-only and return a single enriched model, or None."""
    connection = connect_sqlite(db_path, readonly=True)   # open a connection in read-only mode
    try:
        row = _model_row(connection, model_id)           # fetch the core row
        return _enrich_model(connection, row) if row else None
    finally:
        connection.close()                                # always close the connection


def list_models(
    db_path: str | Path,
    *,
    active_only: bool = True,
    include_all: bool = False,
    modality: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """List catalog models with normalized child metadata, applying optional filters.

    Filters:
    - active_only: include only active models (ignored if include_all is True)
    - modality: require the model to have the given modality in either direction
    - search: substring match against id, name, or owned_by (case-insensitive)
    """
    clauses: list[str] = []               # collect WHERE clause fragments dynamically
    values: list[Any] = []                # collect corresponding parameter bindings
    if active_only and not include_all:   # only filter on active when not explicitly including all
        clauses.append("m.active = 1")
    if modality:
        # Use EXISTS subquery to check that the model declares the given modality
        clauses.append(
            "EXISTS (SELECT 1 FROM model_modalities mm WHERE mm.model_id = m.id AND mm.modality = ?)"
        )
        values.append(modality)
    if search:
        # Case-insensitive LIKE match across three text columns
        clauses.append("(LOWER(m.id) LIKE ? OR LOWER(COALESCE(m.name, '')) LIKE ? OR LOWER(COALESCE(m.owned_by, '')) LIKE ?)")
        term = f"%{search.lower()}%"   # wrap the search term in wildcards
        values.extend((term, term, term))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""  # build the WHERE clause
    connection = connect_sqlite(db_path, readonly=True)           # open read-only connection
    try:
        rows = connection.execute(
            f"SELECT m.* FROM models m{where} ORDER BY m.active DESC, COALESCE(m.name, m.id), m.id",
            values,
        ).fetchall()
        return [_enrich_model(connection, row) for row in rows]   # enrich each row before returning
    finally:
        connection.close()


def model_ids(
    db_path: str | Path,
    *,
    active_only: bool = True,
    include_all: bool = False,
    text_only: bool = True,
) -> list[str]:
    """Return a list of model IDs satisfying optional filtering criteria.

    text_only further filters to models that support the "text" modality in both
    input and output, which is the default for chat-style usage.
    """
    models = list_models(
        db_path,
        active_only=active_only,
        include_all=include_all,
    )
    if text_only:
        # Filter to models that have "text" in both input and output modalities
        models = [
            model
            for model in models
            if "text" in model.get("input_modalities", [])
            and "text" in model.get("output_modalities", [])
        ]
    return [model["id"] for model in models]


def catalog_stats(db_path: str | Path) -> dict[str, int]:
    """Return aggregate counts for models, active models, features, and parameters."""
    connection = connect_sqlite(db_path, readonly=True)
    try:
        return {
            # Total number of models in the catalog
            "total": connection.execute("SELECT COUNT(*) FROM models").fetchone()[0],
            # Total number of active models
            "active": connection.execute("SELECT COUNT(*) FROM models WHERE active = 1").fetchone()[0],
            # Total number of feature entries across all models
            "features": connection.execute("SELECT COUNT(*) FROM model_features").fetchone()[0],
            # Total number of sampling parameter entries across all models
            "parameters": connection.execute("SELECT COUNT(*) FROM model_sampling_parameters").fetchone()[0],
        }
    finally:
        connection.close()



__all__ = [
    "CATALOG_URL",                  # default Groq model listing endpoint
    "catalog_stats",                # aggregate catalog statistics
    "get_model",                    # fetch one enriched model
    "import_model_snapshot",        # import from local JSON file
    "list_models",                  # list with filtering
    "model_ids",                    # list just the IDs
    "normalize_model",              # normalize a raw model dict
    "sync_models",                  # fetch and upsert from API
    "upsert_model",                 # insert or update one model
]
