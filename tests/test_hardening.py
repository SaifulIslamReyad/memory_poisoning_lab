import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.agent import AgentRunner
from core.archive import export_experiment, import_archive, verify_archive
from core.catalog import upsert_model
from core.config import LabConfig, redact
from core.database import initialize_lab_db, initialize_models_db
from core.experiments import create_experiment, create_session
from core.memory import (
    read_memory,
    search_memory,
    write_document_memory,
    write_memory,
)
from core.mitigations import apply_mitigation
from core.models import ModelCapabilities, RequestValidationError, validate_request


class FakeCompletions:
    def __init__(self):
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return {
            "choices": [
                {
                    "message": {"content": "response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }


class FakeClient:
    def __init__(self):
        self.completions = FakeCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


class AgentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = Path(tempfile.mkdtemp())
        self.models_db = self.tempdir / "models.sqlite"
        self.lab_db = self.tempdir / "lab.sqlite"
        self.models = initialize_models_db(self.models_db)
        self.lab = initialize_lab_db(self.lab_db)
        try:
            upsert_model(
                self.models,
                {
                    "id": "test/model",
                    "object": "model",
                    "active": True,
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "features": [],
                    "sampling_parameters": [],
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                },
            )
            self.models.commit()
            experiment_id = create_experiment(
                self.lab, "agent evidence", model_id="test/model"
            )
            self.session_id = create_session(self.lab, experiment_id)
            write_memory(
                self.lab,
                self.session_id,
                "fact",
                {"value": "trusted"},
                provenance="trusted",
            )
            self.lab.commit()
            self.experiment_id = experiment_id
        finally:
            self.models.close()
            self.lab.close()

    def test_provider_request_is_separate_from_redacted_evidence(self):
        client = FakeClient()
        config = LabConfig(base_dir=self.tempdir, models_db=self.models_db, lab_db=self.lab_db)
        runner = AgentRunner(config=config, client=client)
        result = runner.run_turn(
            self.experiment_id,
            self.session_id,
            "hello apiKey=secret-value-123456",
        )

        sent = client.completions.request
        self.assertIn("secret-value-123456", sent["messages"][-1]["content"])
        self.assertNotIn(
            "secret-value-123456",
            result["request"]["messages"][-1]["content"],
        )
        connection = sqlite3.connect(self.lab_db)
        connection.row_factory = sqlite3.Row
        try:
            reads = connection.execute(
                "SELECT COUNT(*) FROM memory_events WHERE operation = 'read'"
            ).fetchone()[0]
            metrics = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM metrics WHERE experiment_id = ?",
                    (self.experiment_id,),
                )
            }
        finally:
            connection.close()
        self.assertEqual(reads, 1)
        self.assertIn("latency_ms", metrics)
        self.assertIn("estimated_cost", metrics)


class PersistenceAndPolicyTests(unittest.TestCase):
    def test_sessions_reuse_experiment_namespace_by_default(self):
        tempdir = Path(tempfile.mkdtemp())
        connection = initialize_lab_db(tempdir / "lab.sqlite")
        try:
            experiment_id = create_experiment(connection, "namespace")
            first = create_session(connection, experiment_id)
            second = create_session(connection, experiment_id)
            rows = connection.execute(
                "SELECT memory_namespace FROM sessions ORDER BY started_at"
            ).fetchall()
            self.assertEqual(rows[0]["memory_namespace"], rows[1]["memory_namespace"])
            self.assertNotEqual(first, second)
        finally:
            connection.close()

    def test_mitigation_quarantines_untrusted_memory(self):
        tempdir = Path(tempfile.mkdtemp())
        connection = initialize_lab_db(tempdir / "lab.sqlite")
        try:
            experiment_id = create_experiment(connection, "mitigation")
            session_id = create_session(connection, experiment_id)
            write_memory(
                connection,
                session_id,
                "poison",
                "bad",
                provenance="poison:lab_canary",
            )
            result = apply_mitigation(
                connection,
                experiment_id,
                "trusted_sources_only",
                session_id=session_id,
            )
            self.assertGreaterEqual(result["affected"], 1)
            row = connection.execute(
                "SELECT status FROM memory_events WHERE memory_key = 'poison'"
            ).fetchone()
            self.assertEqual(row["status"], "quarantined")
        finally:
            connection.close()

    def test_document_memory_search(self):
        tempdir = Path(tempfile.mkdtemp())
        connection = initialize_lab_db(tempdir / "lab.sqlite")
        try:
            experiment_id = create_experiment(connection, "search")
            session_id = create_session(connection, experiment_id)
            write_document_memory(
                connection,
                session_id,
                "doc",
                {"text": "needle in document"},
                provenance="retrieved_document",
            )
            results = search_memory(connection, connection.execute(
                "SELECT memory_namespace FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()["memory_namespace"], "needle")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["memory_key"], "doc")
        finally:
            connection.close()


class ModelValidationTests(unittest.TestCase):
    def test_structured_schema_and_tool_validation(self):
        capabilities = ModelCapabilities(
            model_id="test/model",
            active=True,
            features=frozenset({"structured_outputs", "tools"}),
            sampling_parameters=frozenset({"max_tokens"}),
            max_completion_tokens=100,
        )
        validated = validate_request(
            capabilities,
            {
                "max_tokens": 10,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "strict": True,
                        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    },
                },
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            },
        )
        self.assertEqual(validated["max_tokens"], 10)
        with self.assertRaises(RequestValidationError):
            validate_request(
                capabilities,
                {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "bad", "schema": []},
                    }
                },
            )


class RedactionTests(unittest.TestCase):
    def test_camel_case_and_assignment_redaction(self):
        value = {"clientSecret": "secret-value-123456", "note": "apiKey=another-secret"}
        result = redact(value)
        self.assertEqual(result["clientSecret"], "[REDACTED]")
        self.assertIn("[REDACTED]", result["note"])


class ArchiveImportTests(unittest.TestCase):
    def test_import_requires_verified_archive(self):
        tempdir = Path(tempfile.mkdtemp())
        models = initialize_models_db(tempdir / "models.sqlite")
        lab = initialize_lab_db(tempdir / "lab.sqlite")
        try:
            upsert_model(models, {"id": "test/model", "active": True})
            models.commit()
            experiment_id = create_experiment(lab, "archive", model_id="test/model")
            create_session(lab, experiment_id)
            lab.commit()
            config = LabConfig(
                base_dir=tempdir,
                models_db=tempdir / "models.sqlite",
                lab_db=tempdir / "lab.sqlite",
                archives_dir=tempdir / "archives",
            )
            exported = export_experiment(experiment_id, config=config)
            self.assertTrue(verify_archive(exported["path"])["valid"])
            imported = import_archive(exported["path"], config=config)
            self.assertTrue(Path(imported["path"]).exists())
        finally:
            models.close()
            lab.close()


if __name__ == "__main__":
    unittest.main()
