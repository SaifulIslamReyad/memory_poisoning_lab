"""Database-backed memory operations for isolated experiment namespaces."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

# Import utility functions for JSON serialization, ID generation, hashing, redaction, and timestamps
from .config import canonical_json, json_dumps, new_id, redact, utc_now
from .database import transaction


def _commit(connection: Any) -> None:
    """Commit the current transaction on the connection."""
    connection.commit()


def _json_value(value: Any) -> str:
    """Serialize a value to JSON string using canonical (sorted, compact) format."""
    return json_dumps(value)


def _parse_json(value: str | None, default: Any = None) -> Any:
    """Parse a JSON string, returning a default value on failure or empty input."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _iso_expiry(value: str | datetime | None) -> str | None:
    """Normalize an expiration value to an ISO 8601 UTC string, or None."""
    if value is None:
        return None
    # If a datetime object is given, ensure it has timezone info and format as UTC
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    # Otherwise, just convert to string
    return str(value)


# Set of provenance categories considered "trusted" (safe to read in trusted policy mode)
_TRUSTED_PROVENANCE = {
    "explicit",  # Explicitly written by experiment operator
    "trusted",  # Trusted system memory
    "system",  # System-generated memory
    "tool",  # Results from approved tool calls
    "baseline",  # Baseline phase memory
    "retrieved_trusted",  # Retrieved and trusted document
    "assistant_summary",  # Conversation summary from assistant
}
# Set of provenance categories considered "untrusted" (may contain poison/injection)
_UNTRUSTED_PROVENANCE = {
    "untrusted",  # Untrusted user source
    "untrusted_source",  # Untrusted source
    "poison",  # Poisoned memory
    "poisoned",  # Poisoned memory (alternate form)
    "canary",  # Canary marker
    "tool_untrusted",  # Tool result from untrusted source
    "user_untrusted",  # User-supplied untrusted content
}
# Set of memory operations that represent "fact" writes (vs. reads/deletes)
_FACT_OPERATIONS = {"write", "update"}


def _provenance_category(value: str) -> str:
    """Normalize a provenance string to its base category (strip prefixes/suffixes)."""
    # Lowercase and replace hyphens with underscores for consistent matching
    normalized = str(value or "").strip().lower().replace("-", "_")
    # Extract the base category (before any ':' or ',' separator)
    return normalized.split(":", 1)[0].split(",", 1)[0]


def _memory_policy_match(provenance: str, policy: str) -> bool:
    """Check whether a memory provenance matches the given read policy."""
    category = _provenance_category(provenance)
    # "trusted" policy allows only trusted provenance categories
    if policy == "trusted":
        return category in _TRUSTED_PROVENANCE or category.startswith("trusted_")
    # "untrusted" policy allows only untrusted provenance categories
    if policy == "untrusted":
        return category in _UNTRUSTED_PROVENANCE or category.startswith("untrusted_")
    # Default policy allows everything
    return True


def _is_expired(expires_at: str | None) -> bool:
    """Check whether a memory entry has expired based on its expires_at timestamp."""
    if not expires_at:
        return False
    try:
        # Parse the timestamp; handle 'Z' suffix by replacing with +00:00 for fromisoformat
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        # Entry is expired if its expiry time is in the past
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        # If the timestamp can't be parsed, treat it as expired (fail-safe)
        return True


def memory_snapshot_hash(
    connection: Any,
    namespace: str,
    *,
    include_inactive: bool = False,
) -> str:
    """Compute a SHA-256 hash of the canonical active memory state for a namespace."""
    # Build query clauses based on whether inactive entries should be included
    clauses = [
        "s.memory_namespace = ?",
        "m.status = 'active'",
        "m.operation IN ('write', 'update')",
    ]
    values: list[Any] = [namespace]
    if include_inactive:
        # When including inactive, only filter by namespace and operation (ignore status)
        clauses = ["s.memory_namespace = ?", "m.operation IN ('write', 'update')"]
    # Hash only the latest event for each key so superseded history does not change state hashes.
    rows = connection.execute(
        f"""
        SELECT memory_key, value_json, provenance, confidence, expires_at, status
        FROM (
            SELECT m.memory_key, m.value_json, m.provenance, m.confidence,
                   m.expires_at, m.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.memory_key
                       ORDER BY m.created_at DESC, m.id DESC
                   ) AS row_number
            FROM memory_events m
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(clauses)}
        ) latest
        WHERE row_number = 1
        ORDER BY memory_key
        """,
        values,
    ).fetchall()
    # Filter out expired entries from the payload, convert to dicts
    payload = [dict(row) for row in rows if not _is_expired(row["expires_at"])]
    # Hash the canonical JSON representation for a stable, comparable snapshot
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def write_memory(
    connection: Any,
    session_id: str,
    key: str,
    value: Any,
    *,
    turn_id: str | None = None,
    source_turn_id: str | None = None,
    provenance: str = "explicit",
    confidence: float = 1.0,
    expires_at: str | datetime | None = None,
    status: str = "active",
    operation: str = "write",
    commit: bool = True,
) -> str:
    """Insert a memory event record and return its ID."""
    # Validate that a memory key was provided
    if not key:
        raise ValueError("memory key is required")
    # Validate that confidence is a finite number between 0 and 1
    if (
        not isinstance(confidence, (int, float))
        or confidence != confidence
        or not 0 <= float(confidence) <= 1
    ):
        raise ValueError("confidence must be a finite value between 0 and 1")
    # Validate the operation type
    if operation not in {"read", "write", "update", "delete", "quarantine"}:
        raise ValueError("invalid memory operation")
    # Validate the status
    if status not in {"active", "quarantined", "expired", "deleted"}:
        raise ValueError("invalid memory status")
    # Generate a unique memory event ID
    memory_id = new_id("mem_")
    # Insert the memory event with redacted values
    connection.execute(
        """
        INSERT INTO memory_events (
            id, session_id, turn_id, operation, memory_key, value_json,
            source_turn_id, provenance, confidence, expires_at, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            session_id,
            turn_id,
            operation,
            redact(key),
            _json_value(redact(value)),
            source_turn_id,
            redact(provenance or "explicit"),
            float(confidence),
            _iso_expiry(expires_at),
            status,
            utc_now(),
        ),
    )
    if commit:
        _commit(connection)
    return memory_id


def record_memory_event(
    connection: Any,
    session_id: str,
    key: str,
    value: Any = None,
    *,
    operation: str = "read",
    turn_id: str | None = None,
    source_turn_id: str | None = None,
    provenance: str = "system",
    confidence: float = 1.0,
    expires_at: str | datetime | None = None,
    status: str = "active",
    commit: bool = True,
) -> str:
    """Wrapper around write_memory for recording memory read/update/delete events."""
    return write_memory(
        connection,
        session_id,
        key,
        value,
        turn_id=turn_id,
        source_turn_id=source_turn_id,
        provenance=provenance,
        confidence=confidence,
        expires_at=expires_at,
        status=status,
        operation=operation,
        commit=commit,
    )


def write_summary_memory(
    connection: Any,
    session_id: str,
    key: str,
    summary: str,
    **kwargs: Any,
) -> str:
    """Write a conversation summary to memory with assistant_summary provenance."""
    return write_memory(
        connection,
        session_id,
        key,
        summary,
        provenance=kwargs.pop("provenance", "assistant_summary"),
        **kwargs,
    )


def write_document_memory(
    connection: Any,
    session_id: str,
    key: str,
    document: Any,
    **kwargs: Any,
) -> str:
    """Write a retrieved document to memory with retrieved_document provenance."""
    return write_memory(
        connection,
        session_id,
        key,
        document,
        provenance=kwargs.pop("provenance", "retrieved_document"),
        **kwargs,
    )


def search_memory(
    connection: Any,
    namespace: str,
    query: str,
    *,
    policy: str = "active",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search memory entries by keyword, optionally filtered by trust policy."""
    if policy not in {"active", "all", "trusted", "untrusted"}:
        raise ValueError("policy must be active, all, trusted, or untrusted")
    # Return empty list for empty queries
    if not query.strip():
        return []
    rows = connection.execute(
        """
            SELECT m.*
            FROM memory_events m
            JOIN sessions s ON s.id = m.session_id
            WHERE s.memory_namespace = ?
              AND m.operation IN ('write', 'update')
              AND (m.status = 'active' OR ? != 'active')
              AND (lower(m.memory_key) LIKE ? OR lower(COALESCE(m.value_json, '')) LIKE ?)
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT ?
            """,
        (
            namespace,
            policy,
            f"%{query.lower()}%",
            f"%{query.lower()}%",
            max(1, int(limit)),
        ),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Apply status filter for "active" policy
        if policy == "active" and item.get("status") != "active":
            continue
        # Apply expiry filter for non-"all" policies
        if policy != "all" and _is_expired(item.get("expires_at")):
            continue
        # Apply provenance filter for trusted/untrusted policies
        if policy in {"trusted", "untrusted"} and not _memory_policy_match(
            item.get("provenance") or "", policy
        ):
            continue
        # Parse the JSON value for the result
        item["value"] = _parse_json(item.pop("value_json", None))
        result.append(item)
        if len(result) >= limit:
            break
    return result


def read_memory(
    connection: Any,
    namespace: str,
    *,
    policy: str = "active",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read memory entries allowed by the selected policy."""
    if policy not in {"active", "all", "trusted", "untrusted"}:
        raise ValueError("policy must be active, all, trusted, or untrusted")
    clauses = ["s.memory_namespace = ?", "m.operation IN ('write', 'update')"]
    values: list[Any] = [namespace]
    # For "active" policy: only active write/update entries
    if policy == "active":
        clauses.extend(["m.status = 'active'", "m.operation IN ('write', 'update')"])
    # For "trusted"/"untrusted" policy: also filter to active write/update (then further filter by provenance)
    elif policy in {"trusted", "untrusted"}:
        clauses.extend(["m.status = 'active'", "m.operation IN ('write', 'update')"])
    rows = connection.execute(
        f"""
        SELECT m.*, s.memory_namespace, s.phase
        FROM memory_events m
        JOIN sessions s ON s.id = m.session_id
        WHERE {' AND '.join(clauses)}
        ORDER BY m.created_at, m.id
        LIMIT ?
        """,
        # For trusted/untrusted policies, fetch extra rows (4x) to account for provenance filtering
        [
            *values,
            max(
                1, int(limit) * 4 if policy in {"trusted", "untrusted"} else int(limit)
            ),
        ],
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Filter by provenance for trusted/untrusted policies
        if policy in {"trusted", "untrusted"} and not _memory_policy_match(
            item.get("provenance") or "", policy
        ):
            continue
        # Filter expired entries for non-"all" policies
        if policy != "all" and _is_expired(item.get("expires_at")):
            continue
        item["value"] = _parse_json(item.pop("value_json", None))
        item["expired"] = _is_expired(item.get("expires_at"))
        result.append(item)
        if len(result) >= limit:
            break
    return result


def get_memory(
    connection: Any,
    namespace: str,
    key: str,
    *,
    include_inactive: bool = False,
) -> dict[str, Any] | None:
    """Get the latest memory entry for a key in a namespace, or None if not found."""
    clauses = [
        "s.memory_namespace = ?",
        "m.memory_key = ?",
        "m.operation IN ('write', 'update')",
    ]
    values: list[Any] = [namespace, redact(key)]
    # Exclude inactive entries unless explicitly requested
    if not include_inactive:
        clauses.append("m.status = 'active'")
    # Get the most recent entry (ORDER BY created_at DESC, id DESC) limited to 1
    row = connection.execute(
        f"""
        SELECT m.*, s.memory_namespace
        FROM memory_events m
        JOIN sessions s ON s.id = m.session_id
        WHERE {' AND '.join(clauses)}
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT 1
        """,
        values,
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["value"] = _parse_json(item.pop("value_json", None))
    item["expired"] = _is_expired(item.get("expires_at"))
    # If not including inactive, return None for deleted/expired entries
    if not include_inactive and (item.get("status") != "active" or item["expired"]):
        return None
    return item


def quarantine_memory(
    connection: Any,
    namespace: str,
    key: str | None = None,
    *,
    turn_id: str | None = None,
    commit: bool = True,
) -> int:
    """Mark active memory entries as quarantined, returning the count affected."""
    # Build query clauses: namespace + active status, optionally filtered by key or turn_id
    clauses = ["s.memory_namespace = ?", "m.status = 'active'"]
    values: list[Any] = [namespace]
    if key is not None:
        clauses.append("m.memory_key = ?")
        values.append(redact(key))
    if turn_id is not None:
        clauses.append("m.turn_id = ?")
        values.append(turn_id)
    # Update status to 'quarantined' for matching entries
    cursor = connection.execute(
        f"""
        UPDATE memory_events AS m
        SET status = 'quarantined'
        WHERE m.session_id IN (
            SELECT s.id FROM sessions AS s WHERE {' AND '.join(clauses)}
        )
        """,
        values,
    )
    if commit:
        _commit(connection)
    return cursor.rowcount


def delete_memory(
    connection: Any,
    namespace: str,
    key: str | None = None,
    *,
    turn_id: str | None = None,
    commit: bool = True,
) -> int:
    """Mark memory entries as deleted (soft-delete), returning the count affected."""
    # Build query clauses: namespace + non-deleted status, optionally filtered by key or turn_id
    clauses = ["s.memory_namespace = ?", "m.status != 'deleted'"]
    values: list[Any] = [namespace]
    if key is not None:
        clauses.append("m.memory_key = ?")
        values.append(redact(key))
    if turn_id is not None:
        clauses.append("m.turn_id = ?")
        values.append(turn_id)
    # Update status to 'deleted' for matching entries
    cursor = connection.execute(
        f"""
        UPDATE memory_events AS m
        SET status = 'deleted'
        WHERE m.session_id IN (
            SELECT s.id FROM sessions AS s WHERE {' AND '.join(clauses)}
        )
        """,
        values,
    )
    if commit:
        _commit(connection)
    return cursor.rowcount


def reset_memory(
    connection: Any,
    namespace: str,
    *,
    commit: bool = True,
) -> int:
    """Delete all active memory entries in a namespace (soft-delete), returning count."""
    cursor = connection.execute(
        """
        UPDATE memory_events
        SET status = 'deleted'
        WHERE session_id IN (
            SELECT id FROM sessions WHERE memory_namespace = ?
        ) AND status != 'deleted'
        """,
        (namespace,),
    )
    if commit:
        _commit(connection)
    return cursor.rowcount


def expire_memory(
    connection: Any,
    *,
    namespace: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Mark all expired memory entries as 'expired' status, optionally scoped to a namespace."""
    # Use provided datetime or current UTC time
    current = now or datetime.now(timezone.utc)
    # Build namespace clause if a namespace is specified
    namespace_clause = (
        "AND session_id IN (SELECT id FROM sessions WHERE memory_namespace = ?)"
        if namespace
        else ""
    )
    values: list[Any] = [
        current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    ]
    if namespace:
        values.append(namespace)
    # Update status to 'expired' for entries that are currently active and past their expiry
    cursor = connection.execute(
        f"""
        UPDATE memory_events
        SET status = 'expired'
        WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
        {namespace_clause}
        """,
        values,
    )
    if commit:
        _commit(connection)
    return cursor.rowcount


def list_memory_events(
    connection: Any,
    *,
    session_id: str | None = None,
    namespace: str | None = None,
    status: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """List memory events with optional filtering by session, namespace, or status."""
    clauses: list[str] = []
    values: list[Any] = []
    if session_id:
        clauses.append("m.session_id = ?")
        values.append(session_id)
    if namespace:
        clauses.append("s.memory_namespace = ?")
        values.append(namespace)
    if status:
        clauses.append("m.status = ?")
        values.append(status)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"""
        SELECT m.*, s.memory_namespace, s.phase
        FROM memory_events m
        JOIN sessions s ON s.id = m.session_id
        {where}
        ORDER BY m.created_at, m.id
        LIMIT ?
        """,
        [*values, max(1, int(limit))],
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Parse JSON value for the result
        item["value"] = _parse_json(item.pop("value_json", None))
        result.append(item)
    return result


__all__ = [
    "delete_memory",
    "expire_memory",
    "get_memory",
    "list_memory_events",
    "memory_snapshot_hash",
    "quarantine_memory",
    "read_memory",
    "record_memory_event",
    "reset_memory",
    "search_memory",
    "write_document_memory",
    "write_memory",
    "write_summary_memory",
]
