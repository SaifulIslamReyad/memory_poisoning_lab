"""Tests for core.experiments — no network or third-party packages required."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.config import new_id
from core.database import initialize_lab_db
from core.experiments import (
    PHASES,
    ROLES,
    append_turn,
    create_experiment,
    create_session,
    finish_experiment,
    get_experiment,
    get_session,
    get_session_messages,
    get_turn,
    get_turns,
    list_experiments,
    list_sessions,
    transition_session,
    update_experiment_status,
)


class TestCreateExperiment(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()

    def test_creates_experiment(self) -> None:
        exp_id = create_experiment(self.conn, "My Experiment")
        self.assertTrue(exp_id.startswith("exp_"))
        result = get_experiment(self.conn, exp_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "My Experiment")
        self.assertEqual(result["status"], "created")

    def test_requires_title(self) -> None:
        with self.assertRaises(ValueError):
            create_experiment(self.conn, "")
        with self.assertRaises(ValueError):
            create_experiment(self.conn, "   ")

    def test_custom_experiment_id(self) -> None:
        exp_id = create_experiment(self.conn, "Test", experiment_id="custom_id")
        self.assertEqual(exp_id, "custom_id")

    def test_options_persisted(self) -> None:
        exp_id = create_experiment(
            self.conn,
            "Test",
            hypothesis="h",
            model_id="test/model",
            poison_vector="direct",
            mitigation_strategy="quarantine",
            notes="some notes",
        )
        result = get_experiment(self.conn, exp_id)
        self.assertEqual(result["hypothesis"], "h")
        self.assertEqual(result["model_id"], "test/model")
        self.assertEqual(result["poison_vector"], "direct")
        self.assertEqual(result["mitigation_strategy"], "quarantine")
        self.assertEqual(result["notes"], "some notes")


class TestCreateSession(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()

    def test_creates_session(self) -> None:
        exp_id = create_experiment(self.conn, "Test")
        session_id = create_session(self.conn, exp_id)
        self.assertTrue(session_id.startswith("ses_"))
        result = get_session(self.conn, session_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["experiment_id"], exp_id)

    def test_phase_validation(self) -> None:
        exp_id = create_experiment(self.conn, "Test")
        with self.assertRaises(ValueError):
            create_session(self.conn, exp_id, phase="invalid")

    def test_phase_options(self) -> None:
        exp_id = create_experiment(self.conn, "Test")
        for phase in PHASES:
            session_id = create_session(self.conn, exp_id, phase=phase)
            result = get_session(self.conn, session_id)
            self.assertEqual(result["phase"], phase)

    def test_namespace_auto_generated(self) -> None:
        exp_id = create_experiment(self.conn, "Test")
        session_id = create_session(self.conn, exp_id)
        result = get_session(self.conn, session_id)
        self.assertIn("namespace", result["memory_namespace"])


class TestAppendTurn(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        exp_id = create_experiment(self.conn, "Test")
        self.session_id = create_session(self.conn, exp_id)
        self.exp_id = exp_id

    def tearDown(self) -> None:
        self.conn.close()

    def test_append_and_read(self) -> None:
        turn_id = append_turn(self.conn, self.session_id, role="user", content="hello")
        self.assertTrue(turn_id.startswith("turn_"))
        turn = get_turn(self.conn, turn_id)
        self.assertIsNotNone(turn)
        self.assertEqual(turn["content"], "hello")
        self.assertEqual(turn["role"], "user")

    def test_invalid_role(self) -> None:
        with self.assertRaises(ValueError):
            append_turn(self.conn, self.session_id, role="invalid", content="hello")

    def test_unknown_session(self) -> None:
        with self.assertRaises(KeyError):
            append_turn(self.conn, "unknown_session", role="user", content="hello")

    def test_sequence_increment(self) -> None:
        append_turn(self.conn, self.session_id, role="user", content="first")
        append_turn(self.conn, self.session_id, role="assistant", content="second")
        turns = get_turns(self.conn, session_id=self.session_id)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["sequence"], 0)
        self.assertEqual(turns[1]["sequence"], 1)

    def test_turn_phase_inherits_session(self) -> None:
        session_id = create_session(self.conn, self.exp_id, phase="poison")
        append_turn(self.conn, session_id, role="user", content="hello")
        turns = get_turns(self.conn, session_id=session_id)
        self.assertEqual(turns[0]["phase"], "poison")

    def test_persists_json_fields(self) -> None:
        append_turn(
            self.conn,
            self.session_id,
            role="assistant",
            content="test",
            tool_input={"sql": "SELECT 1"},
            tool_output={"result": [1]},
            model_request={"model": "x"},
            model_response={"content": "hi"},
        )
        turns = get_turns(self.conn, session_id=self.session_id)
        self.assertEqual(turns[0]["tool_input"], {"sql": "SELECT 1"})
        self.assertEqual(turns[0]["tool_output"], {"result": [1]})
        self.assertEqual(turns[0]["model_request"], {"model": "x"})
        self.assertEqual(turns[0]["model_response"], {"content": "hi"})


class TestGetSessionMessages(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        exp_id = create_experiment(self.conn, "Test")
        self.session_id = create_session(self.conn, exp_id)

    def tearDown(self) -> None:
        self.conn.close()

    def test_returns_messages(self) -> None:
        append_turn(self.conn, self.session_id, role="user", content="hello")
        append_turn(self.conn, self.session_id, role="assistant", content="hi")
        messages = get_session_messages(self.conn, self.session_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")


class TestListExperiments(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()

    def test_lists_experiments(self) -> None:
        create_experiment(self.conn, "Experiment A")
        create_experiment(self.conn, "Experiment B")
        experiments = list_experiments(self.conn)
        self.assertEqual(len(experiments), 2)

    def test_filter_by_status(self) -> None:
        create_experiment(self.conn, "A")
        create_experiment(self.conn, "B")
        update_experiment_status(self.conn, list_experiments(self.conn)[0]["id"], "completed")
        completed = list_experiments(self.conn, status="completed")
        self.assertEqual(len(completed), 1)

    def test_session_count(self) -> None:
        exp_id = create_experiment(self.conn, "Test")
        create_session(self.conn, exp_id)
        create_session(self.conn, exp_id)
        experiments = list_experiments(self.conn)
        self.assertEqual(experiments[0]["session_count"], 2)


class TestTransitionSession(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        exp_id = create_experiment(self.conn, "Test")
        self.session_id = create_session(self.conn, exp_id)

    def tearDown(self) -> None:
        self.conn.close()

    def test_transitions_phase(self) -> None:
        transition_session(self.conn, self.session_id, "poison")
        result = get_session(self.conn, self.session_id)
        self.assertEqual(result["phase"], "poison")

    def test_invalid_phase(self) -> None:
        with self.assertRaises(ValueError):
            transition_session(self.conn, self.session_id, "invalid")

    def test_unknown_session_raises(self) -> None:
        with self.assertRaises(KeyError):
            transition_session(self.conn, "unknown", "poison")


class TestUpdateExperimentStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        self.exp_id = create_experiment(self.conn, "Test")

    def tearDown(self) -> None:
        self.conn.close()

    def test_updates_status(self) -> None:
        update_experiment_status(self.conn, self.exp_id, "running")
        result = get_experiment(self.conn, self.exp_id)
        self.assertEqual(result["status"], "running")

    def test_invalid_status(self) -> None:
        with self.assertRaises(ValueError):
            update_experiment_status(self.conn, self.exp_id, "bogus")

    def test_finish_experiment(self) -> None:
        finish_experiment(self.conn, self.exp_id)
        result = get_experiment(self.conn, self.exp_id)
        self.assertEqual(result["status"], "completed")


class TestGetTurns(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "lab.sqlite"
        self.conn = initialize_lab_db(self.db_path)
        exp_id = create_experiment(self.conn, "Test")
        session_id = create_session(self.conn, exp_id)
        append_turn(self.conn, session_id, role="user", content="hello")
        self.exp_id = exp_id

    def tearDown(self) -> None:
        self.conn.close()

    def test_by_experiment(self) -> None:
        turns = get_turns(self.conn, experiment_id=self.exp_id)
        self.assertEqual(len(turns), 1)

    def test_by_phase(self) -> None:
        exp_id = create_experiment(self.conn, "Test")
        session_id = create_session(self.conn, exp_id, phase="poison")
        append_turn(self.conn, session_id, role="user", content="hello")
        turns = get_turns(self.conn, experiment_id=exp_id, phase="poison")
        self.assertEqual(len(turns), 1)
