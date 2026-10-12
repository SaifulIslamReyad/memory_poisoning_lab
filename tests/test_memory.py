"""Tests for core.memory — no network or third-party packages required."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import new_id
from core.database import connect_sqlite, initialize_lab_db
from core.experiments import append_turn
from core.memory import (
    delete_memory,
    expire_memory,
    get_memory,
    list_memory_events,
    memory_snapshot_hash,
    quarantine_memory,
    read_memory,
    record_memory_event,
    reset_memory,
    write_memory,
)


class TestWriteMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_writes_entry(self) -> None:
        mem_id = write_memory(self.conn, "ses_test", "key1", value="hello")
        self.assertTrue(mem_id.startswith("mem_"))
        row = self.conn.execute("SELECT * FROM memory_events WHERE id = ?", (mem_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["memory_key"], "key1")
        self.assertEqual(row["value_json"], '"hello"')

    def test_requires_key(self) -> None:
        with self.assertRaises(ValueError):
            write_memory(self.conn, "ses_test", "", value="hello")

    def test_invalid_operation(self) -> None:
        with self.assertRaises(ValueError):
            write_memory(self.conn, "ses_test", "k", value="x", operation="invalid")

    def test_invalid_status(self) -> None:
        with self.assertRaises(ValueError):
            write_memory(self.conn, "ses_test", "k", value="x", status="bogus")

    def test_does_not_commit_with_false(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a", commit=False)
        write_memory(self.conn, "ses_test", "k2", value="b", commit=False)
        other = connect_sqlite(self.db_path, readonly=True)
        try:
            count = other.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        finally:
            other.close()
        self.assertEqual(count, 0)


class TestReadMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_read_active(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="active_val")
        results = read_memory(self.conn, "ns_test", policy="active")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "active_val")

    def test_read_all(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a", status="active")
        write_memory(self.conn, "ses_test", "k2", value="b", status="deleted")
        results = read_memory(self.conn, "ns_test", policy="all")
        self.assertEqual(len(results), 2)

    def test_read_trusted(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a", provenance="explicit")
        write_memory(self.conn, "ses_test", "k2", value="b", provenance="untrusted-source")
        results = read_memory(self.conn, "ns_test", policy="trusted")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "a")

    def test_read_untrusted(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a", provenance="explicit")
        write_memory(self.conn, "ses_test", "k2", value="b", provenance="untrusted-source")
        results = read_memory(self.conn, "ns_test", policy="untrusted")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "b")

    def test_invalid_policy(self) -> None:
        with self.assertRaises(ValueError):
            read_memory(self.conn, "ns_test", policy="bogus")

    def test_missing_namespace_returns_empty(self) -> None:
        results = read_memory(self.conn, "nonexistent", policy="active")
        self.assertEqual(results, [])


class TestGetMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_get_existing(self) -> None:
        write_memory(self.conn, "ses_test", "key1", value="data")
        result = get_memory(self.conn, "ns_test", "key1")
        self.assertIsNotNone(result)
        self.assertEqual(result["value"], "data")

    def test_get_missing(self) -> None:
        self.assertIsNone(get_memory(self.conn, "ns_test", "nonexistent"))


class TestQuarantineDelete(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_quarantine(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a")
        write_memory(self.conn, "ses_test", "k2", value="b")
        count = quarantine_memory(self.conn, "ns_test", key="k1")
        self.assertEqual(count, 1)
        result = get_memory(self.conn, "ns_test", "k1", include_inactive=True)
        self.assertEqual(result["status"], "quarantined")

    def test_delete(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a")
        write_memory(self.conn, "ses_test", "k2", value="b")
        count = delete_memory(self.conn, "ns_test", key="k1")
        self.assertEqual(count, 1)
        result = get_memory(self.conn, "ns_test", "k1", include_inactive=True)
        self.assertEqual(result["status"], "deleted")

    def test_delete_all(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a")
        write_memory(self.conn, "ses_test", "k2", value="b")
        count = delete_memory(self.conn, "ns_test")
        self.assertEqual(count, 2)

    def test_quarantine_filters_by_turn_id(self) -> None:
        first_turn = append_turn(self.conn, "ses_test", role="user", content="first")
        second_turn = append_turn(self.conn, "ses_test", role="user", content="second")
        write_memory(self.conn, "ses_test", "k1", value="a", turn_id=first_turn)
        write_memory(self.conn, "ses_test", "k2", value="b", turn_id=second_turn)
        count = quarantine_memory(self.conn, "ns_test", turn_id=first_turn)
        self.assertEqual(count, 1)
        self.assertEqual(get_memory(self.conn, "ns_test", "k1", include_inactive=True)["status"], "quarantined")
        self.assertEqual(get_memory(self.conn, "ns_test", "k2", include_inactive=True)["status"], "active")

    def test_reset(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a", status="active")
        write_memory(self.conn, "ses_test", "k2", value="b", status="active")
        count = reset_memory(self.conn, "ns_test")
        self.assertEqual(count, 2)
        results = read_memory(self.conn, "ns_test", policy="all")
        self.assertTrue(all(r["status"] == "deleted" for r in results))


class TestExpireMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_expires_past(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        write_memory(self.conn, "ses_test", "k1", value="a", expires_at=past)
        write_memory(self.conn, "ses_test", "k2", value="b")
        count = expire_memory(self.conn)
        self.assertEqual(count, 1)
        expired = get_memory(self.conn, "ns_test", "k1", include_inactive=True)
        self.assertEqual(expired["status"], "expired")

    def test_does_not_expire_future(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        write_memory(self.conn, "ses_test", "k1", value="a", expires_at=future)
        count = expire_memory(self.conn)
        self.assertEqual(count, 0)


class TestListMemoryEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_other', 'exp_test', 'session_def', 'ns_other', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_by_session(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a")
        write_memory(self.conn, "ses_other", "k2", value="b")
        events = list_memory_events(self.conn, session_id="ses_test")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["memory_key"], "k1")

    def test_by_namespace(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a")
        write_memory(self.conn, "ses_other", "k2", value="b")
        events = list_memory_events(self.conn, namespace="ns_test")
        self.assertEqual(len(events), 1)

    def test_by_status(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a", status="active")
        write_memory(self.conn, "ses_test", "k2", value="b", status="deleted")
        events = list_memory_events(self.conn, session_id="ses_test", status="active")
        self.assertEqual(len(events), 1)


class TestRecordMemoryEvent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_records_read_event(self) -> None:
        event_id = record_memory_event(self.conn, "ses_test", "k1", operation="read")
        self.assertTrue(event_id.startswith("mem_"))
        row = self.conn.execute("SELECT * FROM memory_events WHERE id = ?", (event_id,)).fetchone()
        self.assertEqual(row["operation"], "read")
        self.assertEqual(row["provenance"], "system")


class TestMemorySnapshotHash(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.conn.execute(
            "INSERT INTO experiments (id, title, status, created_at) VALUES ('exp_test', 'Test Experiment', 'created', '2025-01-01T00:00:00Z')"
        )
        self.conn.execute(
            "INSERT INTO sessions (id, experiment_id, session_key, memory_namespace, phase, started_at) VALUES ('ses_test', 'exp_test', 'session_abc', 'ns_test', 'baseline', '2025-01-01T00:00:00Z')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_hash_changes_with_content(self) -> None:
        write_memory(self.conn, "ses_test", "k1", value="a")
        hash1 = memory_snapshot_hash(self.conn, "ns_test")
        write_memory(self.conn, "ses_test", "k2", value="b")
        hash2 = memory_snapshot_hash(self.conn, "ns_test")
        self.assertNotEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)
