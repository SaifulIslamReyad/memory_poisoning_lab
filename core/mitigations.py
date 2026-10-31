"""Mitigation actions with before/after memory integrity evidence.

Each mitigation strategy transforms or quarantines memory events and records
a snapshot hash delta so the effect can be audited. Strategies are constrained
to a known set to prevent injection of arbitrary destructive actions.
"""

from __future__ import annotations

from typing import Any

from .config import (
    new_id,
    redact,
    utc_now,
)  # new_id generates IDs with prefix; redact scrubs secrets; utc_now gives timestamp
from .memory import (
    delete_memory,  # permanently remove memory events
    expire_memory,  # mark events as expired based on TTL
    memory_snapshot_hash,  # compute a deterministic hash over memory state
    quarantine_memory,  # move events to quarantined status
    reset_memory,  # reset all memory events in a namespace
)

# The set of valid mitigation strategy names; must be checked before dispatch
STRATEGIES = {
    "quarantine",  # quarantine suspicious memory events
    "delete",  # delete memory events entirely
    "trusted_sources_only",  # quarantine everything not from trusted sources
    "require_confirmation",  # quarantine all non-trusted/tool/system events
    "ttl",  # expire events based on TTL policy
    "reset",  # wipe and reset all memory in the namespace
}


def apply_mitigation(
    connection: Any,
    experiment_id: str,
    strategy: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    memory_key: str | None = None,
    namespace: str | None = None,
    result: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Apply a supported mitigation strategy and record its memory hash delta.

    The namespace is resolved from the experiment or session ID. The memory
    snapshot hash is computed before and after the mitigation so changes can
    be audited via the `mitigations` table.
    """
    # Guard: only known strategies are permitted
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {sorted(STRATEGIES)}")
    # Fetch the experiment to verify it exists
    experiment = connection.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise KeyError(f"unknown experiment: {experiment_id}")
    # Resolve the memory namespace from the session if not provided directly
    if namespace is None and session_id:
        session = connection.execute(
            "SELECT memory_namespace FROM sessions WHERE id = ? AND experiment_id = ?",
            (session_id, experiment_id),
        ).fetchone()
        if session is None:
            raise KeyError(f"unknown session for experiment: {session_id}")
        namespace = session["memory_namespace"]
    # Fall back to the first session's namespace for this experiment
    if namespace is None:
        rows = connection.execute(
            "SELECT memory_namespace FROM sessions WHERE experiment_id = ? ORDER BY started_at LIMIT 1",
            (experiment_id,),
        ).fetchall()
        namespace = (
            rows[0]["memory_namespace"] if rows else f"namespace_{experiment_id}"
        )
    # Validate that the namespace actually belongs to this experiment
    if (
        namespace
        and connection.execute(
            "SELECT 1 FROM sessions WHERE experiment_id = ? AND memory_namespace = ? LIMIT 1",
            (experiment_id, namespace),
        ).fetchone()
        is None
    ):
        raise ValueError("memory namespace does not belong to experiment")
    # Validate that the turn belongs to this experiment (if provided)
    if (
        turn_id
        and connection.execute(
            "SELECT 1 FROM turns t JOIN sessions s ON s.id = t.session_id WHERE t.id = ? AND s.experiment_id = ?",
            (turn_id, experiment_id),
        ).fetchone()
        is None
    ):
        raise KeyError(f"unknown turn for experiment: {turn_id}")
    # Capture the memory hash before mitigation to establish a baseline
    before = memory_snapshot_hash(connection, namespace)
    affected = 0  # count of rows affected by the mitigation
    # Dispatch to the appropriate strategy implementation
    if strategy == "quarantine":
        affected = quarantine_memory(
            connection, namespace, memory_key, turn_id=turn_id, commit=False
        )
    elif strategy == "delete":
        affected = delete_memory(
            connection, namespace, memory_key, turn_id=turn_id, commit=False
        )
    elif strategy == "reset":
        affected = reset_memory(connection, namespace, commit=False)
    elif strategy == "trusted_sources_only":
        # Quarantine all active events whose provenance mentions untrusted/poison/canary
        affected = connection.execute(
            """
            UPDATE memory_events AS m
            SET status = 'quarantined'
            WHERE m.session_id IN (
                SELECT s.id FROM sessions AS s
                WHERE s.memory_namespace = ? AND m.status = 'active'
                  AND (
                    lower(m.provenance) LIKE '%untrusted%'
                    OR lower(m.provenance) LIKE '%poison%'
                    OR lower(m.provenance) LIKE '%canary%'
                  )
            )
            """,
            (namespace,),
        ).rowcount
    elif strategy == "require_confirmation":
        # Quarantine all active events whose provenance is not explicitly trusted/tool/system
        affected = connection.execute(
            """
            UPDATE memory_events AS m
            SET status = 'quarantined'
            WHERE m.session_id IN (
                SELECT s.id FROM sessions AS s
                WHERE s.memory_namespace = ? AND m.status = 'active'
                  AND COALESCE(lower(m.provenance), '') NOT IN ('trusted', 'tool', 'system')
            )
            """,
            (namespace,),
        ).rowcount
    elif strategy == "ttl":
        # Expire (quarantine) events based on their TTL configuration
        affected = expire_memory(connection, namespace=namespace, commit=False)
    # Capture the memory hash after mitigation to show the delta
    after = memory_snapshot_hash(connection, namespace)
    mitigation_id = new_id("mit_")  # generate a unique ID for this mitigation record
    mitigation_result = (
        redact(result) if result else f"affected {affected} memory event(s)"
    )
    # Persist the mitigation record with before/after hashes
    connection.execute(
        """
        INSERT INTO mitigations (
            id, experiment_id, turn_id, strategy, applied_at,
            before_memory_hash, after_memory_hash, result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mitigation_id,
            experiment_id,
            turn_id,
            strategy,
            utc_now(),
            before,
            after,
            mitigation_result,
        ),
    )
    if commit:
        connection.commit()
    # Return a summary dict for programmatic consumers
    return {
        "id": mitigation_id,
        "experiment_id": experiment_id,
        "turn_id": turn_id,
        "strategy": strategy,
        "namespace": namespace,
        "before_memory_hash": before,
        "after_memory_hash": after,
        "result": mitigation_result,
        "affected": affected,
    }


def list_mitigations(
    connection: Any,
    *,
    experiment_id: str | None = None,
    turn_id: str | None = None,
    strategy: str | None = None,
) -> list[dict[str, Any]]:
    """Query mitigations with optional filtering by experiment, turn, or strategy."""
    clauses: list[str] = []  # collect WHERE clause fragments
    values: list[Any] = []  # collect parameter bindings
    if experiment_id:
        clauses.append("experiment_id = ?")
        values.append(experiment_id)
    if turn_id:
        clauses.append("turn_id = ?")
        values.append(turn_id)
    if strategy:
        clauses.append("strategy = ?")
        values.append(strategy)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM mitigations{where} ORDER BY applied_at, id", values
    ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["STRATEGIES", "apply_mitigation", "list_mitigations"]
