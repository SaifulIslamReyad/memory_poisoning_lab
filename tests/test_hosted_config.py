from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from core.config import load_config
from core.database import connect_database
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
        for query in ("DELETE FROM users", "SELECT 1; SELECT 2", "SELECT * FROM public.users"):
            with self.assertRaises(UnsafeSQLError):
                _validate_postgres_query(query)


if __name__ == "__main__":
    unittest.main()
