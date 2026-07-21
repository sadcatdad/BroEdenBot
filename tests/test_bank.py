import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DISCORD_TOKEN", "test-token")

from cogs.bank import PROJECT_ROOT, bank_database_path


class BankDatabasePathTests(unittest.TestCase):
    def test_default_database_stays_in_project_root(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BANK_DATABASE_PATH", None)
            self.assertEqual(bank_database_path(), (PROJECT_ROOT / "brobank.db").resolve())

    def test_absolute_database_path_supports_persistent_volumes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "nested" / "brobank.db"
            with patch.dict(
                os.environ,
                {"BANK_DATABASE_PATH": str(database)},
                clear=False,
            ):
                self.assertEqual(bank_database_path(), database.resolve())

    def test_relative_database_path_is_resolved_from_project_root(self):
        with patch.dict(
            os.environ,
            {"BANK_DATABASE_PATH": "runtime/brobank.db"},
            clear=False,
        ):
            self.assertEqual(
                bank_database_path(),
                (PROJECT_ROOT / "runtime/brobank.db").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
