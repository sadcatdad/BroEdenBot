import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.app import app, validate_dashboard_config
from dashboard.db import bank_overview, import_history, vcxp_overview
from utils.settings import get_setting, initialize_settings_from_env, set_setting


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "admin",
                "DASHBOARD_PASSWORD": "test-password",
                "DASHBOARD_SECRET_KEY": "test-session-signing-key",
                "STAFF_AI_ALLOWED_ROLE_IDS": "11111111111111111",
                "MESSAGE_CONTEXT_ALLOWED_ROLE_IDS": "22222222222222222",
                "BOT_OWNER_USER_IDS": "33333333333333333",
                "VCXP_TRIGGER_ROLE_ID": "44444444444444444",
                "VCXP_EXCLUDED_ROLE_IDS": "55555555555555555",
                "VCXP_MINUTES_PER_PULSE": "30",
                "VCXP_ROLE_REMOVE_DELAY_SECONDS": "30",
                "DISCORD_TOKEN": "discord-super-secret-value",
                "GEMINI_API_KEY": "gemini-super-secret-value",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def login(self):
        login_page = self.client.get("/login")
        match = re.search(r'name="csrf" value="([^"]+)"', login_page.text)
        self.assertIsNotNone(match)
        response = self.client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf": match.group(1),
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_protected_page_redirects_to_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "http://testserver/login")

    def test_login_and_settings_do_not_expose_secrets(self):
        self.login()
        settings = self.client.get("/settings/permissions")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("STAFF_AI_ALLOWED_ROLE_IDS", settings.text)
        self.assertIn("MESSAGE_CONTEXT_ALLOWED_ROLE_IDS", settings.text)
        self.assertIn("BOT_OWNER_USER_IDS", settings.text)
        self.assertIn("11111111111111111", settings.text)
        self.assertIn("22222222222222222", settings.text)
        self.assertIn("33333333333333333", settings.text)
        self.assertNotIn("DISCORD_TOKEN", settings.text)
        self.assertNotIn("GEMINI_API_KEY", settings.text)
        self.assertNotIn("test-password", settings.text)
        self.assertNotIn("test-session-signing-key", settings.text)
        self.assertNotIn("discord-super-secret-value", settings.text)
        self.assertNotIn("gemini-super-secret-value", settings.text)
        overview = self.client.get("/")
        self.assertIn("VC XP Role-Pulse Readiness", overview.text)
        self.assertIn("44444444444444444", overview.text)
        self.assertNotIn("test-password", overview.text)
        self.assertNotIn("test-session-signing-key", overview.text)
        self.assertNotIn("discord-super-secret-value", overview.text)
        self.assertNotIn("gemini-super-secret-value", overview.text)

    def test_vcxp_trigger_role_uses_single_role_picker(self):
        self.login()
        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("VCXP_TRIGGER_ROLE_ID", settings.text)
        self.assertIn("<role-single-select", settings.text)

    def test_vcxp_excluded_roles_use_csv_role_picker(self):
        self.login()
        settings = self.client.get("/settings")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("VCXP_EXCLUDED_ROLE_IDS", settings.text)
        self.assertIn('value-format="csv"', settings.text)

    def test_unauthenticated_user_cannot_update_settings(self):
        response = self.client.post(
            "/settings/update",
            data={"key": "ASK_COOLDOWN_SECONDS", "value": "45", "csrf": "nope"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "30")

    def test_authenticated_user_can_update_allowed_setting(self):
        self.login()
        settings = self.client.get("/settings")
        match = re.search(r'name="csrf" value="([^"]+)"', settings.text)
        self.assertIsNotNone(match)
        response = self.client.post(
            "/settings/update",
            data={
                "key": "ASK_COOLDOWN_SECONDS",
                "value": "45",
                "csrf": match.group(1),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "45")

    def test_forbidden_setting_update_is_rejected(self):
        self.login()
        settings = self.client.get("/settings")
        token = re.search(r'name="csrf" value="([^"]+)"', settings.text).group(1)
        for key in (
            "DISCORD_TOKEN",
            "GEMINI_API_KEY",
            "DASHBOARD_PASSWORD",
            "DASHBOARD_SECRET_KEY",
            "CUSTOM_TOKEN_VALUE",
        ):
            response = self.client.post(
                "/settings/update",
                data={"key": key, "value": "do-not-store", "csrf": token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 400)

    def test_health_is_available_without_login(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class DashboardConfigurationTests(unittest.TestCase):
    def run_dashboard_import(self, environment):
        process_environment = os.environ.copy()
        process_environment.update(environment)
        process_environment["PYTHON_DOTENV_DISABLED"] = "1"
        return subprocess.run(
            [sys.executable, "-c", "import dashboard.app"],
            cwd=Path(__file__).resolve().parent.parent,
            env=process_environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_missing_secret_key_raises_when_enabled(self):
        environment = {
            "DASHBOARD_ENABLED": "true",
            "DASHBOARD_PASSWORD": "test-password",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DASHBOARD_SECRET_KEY is required"):
                validate_dashboard_config()
        process_environment = dict(environment)
        process_environment["DASHBOARD_SECRET_KEY"] = ""
        result = self.run_dashboard_import(process_environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DASHBOARD_SECRET_KEY is required", result.stderr)

    def test_missing_password_raises_when_enabled(self):
        environment = {
            "DASHBOARD_ENABLED": "true",
            "DASHBOARD_SECRET_KEY": "test-session-signing-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DASHBOARD_PASSWORD is required"):
                validate_dashboard_config()
        process_environment = dict(environment)
        process_environment["DASHBOARD_PASSWORD"] = ""
        result = self.run_dashboard_import(process_environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DASHBOARD_PASSWORD is required", result.stderr)


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

    def test_vcxp_overview_summarizes_role_and_pulse_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.db"
            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                initialize_settings_from_env()
                set_setting("VCXP_TRIGGER_ROLE_ID", "44444444444444444")
                set_setting("VCXP_ENABLED", "true")
                connection = sqlite3.connect(database)
                connection.execute(
                    """
                    CREATE TABLE dashboard_discord_roles (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        managed INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO dashboard_discord_roles (id, name, managed)
                    VALUES ('44444444444444444', 'pulse', 0)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE vc_xp_user_state (
                        guild_id INTEGER,
                        user_id INTEGER,
                        pulses_earned INTEGER,
                        pulses_paid INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vc_xp_user_state
                    VALUES (1, 10, 5, 3), (1, 11, 1, 1)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE vc_xp_pulses (
                        id INTEGER PRIMARY KEY,
                        status TEXT,
                        error TEXT,
                        granted_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vc_xp_pulses (status, error, granted_at)
                    VALUES ('paid', NULL, ?), ('pending', NULL, ?)
                    """,
                    ("2099-01-01T00:00:00+00:00", "2099-01-01T00:01:00+00:00"),
                )
                connection.commit()
                connection.close()

                result = vcxp_overview()

            self.assertEqual(result["status"], "Enabled")
            self.assertEqual(result["trigger_role_name"], "pulse")
            self.assertEqual(result["unpaid_users"], 1)
            self.assertEqual(result["unpaid_pulses"], 2)
            self.assertEqual(result["active_pulses"], 1)
            self.assertEqual(result["paid_24h"], 1)


if __name__ == "__main__":
    unittest.main()
