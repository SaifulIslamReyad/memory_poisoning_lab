"""Tests for core.tools (SQL safety) — no network or third-party packages required."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.tools import (
    QueryLimitError,
    UnsafeSQLError,
    execute_read_only_sql,
    execute_sql,
    prepare_sandbox,
    quote_identifier,
    register_read_only_tool,
)
from core.database import connect_sqlite, initialize_lab_db


class TestQuoteIdentifier(unittest.TestCase):
    def test_simple_name(self) -> None:
        self.assertEqual(quote_identifier("mytable"), '"mytable"')

    def test_reserved_word(self) -> None:
        self.assertEqual(quote_identifier("select"), '"select"')

    def test_name_with_quotes(self) -> None:
        self.assertEqual(quote_identifier('table"name'), '"table""name"')

    def test_null_rejected(self) -> None:
        with self.assertRaises(ValueError):
            quote_identifier(None)

    def test_empty_string(self) -> None:
        self.assertEqual(quote_identifier(""), '""')

    def test_null_byte_rejected(self) -> None:
        with self.assertRaises(ValueError):
            quote_identifier("table\x00name")


class TestExecuteReadOnlySQL(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        conn = connect_sqlite(self.db_path)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT, value REAL)")
        conn.execute("INSERT INTO test VALUES (1, 'alpha', 10.5)")
        conn.execute("INSERT INTO test VALUES (2, 'beta', 20.3)")
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO other VALUES (1, 'hello')")
        conn.commit()
        conn.close()
        self.tmpdir = Path(self.db_path).parent

    def test_select_works(self) -> None:
        rows = execute_read_only_sql(self.db_path, "SELECT * FROM test")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "alpha")

    def test_limit(self) -> None:
        rows = execute_read_only_sql(self.db_path, "SELECT * FROM test", max_rows=10)
        self.assertEqual(len(rows), 2)

    def test_limit_enforced(self) -> None:
        with self.assertRaises(QueryLimitError):
            execute_read_only_sql(self.db_path, "SELECT * FROM test", max_rows=1)

    def test_where_clause(self) -> None:
        rows = execute_read_only_sql(self.db_path, "SELECT * FROM test WHERE id = 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "alpha")

    def test_insert_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "INSERT INTO test (name, value) VALUES ('x', 1.0)")

    def test_update_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "UPDATE test SET value = 99")

    def test_delete_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "DELETE FROM test")

    def test_drop_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "DROP TABLE test")

    def test_multiple_statements_rejected(self) -> None:
        with self.assertRaises((UnsafeSQLError, sqlite3.ProgrammingError)):
            execute_read_only_sql(self.db_path, "SELECT 1; SELECT 2")

    def test_non_select_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "CREATE INDEX idx ON test(name)")

    def test_empty_sql_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "")

    def test_whitespace_only_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "   ")

    def test_invalid_sql_raises(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            execute_read_only_sql(self.db_path, "SELECT * FROM nonexistent_table")

    def test_alias_works(self) -> None:
        rows = execute_sql(self.db_path, "SELECT * FROM test LIMIT 1")
        self.assertEqual(len(rows), 1)

    def test_custom_limits(self) -> None:
        rows = execute_read_only_sql(self.db_path, "SELECT * FROM test", max_rows=10, max_result_bytes=1024 * 1024)
        self.assertEqual(len(rows), 2)

    def test_dangerous_function_rejected(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "SELECT load_extension('x')")

        with self.assertRaises(FileNotFoundError):
            execute_read_only_sql(Path(tempfile.mkdtemp()) / "does_not_exist.db", "SELECT 1")

    def test_query_limit_error(self) -> None:
        with self.assertRaises(ValueError):
            execute_read_only_sql(self.db_path, "SELECT * FROM test", max_rows=0)


class TestExecuteReadOnlySqlInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        conn = connect_sqlite(self.db_path)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT, value REAL)")
        conn.execute("INSERT INTO test VALUES (1, 'alpha', 10.5)")
        conn.execute("INSERT INTO test VALUES (2, 'beta', 20.3)")
        conn.commit()
        conn.close()

    def test_drop_with_semicolon(self) -> None:
        with self.assertRaises(UnsafeSQLError):
            execute_read_only_sql(self.db_path, "DROP TABLE test;")

    def test_or_1_equals_1(self) -> None:
        rows = execute_read_only_sql(self.db_path, "SELECT * FROM test WHERE id = 1 OR 1=1")
        self.assertEqual(len(rows), 2)


class TestPrepareSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def test_copies_fixture(self) -> None:
        fixture = self.tmpdir / "fixture.db"
        conn = sqlite3.connect(fixture)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'test')")
        conn.commit()
        conn.close()

        sandbox_dir = self.tmpdir / "sandbox"
        result = prepare_sandbox(fixture, sandbox_dir, "exp1")
        self.assertTrue(result.exists())

        check = sqlite3.connect(result)
        rows = check.execute("SELECT * FROM items").fetchall()
        check.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "test")

    def test_query_only_on_sandbox(self) -> None:
        fixture = self.tmpdir / "fixture.db"
        conn = sqlite3.connect(fixture)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        sandbox_dir = self.tmpdir / "sandbox"
        result = prepare_sandbox(fixture, sandbox_dir, "exp1")
        # Verify sandbox is a valid SQLite database
        check = sqlite3.connect(result)
        rows = check.execute("SELECT * FROM items").fetchall()
        check.close()
        self.assertEqual(len(rows), 0)


class TestRegisterReadOnlyTool(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        conn = connect_sqlite(self.db_path)
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT, value REAL)")
        conn.execute("INSERT INTO test VALUES (1, 'alpha', 10.5)")
        conn.commit()
        conn.close()

    def test_returns_callable(self) -> None:
        tool = register_read_only_tool(self.db_path)
        result = tool("SELECT * FROM test LIMIT 1")
        self.assertEqual(len(result), 1)

    def test_rejects_write(self) -> None:
        tool = register_read_only_tool(self.db_path)
        with self.assertRaises(UnsafeSQLError):
            tool("DELETE FROM test")
