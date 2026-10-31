"""Metric recording and experiment-level summaries.

This module provides functions to record arbitrary metrics (e.g., toxicity
scores, classification accuracy) against an experiment/session/turn, and to
compute aggregate summaries combining persistence, safety, quality, latency,
and cost metrics.
"""

from __future__ import annotations

import json
from typing import Any

from .config import json_dumps, new_id, utc_now    # json_dumps safely serializes to JSON; new_id generates prefixed IDs; utc_now gives normalized timestamp


def record_metric(
    connection: Any,
    experiment_id: str,
    name: str,
    value: float,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    label: str | None = None,
    details: Any = None,
    metric_id: str | None = None,
    commit: bool = True,
) -> str:
    """Persist a single metric measurement to the metrics table.

    Metrics are typed as (name, value) pairs but can carry an optional
    label and structured details payload for richer reporting.
    """
    # Guard: metric name must be a non-empty string
    if not name.strip():
        raise ValueError("metric name is required")
    metric_id = metric_id or new_id("metric_")  # generate an ID if not provided
    connection.execute(
        """
        INSERT INTO metrics (
            id, experiment_id, session_id, turn_id, name, value,
            label, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metric_id,
            experiment_id,                        # experiment the metric belongs to
            session_id,                           # optional session scope (nullable)
            turn_id,                              # optional turn scope (nullable)
            name.strip(),                         # stored without leading/trailing whitespace
            float(value),                         # ensure the value is a proper float
            label,                                # optional categorical label (e.g., "safe"/"unsafe")
            json_dumps(details) if details is not None else None,  # optional structured payload
            utc_now(),                            # timestamp of when the metric was recorded
        ),
    )
    if commit:
        connection.commit()                      # commit transaction if caller owns it
    return metric_id


def summarize_experiment(
    connection: Any,
    experiment_id: str,
) -> dict[str, Any]:
    """Compute persistence, safety, quality, latency, cost, and error metrics.

    Runs five aggregate queries (turns, detections, mitigations, memory, custom
    metrics) and combines the results into a single summary dict. All numeric
    values are coerced to proper Python types to ensure JSON serializability.
    """
    # Fetch the experiment record to verify existence and retrieve status/model
    experiment = connection.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise KeyError(f"unknown experiment: {experiment_id}")
    # --- Turn-level aggregates: session/turn counts, latency, errors, tokens, cost ---
    row = connection.execute(
        """
        SELECT
            COUNT(DISTINCT s.id) AS sessions,                  -- distinct sessions under this experiment
            COUNT(DISTINCT t.id) AS turns,                     -- total turns (user + assistant)
            COUNT(DISTINCT CASE WHEN t.role = 'assistant' THEN t.id END) AS assistant_turns,  -- only assistant turns
            COALESCE(AVG(t.latency_ms), 0) AS avg_latency_ms,  -- average per-turn latency
            COALESCE(SUM(CASE WHEN t.error IS NOT NULL AND t.error != '' THEN 1 ELSE 0 END), 0) AS errors,  -- error count
            COALESCE(SUM(CASE WHEN t.prompt_tokens IS NOT NULL THEN t.prompt_tokens ELSE 0 END), 0) AS prompt_tokens,   -- total prompt tokens
            COALESCE(SUM(CASE WHEN t.completion_tokens IS NOT NULL THEN t.completion_tokens ELSE 0 END), 0) AS completion_tokens,  -- total completion tokens
            COALESCE(SUM(CAST(t.estimated_cost AS REAL)), 0) AS total_cost                     -- total estimated cost
        FROM sessions s
        LEFT JOIN turns t ON t.session_id = s.id
        WHERE s.experiment_id = ?
        """,
        (experiment_id,),
    ).fetchone()
    # --- Detection-level aggregates: total detections, suspected, avg score ---
    detection = connection.execute(
        """
        SELECT COUNT(*) AS count,
               COALESCE(AVG(score), 0) AS avg_score,                                      -- mean detection confidence
               COALESCE(SUM(CASE WHEN label = 'poison_suspected' THEN 1 ELSE 0 END), 0) AS suspected  -- count of suspected poison
        FROM detections d
        JOIN turns t ON t.id = d.turn_id
        JOIN sessions s ON s.id = t.session_id
        WHERE s.experiment_id = ?
        """,
        (experiment_id,),
    ).fetchone()
    # --- Mitigation-level aggregates: total mitigations applied, with memory hash change ---
    mitigations = connection.execute(
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(CASE WHEN before_memory_hash != after_memory_hash THEN 1 ELSE 0 END), 0) AS changed  -- mitigations that actually changed memory
        FROM mitigations
        WHERE experiment_id = ?
        """,
        (experiment_id,),
    ).fetchone()
    # --- Memory-level aggregates: event counts by status and provenance ---
    memory = connection.execute(
        """
        SELECT COUNT(*) AS events,                                                       -- total memory events
               COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,  -- active events
               COALESCE(SUM(CASE WHEN status = 'quarantined' THEN 1 ELSE 0 END), 0) AS quarantined,  -- quarantined events
               COALESCE(SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END), 0) AS deleted,  -- deleted events
               COALESCE(SUM(CASE WHEN status = 'active' AND (                               -- untrusted active events
                    lower(provenance) LIKE '%untrusted%' OR
                    lower(provenance) LIKE '%poison%' OR
                    lower(provenance) LIKE '%canary%'
                ) THEN 1 ELSE 0 END), 0) AS untrusted
        FROM memory_events m
        JOIN sessions s ON s.id = m.session_id
        WHERE s.experiment_id = ?
        """,
        (experiment_id,),
    ).fetchone()
    # --- Custom metric names and values recorded against this experiment ---
    metrics = connection.execute(
        "SELECT name, value, label, details_json FROM metrics WHERE experiment_id = ? ORDER BY created_at, id",
        (experiment_id,),
    ).fetchall()
    custom = {}  # build a nested dict keyed by metric name
    for metric in metrics:
        item: dict[str, Any] = {"value": metric["value"]}
        if metric["label"]:                           # include label if present
            item["label"] = metric["label"]
        if metric["details_json"]:
            try:
                item["details"] = json.loads(metric["details_json"])  # parse structured details
            except ValueError:
                item["details"] = metric["details_json"]               # fall back to raw JSON string
        custom[metric["name"]] = item
    # Pre-coerce frequently-used values to int for safe arithmetic
    assistant_turns = int(row["assistant_turns"] or 0)
    suspected = int(detection["suspected"] or 0)
    detection_count = int(detection["count"] or 0)
    # Build and return the comprehensive summary
    return {
        "experiment_id": experiment_id,
        "status": experiment["status"],          # experiment lifecycle status
        "model_id": experiment["model_id"],       # model used in the experiment
        "sessions": int(row["sessions"] or 0),
        "turns": int(row["turns"] or 0),
        "assistant_turns": assistant_turns,
        "memory_events": int(memory["events"] or 0),
        "active_memory": int(memory["active"] or 0),
        "quarantined_memory": int(memory["quarantined"] or 0),
        "deleted_memory": int(memory["deleted"] or 0),
        "untrusted_memory": int(memory["untrusted"] or 0),
        # Persist rate = active events / total events; default 0 when no events
        "persistence_rate": (
            int(memory["active"] or 0) / int(memory["events"] or 1)
            if int(memory["events"] or 0)
            else 0.0
        ),
        # Contamination rate = untrusted active / total active; default 0 when no active
        "contamination_rate": (
            int(memory["untrusted"] or 0) / int(memory["active"] or 1)
            if int(memory["active"] or 0)
            else 0.0
        ),
        "detections": detection_count,
        "suspected_detections": suspected,
        # Detection rate = suspected detections / assistant turns
        "detection_rate": suspected / assistant_turns if assistant_turns else 0.0,
        "avg_detection_score": float(detection["avg_score"] or 0),
        "mitigations": int(mitigations["count"] or 0),
        "mitigations_changed_memory": int(mitigations["changed"] or 0),
        "avg_latency_ms": float(row["avg_latency_ms"] or 0),
        "total_cost": float(row["total_cost"] or 0),
        # Average cost per assistant turn; 0 when no assistant turns
        "avg_cost_per_turn": (
            float(row["total_cost"] or 0) / assistant_turns if assistant_turns else 0.0
        ),
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "errors": int(row["errors"] or 0),
        "custom": custom,                        # user-defined metric values
    }


def list_metric_names(connection: Any) -> list[str]:
    """Return the set of distinct metric names recorded across all experiments."""
    rows = connection.execute(
        "SELECT DISTINCT name FROM metrics ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


__all__ = ["list_metric_names", "record_metric", "summarize_experiment"]
