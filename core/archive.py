"""Hash-verifiable experiment archive export and verification."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .config import (
    LabConfig,
    canonical_json,
    json_dumps,
    load_config,
    new_id,
    redact,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .database import (
    LAB_SCHEMA,
    LAB_SCHEMA_VERSION,
    connect_sqlite,
    initialize_lab_db,
)

SOFTWARE_VERSION = "1.0.0"


def _safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in value
    )


def _decode_json(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json_dumps(redact(row)))
            handle.write("\n")


def _write_postgres_sandbox_snapshot(
    database_url: str,
    experiment_id: str,
    destination: Path,
) -> None:
    """Export the experiment's Neon sandbox schema as portable JSON."""
    from .database import PostgresConnection

    safe_id = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in experiment_id
    )
    schema = f"sandbox_{safe_id}"
    connection = PostgresConnection(database_url, readonly=True)
    snapshot: dict[str, Any] = {"schema": schema, "tables": {}}
    try:
        tables = connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (schema,),
        ).fetchall()
        for table_row in tables:
            table = table_row[0]
            if not isinstance(table, str) or not table.replace("_", "a").isalnum():
                continue
            quoted_table = '"' + table.replace('"', '""') + '"'
            rows = connection.execute(
                f'SELECT * FROM "{schema}".{quoted_table}'
            ).fetchall()
            snapshot["tables"][table] = [dict(row) for row in rows]
    finally:
        connection.close()
    destination.write_text(
        json.dumps(redact(snapshot), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _redacted_values(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return redact(dict(row))


def _insert_redacted(
    connection: sqlite3.Connection,
    table: str,
    row: sqlite3.Row | dict[str, Any],
) -> None:
    values = _redacted_values(row)
    columns = list(values)
    placeholders = ",".join("?" for _ in columns)
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    connection.execute(
        f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )


def _copy_filtered_lab_database(
    source_path: Path,
    target_path: Path,
    experiment_id: str,
) -> None:
    """Copy only one experiment and its related evidence into a new database."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = connect_sqlite(source_path, readonly=True)
    target = sqlite3.connect(target_path)
    target.row_factory = sqlite3.Row
    try:
        target.executescript(LAB_SCHEMA)
        target.execute("PRAGMA foreign_keys = ON")
        if (
            source.execute(
                "SELECT 1 FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            is None
        ):
            raise KeyError(f"unknown experiment: {experiment_id}")

        experiment_row = source.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        _insert_redacted(target, "experiments", experiment_row)
        sessions = source.execute(
            "SELECT * FROM sessions WHERE experiment_id = ? ORDER BY started_at, id",
            (experiment_id,),
        ).fetchall()
        session_ids = [row["id"] for row in sessions]
        for row in sessions:
            _insert_redacted(target, "sessions", row)

        turn_rows: list[sqlite3.Row] = []
        turn_ids: list[str] = []
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            turn_rows = source.execute(
                f"SELECT * FROM turns WHERE session_id IN ({placeholders}) ORDER BY session_id, sequence",
                session_ids,
            ).fetchall()
            turn_ids = [row["id"] for row in turn_rows]
            for row in turn_rows:
                _insert_redacted(target, "turns", row)

        memory_rows: list[sqlite3.Row] = []
        detection_rows: list[sqlite3.Row] = []
        if session_ids:
            session_placeholders = ",".join("?" for _ in session_ids)
            memory_rows = source.execute(
                f"SELECT * FROM memory_events WHERE session_id IN ({session_placeholders}) ORDER BY created_at, id",
                session_ids,
            ).fetchall()
            for row in memory_rows:
                _insert_redacted(target, "memory_events", row)
        if turn_ids:
            turn_placeholders = ",".join("?" for _ in turn_ids)
            detection_rows = source.execute(
                f"SELECT * FROM detections WHERE turn_id IN ({turn_placeholders}) ORDER BY created_at, id",
                turn_ids,
            ).fetchall()
            for row in detection_rows:
                _insert_redacted(target, "detections", row)

        mitigation_rows = source.execute(
            "SELECT * FROM mitigations WHERE experiment_id = ? ORDER BY applied_at, id",
            (experiment_id,),
        ).fetchall()
        for row in mitigation_rows:
            _insert_redacted(target, "mitigations", row)
        metric_rows = source.execute(
            "SELECT * FROM metrics WHERE experiment_id = ? ORDER BY created_at, id",
            (experiment_id,),
        ).fetchall()
        for row in metric_rows:
            _insert_redacted(target, "metrics", row)
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def _manifest_payload(
    experiment: dict[str, Any],
    sessions: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "archive_format": "agentic-ai-security-lab",
        "format_version": 1,
        "experiment": redact(experiment),
        "session_ids": [session["id"] for session in sessions],
        "model_id": experiment.get("model_id"),
        "created_at": utc_now(),
        "files": files,
        "schema_version": LAB_SCHEMA_VERSION,
        "software_version": SOFTWARE_VERSION,
        "credentials_excluded": True,
        "credential_note": "API keys and authorization headers are intentionally excluded.",
    }


def import_archive(
    archive_path: str | Path,
    config: LabConfig | None = None,
) -> dict[str, Any]:
    """Verify an archive and create an isolated review copy."""
    cfg = config or load_config()
    cfg.ensure_directories()
    source = Path(archive_path).expanduser().resolve(strict=True)
    verification = verify_archive(source)
    if not verification.get("valid"):
        raise ValueError(
            f"archive verification failed: {verification.get('errors', [])}"
        )
    manifest = verification.get("manifest") or {}
    experiment = manifest.get("experiment") or {}
    experiment_id = str(experiment.get("id") or source.parent.name or "archive")
    safe_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in experiment_id
    )
    review_root = cfg.archives_dir / "reviews"
    review_root.mkdir(parents=True, exist_ok=True)
    destination = review_root / f"review_{safe_id}_{new_id()}"
    shutil.copytree(source, destination)
    experiment_db = destination / "experiment.sqlite"
    if not experiment_db.exists():
        raise ValueError("archive does not contain experiment.sqlite")
    review = sqlite3.connect(f"file:{experiment_db.as_posix()}?mode=ro", uri=True)
    try:
        if review.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("archived experiment database failed integrity check")
    finally:
        review.close()
    return {
        "path": str(destination),
        "experiment_id": experiment_id,
        "verification": verification,
    }


def export_experiment(
    experiment_id: str,
    config: LabConfig | None = None,
    *,
    sandbox_path: str | Path | None = None,
    archive_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Export one experiment and return its manifest metadata."""
    cfg = config or load_config()
    cfg.ensure_directories()
    lab_target = cfg.database_target
    if isinstance(lab_target, Path) and not lab_target.exists():
        initialize_lab_db(lab_target).close()

    connection = connect_sqlite(lab_target, readonly=True)
    try:
        experiment_row = connection.execute(
            "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if experiment_row is None:
            raise KeyError(f"unknown experiment: {experiment_id}")
        experiment = dict(experiment_row)
        sessions = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM sessions WHERE experiment_id = ? ORDER BY started_at, id",
                (experiment_id,),
            ).fetchall()
        ]
        session_ids = [session["id"] for session in sessions]
        turns: list[dict[str, Any]] = []
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            turns = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM turns WHERE session_id IN ({placeholders}) ORDER BY session_id, sequence",
                    session_ids,
                ).fetchall()
            ]
        memory_rows: list[dict[str, Any]] = []
        detections: list[dict[str, Any]] = []
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            memory_rows = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM memory_events WHERE session_id IN ({placeholders}) ORDER BY created_at, id",
                    session_ids,
                ).fetchall()
            ]
            turn_ids = [turn["id"] for turn in turns]
            if turn_ids:
                turn_placeholders = ",".join("?" for _ in turn_ids)
                detections = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT * FROM detections WHERE turn_id IN ({turn_placeholders}) ORDER BY created_at, id",
                        turn_ids,
                    ).fetchall()
                ]
        mitigations = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM mitigations WHERE experiment_id = ? ORDER BY applied_at, id",
                (experiment_id,),
            ).fetchall()
        ]
    finally:
        connection.close()

    destination = (
        Path(archive_dir).expanduser().resolve()
        if archive_dir
        else cfg.archives_dir / _safe_name(experiment_id)
    )
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_dir():
        raise ValueError("archive destination must be a directory")
    temporary: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination_parent)
    )
    try:
        _copy_filtered_lab_database(
            lab_target, temporary / "experiment.sqlite", experiment_id
        )
        _write_jsonl(
            temporary / "requests.jsonl",
            [
                {
                    "turn_id": turn["id"],
                    "session_id": turn["session_id"],
                    "phase": turn["phase"],
                    "request": _decode_json(turn.get("model_request_json")),
                }
                for turn in turns
                if turn.get("model_request_json")
            ],
        )
        _write_jsonl(
            temporary / "responses.jsonl",
            [
                {
                    "turn_id": turn["id"],
                    "session_id": turn["session_id"],
                    "phase": turn["phase"],
                    "response": _decode_json(turn.get("model_response_json")),
                    "latency_ms": turn.get("latency_ms"),
                    "error": turn.get("error"),
                }
                for turn in turns
            ],
        )
        _write_jsonl(temporary / "memory.jsonl", memory_rows)
        _write_jsonl(temporary / "detections.jsonl", detections)
        _write_jsonl(temporary / "mitigations.jsonl", mitigations)
        (temporary / "schema.sql").write_text(LAB_SCHEMA, encoding="utf-8")

        sandbox_source = (
            Path(sandbox_path).expanduser().resolve()
            if sandbox_path
            else cfg.sandbox_dir / f"{_safe_name(experiment_id)}.db"
        )
        sandbox_target = temporary / "sandbox.sqlite"
        if cfg.database_url:
            _write_postgres_sandbox_snapshot(
                cfg.database_url,
                experiment_id,
                temporary / "sandbox.json",
            )
        elif sandbox_source.exists():
            shutil.copy2(sandbox_source, sandbox_target)
        else:
            with sqlite3.connect(sandbox_target) as handle:
                handle.execute("PRAGMA foreign_keys = ON")
                handle.commit()

        file_entries = []
        for path in sorted(temporary.iterdir()):
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"unsafe archive file: {path.name}")
            file_entries.append(
                {
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        manifest = _manifest_payload(experiment, sessions, file_entries)
        manifest_hash = sha256_bytes(canonical_json(manifest))
        manifest["manifest_hash"] = manifest_hash
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        verification = verify_archive(temporary)
        if not verification["valid"]:
            raise ValueError(
                "generated archive failed verification: "
                + "; ".join(verification["errors"])
            )
        total_size = (
            sum((temporary / item["name"]).stat().st_size for item in file_entries)
            + (temporary / "manifest.json").stat().st_size
        )

        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)

    archive_connection = connect_sqlite(lab_target)
    try:
        archive_connection.execute(
            """
            INSERT INTO archives (
                id, experiment_id, path, manifest_hash, size_bytes, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'verified')
            """,
            (
                f"archive_{experiment_id}_{utc_now().replace('-', '').replace(':', '').replace('.', '')}",
                experiment_id,
                str(destination),
                manifest_hash,
                total_size,
                utc_now(),
            ),
        )
        archive_connection.commit()
    finally:
        archive_connection.close()
    return {
        "experiment_id": experiment_id,
        "path": str(destination),
        "manifest_hash": manifest_hash,
        "size_bytes": total_size,
        "files": file_entries,
        "status": "verified",
    }


def _archive_entry_path(root: Path, name: str) -> Path | None:
    """Resolve a manifest entry without allowing paths outside the archive."""
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
    ):
        return None
    candidate = root / name
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if candidate.is_symlink() or resolved.parent != root:
        return None
    return resolved


def verify_archive(archive_path: str | Path) -> dict[str, Any]:
    """Verify every archived file and the manifest hash."""
    root = Path(archive_path).expanduser().resolve()
    manifest_path = root / "manifest.json"
    errors: list[str] = []
    if not root.is_dir():
        return {
            "valid": False,
            "errors": ["archive root is not a directory"],
            "manifest": None,
        }
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {
            "valid": False,
            "errors": ["missing or unsafe manifest.json"],
            "manifest": None,
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "valid": False,
            "errors": [f"invalid manifest: {exc}"],
            "manifest": None,
        }
    if not isinstance(manifest, dict):
        return {
            "valid": False,
            "errors": ["manifest must be an object"],
            "manifest": None,
        }
    expected_hash = manifest.pop("manifest_hash", None)
    if not isinstance(expected_hash, str):
        errors.append("manifest hash is missing or invalid")
    if manifest.get("archive_format") != "agentic-ai-security-lab":
        errors.append("unsupported archive format")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("manifest files must be a list")
        entries = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("manifest file entry must be an object")
            continue
        name = entry.get("name")
        size = entry.get("size_bytes")
        digest = entry.get("sha256")
        if not isinstance(name, str) or name in seen:
            errors.append(f"invalid or duplicate file name: {name!r}")
            continue
        seen.add(name)
        path = _archive_entry_path(root, name)
        if path is None:
            errors.append(f"unsafe file path: {name!r}")
            continue
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f"invalid file size: {name!r}")
            continue
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"invalid file hash: {name!r}")
            continue
        if path.stat().st_size != size:
            errors.append(f"size mismatch: {name}")
        try:
            if sha256_file(path) != digest:
                errors.append(f"hash mismatch: {name}")
        except OSError as exc:
            errors.append(f"cannot read file: {name}: {exc}")
    try:
        actual_files = {
            path.name
            for path in root.iterdir()
            if path.is_file() and not path.is_symlink() and path.name != "manifest.json"
        }
    except OSError as exc:
        actual_files = set()
        errors.append(f"cannot inspect archive directory: {exc}")
    if actual_files != seen:
        for missing in sorted(seen - actual_files):
            if f"missing file: {missing}" not in errors:
                errors.append(f"missing file: {missing}")
        for extra in sorted(actual_files - seen):
            errors.append(f"unlisted file: {extra}")
    actual_hash = sha256_bytes(canonical_json(manifest))
    if expected_hash != actual_hash:
        errors.append("manifest hash mismatch")
    return {
        "valid": not errors,
        "errors": errors,
        "manifest": manifest,
        "manifest_hash": expected_hash,
    }


def list_archives(config: LabConfig | None = None) -> list[dict[str, Any]]:
    cfg = config or load_config()
    if isinstance(cfg.database_target, Path) and not cfg.database_target.exists():
        return []
    connection = connect_sqlite(cfg.database_target, readonly=True)
    try:
        rows = connection.execute(
            "SELECT * FROM archives ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


archive_experiment = export_experiment

__all__ = [
    "SOFTWARE_VERSION",
    "archive_experiment",
    "export_experiment",
    "import_archive",
    "list_archives",
    "verify_archive",
]
