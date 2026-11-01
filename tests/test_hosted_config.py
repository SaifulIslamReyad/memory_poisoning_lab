from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from core.config import load_config
from core.database import connect_database, _postgres_row_factory, _PostgresRow
from core.tools import UnsafeSQLError, _validate_postgres_query


class TestHostedConfiguration(unittest.TestCase):
    def test_database_url_is_required(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.database_url, "")
        with self.assertRaises(ValueError):
            connect_database("")

    def test_postgres_queries_are_restricted(self) -> None:
        self.assertEqual(_validate_postgres_query("SELECT 1;"), "SELECT 1")
        for query in (
            "DELETE FROM users",
            "SELECT 1; SELECT 2",
            "SELECT * FROM public.users",
        ):
            with self.assertRaises(UnsafeSQLError):
                _validate_postgres_query(query)


class TestRowFactoryContract(unittest.TestCase):
    """Locks the psycopg RowFactory -> RowMaker two-step contract.

    The old single-function signature ``_postgres_row(cursor, values)`` is
    incompatible with psycopg >= 3.2 and surfaced as
    ``TypeError: _postgres_row() missing 1 required positional argument: 'values'``
    on the first query. These tests fail if the factory regresses.
    """

    def _fake_cursor(self, names: list[str] | None) -> MagicMock:
        from psycopg import pq

        cursor = MagicMock()
        if names is None:
            cursor.pgresult = None
        else:
            pgresult = MagicMock()
            pgresult.nfields = len(names)
            pgresult.status = pq.ExecStatus.TUPLES_OK
            pgresult.fname.side_effect = [n.encode("utf-8") for n in names]
            cursor.pgresult = pgresult
        cursor._encoding = "utf-8"
        cursor.description = (
            [SimpleNamespace(name=n) for n in names]
            if names is not None
            else None
        )
        return cursor

    def test_factory_returns_row_maker_for_select(self) -> None:
        cursor = self._fake_cursor(["id", "name"])
        maker = _postgres_row_factory(cursor)
        self.assertTrue(callable(maker))
        row = maker((1, "alpha"))
        self.assertIsInstance(row, _PostgresRow)
        self.assertEqual(row["id"], 1)
        self.assertEqual(row["name"], "alpha")
        # Legacy numeric indexing still works
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "alpha")

    def test_factory_returns_no_result_maker_for_non_select(self) -> None:
        from psycopg.rows import no_result
        from psycopg import errors as psycopg_errors

        cursor = self._fake_cursor(None)
        maker = _postgres_row_factory(cursor)
        self.assertIs(maker, no_result)
        with self.assertRaises(psycopg_errors.InterfaceError):
            maker(())


class TestExecutemanyOnCursor(unittest.TestCase):
    """psycopg 3 has no Connection.executemany; the wrapper must use a cursor."""

    def test_executemany_uses_cursor(self) -> None:
        from core.database import PostgresConnection

        fake_cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        fake_conn.cursor.return_value.__exit__.return_value = False
        with patch("psycopg.connect", return_value=fake_conn):
            connection = PostgresConnection.__new__(PostgresConnection)
            connection._connection = fake_conn
            connection.executemany(
                "INSERT INTO t(a, b) VALUES (?, ?)", [(1, 2), (3, 4)]
            )
        fake_cursor.executemany.assert_called_once()
        sql, params = fake_cursor.executemany.call_args.args
        self.assertEqual(sql, "INSERT INTO t(a, b) VALUES (%s, %s)")
        self.assertEqual(params, [(1, 2), (3, 4)])


class TestSchemaSelfRepair(unittest.TestCase):
    """A version-stamped schema whose tables are actually missing must re-apply.

    The old code trusted the ``schema_migrations`` version row alone, so a
    partial prior run (e.g. ``executescript`` silently skipping a statement)
    left the version stamped while child tables such as ``model_pricing`` were
    absent, surfacing as ``UndefinedTable`` on the first enriching query.
    """

    def _fake_connection(self, existing_tables: set[str]) -> MagicMock:
        connection = MagicMock()
        stamped_version = (1,)

        def execute(sql: str, params: Any = (), **kwargs: Any) -> MagicMock:
            normalized = sql.replace("\n", " ").upper()
            if "SELECT VERSION" in normalized and "SCHEMA_MIGRATIONS" in normalized:
                return MagicMock(fetchone=MagicMock(return_value=stamped_version))
            if "INFORMATION_SCHEMA.TABLES" in normalized:
                return MagicMock(
                    fetchall=MagicMock(
                        return_value=[(t,) for t in sorted(existing_tables)]
                    )
                )
            return MagicMock(fetchone=MagicMock(return_value=None),
                            fetchall=MagicMock(return_value=[]))

        connection.execute.side_effect = execute
        return connection

    def test_reapplies_schema_when_tables_are_missing(self) -> None:
        from core.database import _apply_schema, MODEL_SCHEMA

        # Only the parent `models` table exists; child tables are gone.
        connection = self._fake_connection({"models"})
        _apply_schema(connection, MODEL_SCHEMA, "models", 1, {})
        # executescript must have been called to (re)create the missing tables.
        connection.executescript.assert_called_once_with(MODEL_SCHEMA)

    def test_skips_apply_when_all_tables_exist(self) -> None:
        from core.database import _apply_schema, MODEL_SCHEMA

        connection = self._fake_connection(
            {"models", "model_features", "model_modalities", "model_pricing",
             "model_sampling_parameters"}
        )
        _apply_schema(connection, MODEL_SCHEMA, "models", 1, {})
        connection.executescript.assert_not_called()


if __name__ == "__main__":
    unittest.main()
