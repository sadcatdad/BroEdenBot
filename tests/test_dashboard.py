import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.db import bank_overview, import_history


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
            },
            clear=False,
        )
        self.environment.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()

    def test_protected_page_redirects_to_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "http://testserver/login")

    def test_login_and_settings_do_not_expose_secrets(self):
        login_page = self.client.get("/login")
        csrf = login_page.cookies.get("broeden_dashboard_session")
        self.assertIsNotNone(csrf)

        match = re.search(r'name="csrf" value="([^"]+)"', login_page.text)
        self.assertIsNotNone(match)
        token = match.group(1)
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf": token,
            },
        )
        self.assertEqual(response.status_code, 200)
        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertNotIn("DISCORD_TOKEN", settings.text)
        self.assertNotIn("GEMINI_API_KEY", settings.text)

    def test_health_is_available_without_login(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class DashboardDatabaseTests(unittest.TestCase):
    def test_bank_overview_reads_existing_ledger(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "bank.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE bank_transactions (
                    id INTEGER PRIMARY KEY,
                    type TEXT,
                    discord_user_id INTEGER,
                    display_name TEXT,
                    amount REAL,
                    note TEXT,
                    is_public INTEGER,
                    created_at TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO bank_transactions
                VALUES (1, 'contribution', 1, 'Donor', 25, 'Gift', 1, '2026-06-22')
                """
            )
            connection.commit()
            connection.close()

            with patch.dict(os.environ, {"BANK_DATABASE_PATH": str(database)}):
                result = bank_overview()

            self.assertTrue(result["tables_found"])
            self.assertEqual(result["totals"]["balance"], 25)
            self.assertEqual(result["donors"][0]["donor"], "Donor")

    def test_import_history_handles_missing_table(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
            connection.close()

            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                result = import_history()

            self.assertFalse(result["tables_found"])
            self.assertEqual(result["imports"], [])


if __name__ == "__main__":
    unittest.main()
