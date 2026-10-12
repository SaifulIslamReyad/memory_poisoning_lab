"""Tests for core.catalog — no network or third-party packages required."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.catalog import (
    catalog_stats,
    get_model,
    import_model_snapshot,
    list_models,
    model_ids,
    normalize_model,
    upsert_model,
)
from core.database import initialize_models_db


class TestNormalizeModel(unittest.TestCase):
    def test_requires_dict(self) -> None:
        with self.assertRaises(TypeError):
            normalize_model("not a dict")

    def test_requires_non_empty_id(self) -> None:
        with self.assertRaises(ValueError):
            normalize_model({})
        with self.assertRaises(ValueError):
            normalize_model({"id": ""})

    def test_returns_normalized_fields(self) -> None:
        model = {
            "id": "groq/test",
            "active": True,
            "context_window": 1000,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        }
        result = normalize_model(model)
        self.assertEqual(result["id"], "groq/test")
        self.assertTrue(result["active"])
        self.assertEqual(result["context_window"], 1000)
        self.assertIsNotNone(result["raw_json"])
        self.assertIn("fetched_at", result)
        self.assertEqual(result["input_modalities"], ["text"])

    def test_optional_fields_default(self) -> None:
        model = {"id": "x/y"}
        result = normalize_model(model)
        self.assertIsNone(result["object"])
        self.assertIsNone(result["created"])
        self.assertIsNone(result["owned_by"])
        self.assertFalse(result["active"])
        self.assertIsNone(result["name"])
        self.assertEqual(result["pricing"], {})
        self.assertEqual(result["features"], [])
        self.assertEqual(result["sampling_parameters"], [])

    def test_pricing_non_dict(self) -> None:
        result = normalize_model({"id": "x/y", "pricing": "free"})
        self.assertEqual(result["pricing"], {"value": "free"})

    def test_preserves_raw_object(self) -> None:
        raw = {"id": "x/y", "extra": "field"}
        result = normalize_model(raw)
        self.assertEqual(result["raw_object"], raw)


class TestUpsertModel(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test_models.sqlite"
        self.conn = initialize_models_db(self.db_path)

    def tearDown(self) -> None:
        self.conn.close()

    def test_inserts_model(self) -> None:
        model = {
            "id": "test/model-1",
            "active": True,
            "context_window": 8192,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        }
        model_id = upsert_model(self.conn, model)
        self.assertEqual(model_id, "test/model-1")
        row = self.conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["active"], 1)

    def test_upsert_replaces(self) -> None:
        model = {"id": "test/model-1", "active": True, "input_modalities": ["text"], "output_modalities": ["text"]}
        upsert_model(self.conn, model)
        model2 = {"id": "test/model-1", "active": False, "input_modalities": ["image"], "output_modalities": ["image"]}
        upsert_model(self.conn, model2)
        row = self.conn.execute("SELECT active FROM models WHERE id = 'test/model-1'").fetchone()
        self.assertEqual(row["active"], 0)
        modalities = self.conn.execute(
            "SELECT modality FROM model_modalities WHERE model_id = 'test/model-1'"
        ).fetchall()
        self.assertEqual(len([d["modality"] for d in modalities]), 2)

    def test_child_tables_populated(self) -> None:
        model = {
            "id": "test/model-1",
            "active": True,
            "context_window": 8192,
            "max_completion_tokens": 4096,
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "pricing": {"prompt": "0.001", "completion": "0.002"},
            "supported_features": ["json_mode"],
            "supported_sampling_parameters": ["temperature", "top_p"],
        }
        upsert_model(self.conn, model)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_modalities WHERE model_id = 'test/model-1'").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_pricing WHERE model_id = 'test/model-1'").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_features WHERE model_id = 'test/model-1'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM model_sampling_parameters WHERE model_id = 'test/model-1'").fetchone()[0], 2)


class TestImportModelSnapshot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test_models.sqlite"
        self.snapshot_path = self.tmpdir / "snapshot.json"
        self.snapshot_path.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "test/model-1",
                            "object": "model",
                            "created": 1700000000,
                            "owned_by": "TestOrg",
                            "active": True,
                            "context_window": 8192,
                            "max_completion_tokens": 4096,
                            "name": "Test Model 1",
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                            "pricing": {"prompt": "0.001"},
                            "supported_features": ["json_mode", "tools"],
                            "supported_sampling_parameters": ["temperature", "top_p"],
                        },
                        {
                            "id": "test/model-2",
                            "object": "model",
                            "created": 1700000001,
                            "owned_by": "TestOrg",
                            "active": False,
                            "context_window": 4096,
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                            "pricing": {},
                            "supported_features": [],
                            "supported_sampling_parameters": [],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_imports_snapshot(self) -> None:
        conn = initialize_models_db(self.db_path)
        try:
            result = import_model_snapshot(self.snapshot_path, connection=conn)
            self.assertEqual(result["imported"], 2)
            self.assertEqual(len(result["warnings"]), 0)
            models = list_models(self.db_path, active_only=False)
            self.assertEqual(len(models), 2)
        finally:
            conn.close()

    def test_list_models_after_import(self) -> None:
        import_model_snapshot(self.snapshot_path, db_path=self.db_path)
        models = list_models(self.db_path, active_only=False)
        self.assertEqual(len(models), 2)
        ids = [m["id"] for m in models]
        self.assertIn("test/model-1", ids)
        self.assertIn("test/model-2", ids)

    def test_model_ids_filter(self) -> None:
        import_model_snapshot(self.snapshot_path, db_path=self.db_path)
        text_ids = model_ids(self.db_path, active_only=False)
        self.assertIn("test/model-1", text_ids)
        self.assertIn("test/model-2", text_ids)

    def test_active_only_filter(self) -> None:
        import_model_snapshot(self.snapshot_path, db_path=self.db_path)
        active = model_ids(self.db_path, active_only=True)
        self.assertIn("test/model-1", active)
        self.assertNotIn("test/model-2", active)

    def test_catalog_stats(self) -> None:
        import_model_snapshot(self.snapshot_path, db_path=self.db_path)
        stats = catalog_stats(self.db_path)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["active"], 1)

    def test_get_model(self) -> None:
        import_model_snapshot(self.snapshot_path, db_path=self.db_path)
        model = get_model(self.db_path, "test/model-1")
        self.assertIsNotNone(model)
        self.assertEqual(model["id"], "test/model-1")
        self.assertTrue(model["active"])
        self.assertEqual(model["pricing"], {"prompt": "0.001"})
        self.assertIn("json_mode", model["features"])

    def test_get_model_missing(self) -> None:
        initialize_models_db(self.db_path).close()
        self.assertIsNone(get_model(self.db_path, "nonexistent"))

    def test_import_skips_invalid(self) -> None:
        path = self.tmpdir / "bad.json"
        path.write_text(json.dumps({"data": [{"id": "valid"}, {"bad": "data"}]}), encoding="utf-8")
        conn = initialize_models_db(self.db_path)
        try:
            result = import_model_snapshot(path, connection=conn)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(len(result["warnings"]), 1)
        finally:
            conn.close()


class TestListModelsFilters(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test_models.sqlite"
        self.snapshot_path = self.tmpdir / "snapshot.json"
        self.snapshot_path.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "test/model-1",
                            "active": True,
                            "context_window": 8192,
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                            "pricing": {},
                            "supported_features": [],
                            "supported_sampling_parameters": [],
                        },
                        {
                            "id": "test/model-2",
                            "active": False,
                            "context_window": 4096,
                            "input_modalities": ["text"],
                            "output_modalities": ["text"],
                            "pricing": {},
                            "supported_features": [],
                            "supported_sampling_parameters": [],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        import_model_snapshot(self.snapshot_path, db_path=self.db_path)

    def test_active_only(self) -> None:
        active = list_models(self.db_path, active_only=True)
        self.assertTrue(all(m["active"] for m in active))

    def test_include_all(self) -> None:
        all_models = list_models(self.db_path, include_all=True, active_only=True)
        self.assertEqual(len(all_models), 2)

    def test_search(self) -> None:
        results = list_models(self.db_path, search="model-1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "test/model-1")

    def test_modality_filter(self) -> None:
        results = list_models(self.db_path, modality="text", active_only=False)
        self.assertEqual(len(results), 2)
