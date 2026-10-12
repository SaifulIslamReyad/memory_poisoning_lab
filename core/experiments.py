"""Experiment, session, and turn persistence services."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# Import utility functions for ID generation, JSON serialization, redaction, and timestamps
from .config import json_dumps, new_id, redact, utc_now
from .database import transaction

# Set of valid session phases for the experiment lifecycle
PHASES = {"baseline", "poison", "probe", "mitigation", "post_mitigation"}
# Set of valid message roles in the conversation
ROLES = {"system", "user", "assistant", "tool"}


def create_experiment(
    connection: sqlite3.Connection,
    title: str,
    *,
    hypothesis: str | None = None,
    model_id: str | None = None,
    memory_backend: str = "sqlite",
    poison_vector: str | None = None,
    mitigation_strategy: str | None = None,
    notes: str | None = None,
    experiment_id: str | None = None,
    commit: bool = True,
) -> str:
    """Create a new experiment record and return its ID."""
    # Validate that a non-empty title was provided
    if not title.strip():
        raise ValueError("experiment title is required")
    # Use provided experiment ID or generate a new one
    experiment_id = experiment_id or new_id("exp_")
    # Insert the experiment row with redacted title/hypothesis/notes
    connection.execute(
        """
        INSERT INTO experiments (
            id, title, hypothesis, model_id, memory_backend, poison_vector,
            mitigation_strategy, status, created_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?)
        """,
        (
            experiment_id,
            redact(title.strip()),
            redact(hypothesis),
            model_id,
            memory_backend,
            poison_vector,
            mitigation_strategy,
            utc_now(),
            redact(notes),
        ),
    )
    if commit:
        connection.commit()
    return experiment_id


def create_session(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    session_key: str | None = None,
    memory_namespace: str | None = None,
    phase: str = "baseline",
    session_id: str | None = None,
    commit: bool = True,
) -> str:
    """Create a new session within an experiment and return its ID."""
    # Validate the phase is one of the allowed values
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {sorted(PHASES)}")
    # Use provided session ID or generate a new one
    session_id = session_id or new_id("ses_")
    # Use provided session key or generate a new one
    session_key = session_key or new_id("session_")
    # If no namespace provided, reuse the first session's namespace or generate a new one
    if memory_namespace is None:
        existing = connection.execute(
            "SELECT memory_namespace FROM sessions WHERE experiment_id = ? ORDER BY started_at LIMIT 1",
            (experiment_id,),
        ).fetchone()
        memory_namespace = (
            existing["memory_namespace"]
            if existing is not None
            else f"namespace_{experiment_id}_{session_key}"
        )
    # Insert the session record
    connection.execute(
        """
        INSERT INTO sessions (
            id, experiment_id, session_key, memory_namespace, phase,
            started_at, memory_snapshot_hash, reset_count
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
        """,
        (session_id, experiment_id, session_key, memory_namespace, phase, utc_now()),
    )
    if commit:
        connection.commit()
    return session_id


def append_turn(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    role: str,
    content: str | None = None,
    phase: str | None = None,
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_output: Any = None,
    model_request: Any = None,
    model_response: Any = None,
    latency_ms: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    estimated_cost: str | None = None,
    error: str | None = None,
    turn_id: str | None = None,
    created_at: str | None = None,
    commit: bool = True,
) -> str:
    """Persist a conversation turn (message) and return its ID."""
    # Validate the role is one of the allowed values
    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    # Look up the session to get its phase and experiment_id
    session = connection.execute(
        "SELECT phase, experiment_id FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if session is None:
        raise KeyError(f"unknown session: {session_id}")
    # Use provided phase or fall back to session's current phase
    turn_phase = phase or session["phase"]
    if turn_phase not in PHASES:
        raise ValueError(f"phase must be one of {sorted(PHASES)}")
    # Compute the next sequence number for this session
    sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), -1) + 1 FROM turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    # Generate a turn ID if not provided
    turn_id = turn_id or new_id("turn_")
    # Insert the turn record with all optional fields, applying redaction as needed
    connection.execute(
        """
        INSERT INTO turns (
            id, session_id, sequence, phase, role, content, tool_name,
            tool_input_json, tool_output_json, model_request_json,
            model_response_json, latency_ms, prompt_tokens, completion_tokens,
            estimated_cost, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            session_id,
            sequence,
            turn_phase,
            role,
            redact(content) if content is not None else None,
            redact(tool_name) if tool_name is not None else None,
            json_dumps(redact(tool_input)) if tool_input is not None else None,
            json_dumps(redact(tool_output)) if tool_output is not None else None,
            json_dumps(redact(model_request)) if model_request is not None else None,
            json_dumps(redact(model_response)) if model_response is not None else None,
            latency_ms,
            prompt_tokens,
            completion_tokens,
            redact(estimated_cost) if estimated_cost is not None else None,
            redact(error) if error is not None else None,
            created_at or utc_now(),
        ),
    )
    if commit:
        connection.commit()
    return turn_id


def get_experiment(connection: sqlite3.Connection, experiment_id: str) -> dict[str, Any] | None:
    """Fetch a single experiment by ID, or None if not found."""
    row = connection.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    return dict(row) if row else None


def list_experiments(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    model_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """List experiments, optionally filtered by status or model_id, with counts."""
    clauses: list[str] = []
    values: list[Any] = []
    # Add status filter if provided
    if status:
        clauses.append("status = ?")
        values.append(status)
    # Add model_id filter if provided
    if model_id:
        clauses.append("model_id = ?")
        values.append(model_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM experiments{where} ORDER BY created_at DESC LIMIT ?",
        [*values, max(1, int(limit))],
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Count sessions for this experiment
        item["session_count"] = connection.execute(
            "SELECT COUNT(*) FROM sessions WHERE experiment_id = ?", (item["id"],)
        ).fetchone()[0]
        # Count turns (via sessions join) for this experiment
        item["turn_count"] = connection.execute(
            """
            SELECT COUNT(*) FROM turns t
            JOIN sessions s ON s.id = t.session_id
            WHERE s.experiment_id = ?
            """,
            (item["id"],),
        ).fetchone()[0]
        result.append(item)
    return result


def get_session(connection: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    """Fetch a single session by ID, or None if not found."""
    row = connection.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(
    connection: sqlite3.Connection,
    experiment_id: str | None = None,
    *,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """List sessions, optionally filtered by experiment or phase, with turn counts."""
    clauses: list[str] = []
    values: list[Any] = []
    if experiment_id:
        clauses.append("experiment_id = ?")
        values.append(experiment_id)
    if phase:
        clauses.append("phase = ?")
        values.append(phase)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM sessions{where} ORDER BY started_at, id", values
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Count turns for this session
        item["turn_count"] = connection.execute(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?", (item["id"],)
        ).fetchone()[0]
        result.append(item)
    return result


def get_turn(connection: sqlite3.Connection, turn_id: str) -> dict[str, Any] | None:
    """Fetch a single turn by ID, with JSON fields decoded, or None if not found."""
    row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    # Decode JSON columns back to Python objects
    for field in ("tool_input_json", "tool_output_json", "model_request_json", "model_response_json"):
        raw = item.pop(field, None)
        if raw:
            try:
                item[field.removesuffix("_json")] = json.loads(raw)
            except (TypeError, ValueError):
                item[field.removesuffix("_json")] = raw
    return item


def get_turns(
    connection: sqlite3.Connection,
    session_id: str | None = None,
    *,
    experiment_id: str | None = None,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """List turns, optionally filtered by session, experiment, or phase."""
    clauses: list[str] = []
    values: list[Any] = []
    if session_id:
        clauses.append("t.session_id = ?")
        values.append(session_id)
    if experiment_id:
        clauses.append("s.experiment_id = ?")
        values.append(experiment_id)
    if phase:
        clauses.append("t.phase = ?")
        values.append(phase)
    # Join sessions table only if filtering by experiment
    join = " JOIN sessions s ON s.id = t.session_id" if experiment_id else ""
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT t.* FROM turns t{join}{where} ORDER BY t.session_id, t.sequence", values
    ).fetchall()
    return [_decode_turn(row) for row in rows]


def get_session_messages(connection: sqlite3.Connection, session_id: str) -> list[dict[str, str]]:
    """Return the conversation messages for a session (system/user/assistant roles only)."""
    rows = connection.execute(
        "SELECT role, content FROM turns WHERE session_id = ? AND role IN ('system', 'user', 'assistant') AND content IS NOT NULL ORDER BY sequence",
        (session_id,),
    ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def transition_session(
    connection: sqlite3.Connection,
    session_id: str,
    phase: str,
    *,
    memory_snapshot_hash: str | None = None,
    commit: bool = True,
) -> None:
    """Transition a session to a new phase, optionally recording a memory snapshot hash."""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {sorted(PHASES)}")
    # Optionally include a memory_snapshot_hash update (uses COALESCE to preserve existing if None)
    hash_clause = ", memory_snapshot_hash = COALESCE(?, memory_snapshot_hash)" if memory_snapshot_hash else ""
    values: list[Any] = [phase]
    if memory_snapshot_hash:
        values.append(memory_snapshot_hash)
    values.append(session_id)
    cursor = connection.execute(
        f"UPDATE sessions SET phase = ?{hash_clause} WHERE id = ?", values
    )
    if cursor.rowcount == 0:
        raise KeyError(f"unknown session: {session_id}")
    if commit:
        connection.commit()


def update_experiment_status(
    connection: sqlite3.Connection,
    experiment_id: str,
    status: str,
    *,
    ended_at: str | None = None,
    commit: bool = True,
) -> None:
    """Update an experiment's status and optional end timestamp."""
    # Validate the status is one of the allowed values
    if status not in {"created", "running", "completed", "failed", "archived"}:
        raise ValueError("invalid experiment status")
    # Verify the experiment exists
    if connection.execute("SELECT COUNT(*) FROM experiments WHERE id = ?", (experiment_id,)).fetchone()[0] == 0:
        raise KeyError(f"unknown experiment: {experiment_id}")
    # Update status; set ended_at when transitioning to a terminal state
    connection.execute(
        "UPDATE experiments SET status = ?, ended_at = COALESCE(?, ended_at) WHERE id = ?",
        (status, ended_at or utc_now() if status in {"completed", "failed", "archived"} else None, experiment_id),
    )
    if commit:
        connection.commit()


def finish_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    status: str = "completed",
) -> None:
    """Mark an experiment as finished with the given terminal status."""
    update_experiment_status(connection, experiment_id, status, ended_at=utc_now())


def delete_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    commit: bool = True,
) -> bool:
    """Delete a session and all its cascaded data (turns, memory events).
    Returns True if session was found and deleted, False if not found.
    """
    # Check if the session exists before attempting deletion
    cursor = connection.execute("SELECT id FROM sessions WHERE id = ?", (session_id,))
    if cursor.fetchone() is None:
        return False
    # Foreign key cascades should handle turns and memory_events automatically
    connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    if commit:
        connection.commit()
    return True


def delete_experiment(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    commit: bool = True,
) -> bool:
    """Delete an experiment and all its cascaded data (sessions, turns, memory, detections, etc.).
    Returns True if experiment was found and deleted, False if not found.
    """
    # Check if the experiment exists before attempting deletion
    cursor = connection.execute("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if cursor.fetchone() is None:
        return False
    # Foreign key cascades should handle all related data automatically
    connection.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
    if commit:
        connection.commit()
    return True


def _decode_turn(row: sqlite3.Row) -> dict[str, Any]:
    """Decode JSON columns in a turn row back to Python objects."""
    item = dict(row)
    for field in ("tool_input_json", "tool_output_json", "model_request_json", "model_response_json"):
        raw = item.pop(field, None)
        if raw:
            try:
                item[field.removesuffix("_json")] = json.loads(raw)
            except (TypeError, ValueError):
                item[field.removesuffix("_json")] = raw
    return item



__all__ = [
    "PHASES",
    "ROLES",
    "append_turn",
    "create_experiment",
    "create_session",
    "delete_experiment",
    "delete_session",
    "finish_experiment",
    "get_experiment",
    "get_session",
    "get_session_messages",
    "get_turn",
    "get_turns",
    "list_experiments",
    "list_sessions",
    "transition_session",
    "update_experiment_status",
]
