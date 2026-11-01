from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
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


if __name__ == "__main__":
    unittest.main()
