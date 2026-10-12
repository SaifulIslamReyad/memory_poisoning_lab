"""Tests for core.archive — no network or third-party packages required."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.archive import export_experiment, list_archives, verify_archive
from core.config import LabConfig
from core.database import connect_sqlite, initialize_lab_db
from core.experiments import create_experiment, create_session
from core.memory import write_memory


class TestExportExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.lab_db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.lab_db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp1', 'Test Experiment', 'completed', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses1', 'exp1', 'session_abc', 'ns1', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO turns (id, session_id, sequence, phase, role, content, created_at) VALUES ('turn1', 'ses1', 0, 'baseline', 'user', 'hello', '2025-01-01T00:01:00Z')"
        )
        self.conn.execute(
            "INSERT INTO memory_events (id, session_id, turn_id, operation, memory_key, value_json, provenance, confidence, status, created_at) VALUES ('mem1', 'ses1', 'turn1', 'write', 'fact1', '{\"value\":\"data\"}', 'explicit', 1.0, 'active', '2025-01-01T00:01:00Z')"
        )
        self.conn.commit()

        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir()
        self.lab_copy = self.data_dir / "lab.sqlite"
        shutil.copy2(self.lab_db_path, self.lab_copy)

        self.config = LabConfig(
            base_dir=self.tmpdir,
            models_db=self.data_dir / "models.sqlite",
            lab_db=self.lab_copy,
            archives_dir=self.data_dir / "archives",
            sandbox_dir=self.data_dir / "sandbox",
            fixture_db=self.tmpdir / "ecommerce.db",
            model_snapshot=self.tmpdir / "snapshot.json",
            groq_base_url="https://api.groq.com/openai/v1",
            request_timeout=30.0,
            max_rows=100,
            max_result_bytes=1024 * 1024,
            max_query_steps=100000,
        )
        self.config.ensure_directories()

    def tearDown(self) -> None:
        self.conn.close()

    def test_exports_experiment(self) -> None:
        result = export_experiment("exp1", config=self.config)
        self.assertEqual(result["experiment_id"], "exp1")
        self.assertEqual(result["status"], "verified")
        self.assertIsNotNone(result["manifest_hash"])
        self.assertTrue(Path(result["path"]).exists())

    def test_unknown_experiment(self) -> None:
        with self.assertRaises(KeyError):
            export_experiment("unknown", config=self.config)

    def test_archive_contains_files(self) -> None:
        result = export_experiment("exp1", config=self.config)
        archive_dir = Path(result["path"])
        self.assertTrue((archive_dir / "experiment.sqlite").exists())
        self.assertTrue((archive_dir / "manifest.json").exists())
        self.assertTrue((archive_dir / "schema.sql").exists())

    def test_manifest_verifiable(self) -> None:
        result = export_experiment("exp1", config=self.config)
        verification = verify_archive(result["path"])
        self.assertTrue(verification["valid"])


class TestListArchives(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        data_dir = self.tmpdir / "data"
        data_dir.mkdir()
        self.config = LabConfig(
            base_dir=self.tmpdir,
            models_db=data_dir / "models.sqlite",
            lab_db=data_dir / "lab.sqlite",
            archives_dir=data_dir / "archives",
            sandbox_dir=data_dir / "sandbox",
            fixture_db=self.tmpdir / "ecommerce.db",
            model_snapshot=self.tmpdir / "snapshot.json",
            groq_base_url="https://api.groq.com/openai/v1",
            request_timeout=30.0,
            max_rows=100,
            max_result_bytes=1024 * 1024,
            max_query_steps=100000,
        )

    def test_empty_when_no_archives(self) -> None:
        archives = list_archives(self.config)
        self.assertEqual(archives, [])


class TestVerifyArchive(unittest.TestCase):
    def test_missing_manifest(self) -> None:
        result = verify_archive(Path(tempfile.mkdtemp()) / "nonexistent")
        self.assertFalse(result["valid"])

    def test_invalid_manifest_json(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        manifest = tmpdir / "manifest.json"
        manifest.write_text("not json")
        result = verify_archive(tmpdir)
        self.assertFalse(result["valid"])

    def test_rejects_path_traversal_entry(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        manifest = tmpdir / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "archive_format": "agentic-ai-security-lab",
                    "manifest_hash": "0" * 64,
                    "files": [{"name": "../outside", "size_bytes": 0, "sha256": "0" * 64}],
                }
            ),
            encoding="utf-8",
        )
        result = verify_archive(tmpdir)
        self.assertFalse(result["valid"])
        self.assertTrue(any("unsafe file path" in error for error in result["errors"]))

    def test_rejects_unlisted_file(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        manifest = tmpdir / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "archive_format": "agentic-ai-security-lab",
                    "manifest_hash": "0" * 64,
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        (tmpdir / "extra").write_text("extra", encoding="utf-8")
        result = verify_archive(tmpdir)
        self.assertFalse(result["valid"])
        self.assertTrue(any("unlisted file" in error for error in result["errors"]))
