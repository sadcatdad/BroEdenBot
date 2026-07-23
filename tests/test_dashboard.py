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
from dashboard.db import (
    ai_usage_overview,
    bank_overview,
    delete_failed_vcxp_pulses,
    import_history,
    vcxp_overview,
)
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
                "VC_XP_PULSE_MINUTES": "30",
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
        settings = self.client.get("/features/staff_tools")
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
        settings = self.client.get("/features/voice")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("VCXP_TRIGGER_ROLE_ID", settings.text)
        self.assertIn("<role-single-select", settings.text)

    def test_vcxp_exclusions_use_discord_pickers(self):
        self.login()
        settings = self.client.get("/features/voice")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("VCXP_EXCLUDED_ROLE_IDS", settings.text)
        self.assertIn('<role-multi-select input-name="setting__VCXP_EXCLUDED_ROLE_IDS" setting-key="VCXP_EXCLUDED_ROLE_IDS"', settings.text)
        self.assertIn("VCXP_EXCLUDED_VOICE_CHANNEL_IDS", settings.text)
        self.assertIn('<channel-multi-select input-name="setting__VCXP_EXCLUDED_VOICE_CHANNEL_IDS" setting-key="VCXP_EXCLUDED_VOICE_CHANNEL_IDS"', settings.text)

    def test_permission_roles_and_channels_use_discord_pickers(self):
        self.login()
        settings = self.client.get("/features/staff_tools")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("STAFF_AI_ALLOWED_ROLE_IDS", settings.text)
        self.assertIn('<role-multi-select input-name="setting__STAFF_AI_ALLOWED_ROLE_IDS" setting-key="STAFF_AI_ALLOWED_ROLE_IDS"', settings.text)
        voice = self.client.get("/features/voice")
        self.assertIn("EXCLUDED_VOICE_CHANNEL_IDS", voice.text)
        self.assertIn('<channel-multi-select input-name="setting__EXCLUDED_VOICE_CHANNEL_IDS" setting-key="EXCLUDED_VOICE_CHANNEL_IDS"', voice.text)
        bank = self.client.get("/features/bank")
        self.assertNotIn("BANK_LOG_CHANNEL_ID", bank.text)

    def test_reminder_command_permissions_use_role_pickers(self):
        self.login()
        settings = self.client.get("/features/reminders")
        self.assertEqual(settings.status_code, 200)
        for key in (
            "REMINDER_PERSONAL_ALLOWED_ROLE_IDS",
            "REMINDER_EVENT_ALLOWED_ROLE_IDS",
            "REMINDER_MANAGE_ALLOWED_ROLE_IDS",
            "REMINDER_MANAGE_ALL_ROLE_IDS",
            "REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS",
        ):
            self.assertIn(
                f'<role-multi-select input-name="setting__{key}" setting-key="{key}"',
                settings.text,
            )

    def test_discord_settings_omits_duplicate_runtime_settings(self):
        self.login()
        settings = self.client.get("/settings/discord")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("Live guild catalog", settings.text)
        self.assertNotIn("bank_allowed_role_ids", settings.text)
        self.assertNotIn("bank_log_channel_id", settings.text)
        self.assertNotIn("ask_command_allowed_channel_ids", settings.text)

    def test_advanced_settings_do_not_duplicate_normal_sections(self):
        self.login()
        settings = self.client.get("/settings/advanced")
        self.assertEqual(settings.status_code, 200)
        self.assertIn("import_archive_path", settings.text)
        self.assertNotIn("STAFF_AI_ALLOWED_ROLE_IDS", settings.text)
        self.assertNotIn("VCXP_TRIGGER_ROLE_ID", settings.text)

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

    def test_admin_can_clear_only_failed_vcxp_pulse_records(self):
        now = "2099-01-01T00:00:00+00:00"
        set_setting("VCXP_ENABLED", "true")
        connection = sqlite3.connect(self.database)
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
        connection.executemany(
            """
            INSERT INTO vc_xp_pulses (status, error, granted_at)
            VALUES (?, ?, ?)
            """,
            [
                ("added", None, now),
                ("add_failed", "ClientConnectorDNSError", now),
                ("add_failed", "TimeoutError", now),
            ],
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
        connection.commit()
        connection.close()

        self.login()
        overview = self.client.get("/")
        token = re.search(
            r'name="csrf" value="([^"]+)"', overview.text
        ).group(1)
        self.assertIn("Degraded", overview.text)
        self.assertIn("Clear failed XP pulses", overview.text)
        response = self.client.post(
            "/vcxp/failed/clear",
            data={"csrf": token},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        connection = sqlite3.connect(self.database)
        rows = connection.execute(
            "SELECT status FROM vc_xp_pulses ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual(rows, [("added",)])
        refreshed = self.client.get("/")
        self.assertIn("Cleared 2 failed VC XP pulse records", refreshed.text)
        self.assertNotIn("Degraded", refreshed.text)

    def test_clear_failed_vcxp_pulses_rejects_invalid_csrf(self):
        connection = sqlite3.connect(self.database)
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
            VALUES ('add_failed', 'TimeoutError', '2099-01-01T00:00:00+00:00')
            """
        )
        connection.commit()
        connection.close()
        self.login()

        response = self.client.post(
            "/vcxp/failed/clear",
            data={"csrf": "invalid"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        with sqlite3.connect(self.database) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM vc_xp_pulses WHERE status = 'add_failed'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

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
        self.assertIn("ai", response.json())

    def test_ai_dashboard_hides_secrets_and_handles_empty_logs(self):
        self.login()
        response = self.client.get("/ai")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI Framework", response.text)
        self.assertIn("AI settings are currently managed through .env.", response.text)
        self.assertIn("No AI usage logs found.", response.text)
        self.assertIn("No /ask feedback has been recorded yet.", response.text)
        self.assertNotIn("gemini-super-secret-value", response.text)

    def test_ai_dashboard_shows_ask_feedback(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE ask_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT,
                user_id TEXT,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                feedback TEXT,
                feedback_at TEXT,
                kb_sources_json TEXT NOT NULL DEFAULT '[]',
                model_used TEXT,
                tier_used TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ask_feedback (
                created_at, updated_at, user_id, question, answer, feedback,
                feedback_at, kb_sources_json, model_used, tier_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2099-01-01T00:00:00",
                "2099-01-01T00:00:00",
                "123",
                "where is server info?",
                "Use the guide channel.",
                "confused",
                "2099-01-01T00:01:00",
                '[{"source_name": "Public Channel Index", "section_title": "Guides"}]',
                "gemini-2.5-flash",
                "default",
            ),
        )
        connection.commit()
        connection.close()

        self.login()
        response = self.client.get("/ai")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Recent /ask Feedback", response.text)
        self.assertIn("where is server info?", response.text)
        self.assertIn("Public Channel Index", response.text)
        self.assertIn("confused", response.text)

    def test_ai_dashboard_can_be_hidden_by_env(self):
        self.login()
        with patch.dict(os.environ, {"AI_DASHBOARD_VISIBLE": "false"}, clear=False):
            response = self.client.get("/ai")
            overview = self.client.get("/")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(">AI</a>", overview.text)

    def test_ai_kb_dashboard_create_search_edit_and_delete(self):
        self.login()
        page = self.client.get("/ai/kb")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Connected Knowledge Sources", page.text)
        self.assertIn("Manage Knowledge", page.text)
        edit_page = self.client.get("/ai/kb/new")
        self.assertEqual(edit_page.status_code, 200)
        token = re.search(r'name="csrf" value="([^"]+)"', edit_page.text).group(1)

        response = self.client.post(
            "/ai/kb/save",
            data={
                "csrf": token,
                "source_name": "server-faq",
                "source_type": "faq",
                "source_visibility": "public",
                "ai_enabled": "1",
                "raw_content": "# FAQ\n\nTickets go in the support channel.",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        search = self.client.get("/ai/kb?query=support&visibility=public")
        self.assertIn("server-faq", search.text)
        knowledge = self.client.get("/knowledge")
        self.assertIn("server-faq", knowledge.text)
        self.assertIn("Enabled", knowledge.text)

        edit = self.client.get("/ai/kb/server-faq/edit")
        self.assertEqual(edit.status_code, 200)
        self.assertIn("Tickets go in the support channel.", edit.text)
        token = re.search(r'name="csrf" value="([^"]+)"', edit.text).group(1)
        response = self.client.post(
            "/ai/kb/save",
            data={
                "csrf": token,
                "source_name": "server-faq",
                "source_type": "faq",
                "source_visibility": "public",
                "ai_enabled": "1",
                "raw_content": "# FAQ\n\nUpdated answer about support tickets.",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("Updated answer", self.client.get("/ai/kb/server-faq/edit").text)

        token = re.search(
            r'name="csrf" value="([^"]+)"',
            self.client.get("/ai/kb").text,
        ).group(1)
        response = self.client.post(
            "/ai/kb/server-faq/delete",
            data={"csrf": token, "confirm": "server-faq"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        deleted_page = self.client.get("/knowledge").text
        self.assertNotIn("server-faq", deleted_page)

    def test_unauthenticated_user_cannot_access_ai_kb_editor(self):
        response = self.client.get("/ai/kb", follow_redirects=False)
        self.assertEqual(response.status_code, 303)

    def test_knowledge_dashboard_manages_live_discord_source(self):
        self.login()
        page = self.client.get("/knowledge")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Add or Update Channel / Forum Post", page.text)
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

        response = self.client.post(
            "/knowledge/live/save",
            data={
                "csrf": token,
                "guild_id": "123456789012345678",
                "manual_channel_id": "987654321098765432",
                "channel_name": "Survival Guide Post",
                "source_type": "survival_guide",
                "visibility": "public",
                "sync_mode": "live",
                "enabled": "1",
                "ai_enabled": "1",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        knowledge = self.client.get("/knowledge")
        self.assertIn("Survival Guide Post", knowledge.text)
        self.assertIn("987654321098765432", knowledge.text)

        token = re.search(r'name="csrf" value="([^"]+)"', knowledge.text).group(1)
        response = self.client.post(
            "/knowledge/live/123456789012345678/987654321098765432/sync",
            data={"csrf": token, "limit": "50"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        connection = sqlite3.connect(self.database)
        row = connection.execute(
            """
            SELECT action_type, payload_json, status
            FROM dashboard_actions
            WHERE action_type = 'sync_knowledge_source'
            """
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "sync_knowledge_source")
        self.assertIn("987654321098765432", row[1])
        self.assertEqual(row[2], "pending")


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

    def test_invalid_public_url_raises_when_enabled(self):
        environment = {
            "DASHBOARD_ENABLED": "true",
            "DASHBOARD_PASSWORD": "test-password",
            "DASHBOARD_SECRET_KEY": "test-session-signing-key",
            "DASHBOARD_PUBLIC_URL": "https://garden.broeden.com/member",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DASHBOARD_PUBLIC_URL"):
                validate_dashboard_config()


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

    def test_ai_usage_overview_summarizes_usage_logs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE ai_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    guild_id TEXT,
                    channel_id TEXT,
                    user_id TEXT,
                    source_command TEXT,
                    task_type TEXT,
                    requested_tier TEXT,
                    tier_used TEXT,
                    model_used TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0,
                    usage_was_estimated INTEGER DEFAULT 0,
                    success INTEGER DEFAULT 1,
                    blocked_by_budget INTEGER DEFAULT 0,
                    error_message TEXT
                )
                """
            )
            now_prefix = "2099-01-01T00:00:00"
            connection.execute(
                """
                INSERT INTO ai_usage_logs (
                    created_at, user_id, source_command, task_type, requested_tier,
                    tier_used, model_used, input_tokens, output_tokens,
                    total_tokens, estimated_cost_usd, success, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_prefix,
                    "123",
                    "/ai test",
                    "framework_test",
                    "default",
                    "default",
                    "gemini-2.5-flash",
                    10,
                    5,
                    15,
                    0.0001,
                    1,
                    None,
                ),
            )
            connection.commit()
            connection.close()

            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                result = ai_usage_overview(command="/ai test")

            self.assertTrue(result["tables_found"])
            self.assertEqual(result["recent_logs"][0]["source_command"], "/ai test")
            self.assertEqual(result["recent_logs"][0]["user_id"], "123")

    def test_ai_usage_overview_reads_ask_feedback_without_usage_logs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE ask_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    guild_id TEXT,
                    channel_id TEXT,
                    user_id TEXT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    feedback TEXT,
                    feedback_at TEXT,
                    kb_sources_json TEXT NOT NULL DEFAULT '[]',
                    model_used TEXT,
                    tier_used TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO ask_feedback (
                    created_at, updated_at, user_id, question, answer, feedback,
                    feedback_at, kb_sources_json, model_used, tier_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2099-01-01T00:00:00",
                    "2099-01-01T00:00:00",
                    "123",
                    "where is support?",
                    "Open a ticket.",
                    "helped",
                    "2099-01-01T00:01:00",
                    '[{"source_name": "FAQ"}]',
                    "gemini-2.5-flash",
                    "default",
                ),
            )
            connection.commit()
            connection.close()

            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                result = ai_usage_overview()

            self.assertFalse(result["tables_found"])
            self.assertTrue(result["ask_feedback"]["tables_found"])
            self.assertEqual(result["ask_feedback"]["helped"], 1)
            self.assertEqual(
                result["ask_feedback"]["recent"][0]["kb_sources"][0]["source_name"],
                "FAQ",
            )

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
                    VALUES ('added', NULL, ?), ('add_failed', 'Forbidden', ?)
                    """,
                    ("2099-01-01T00:00:00+00:00", "2099-01-01T00:01:00+00:00"),
                )
                connection.commit()
                connection.close()

                result = vcxp_overview()

            self.assertEqual(result["status"], "Degraded")
            self.assertEqual(result["trigger_role_name"], "pulse")
            self.assertEqual(result["unpaid_users"], 0)
            self.assertEqual(result["unpaid_pulses"], 0)
            self.assertEqual(result["active_pulses"], 1)
            self.assertEqual(result["paid_24h"], 1)
            self.assertEqual(result["failed_24h"], 1)
            self.assertTrue(
                any("role adds failed" in issue for issue in result["issues"])
            )

    def test_delete_failed_vcxp_pulses_preserves_successes_and_accounting(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE vc_xp_pulses (id INTEGER PRIMARY KEY, status TEXT)"
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
            connection.executemany(
                "INSERT INTO vc_xp_pulses (status) VALUES (?)",
                [("added",), ("add_failed",), ("add_failed",)],
            )
            connection.execute(
                "INSERT INTO vc_xp_user_state VALUES (1, 10, 5, 4)"
            )
            connection.commit()
            connection.close()

            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                deleted = delete_failed_vcxp_pulses()

            connection = sqlite3.connect(database)
            statuses = connection.execute(
                "SELECT status FROM vc_xp_pulses ORDER BY id"
            ).fetchall()
            state = connection.execute(
                "SELECT pulses_earned, pulses_paid FROM vc_xp_user_state"
            ).fetchone()
            connection.close()
            self.assertEqual(deleted, 2)
            self.assertEqual(statuses, [("added",)])
            self.assertEqual(state, (5, 4))

    def test_vcxp_overview_ignores_stale_unpaid_state_before_reward_start(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "data.db"
            with patch.dict(os.environ, {"DATABASE_PATH": str(database)}):
                initialize_settings_from_env()
                set_setting(
                    "VCXP_REWARD_START_AT",
                    "2026-06-26T19:28:32+00:00",
                )
                connection = sqlite3.connect(database)
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
                    VALUES (1, 10, 120, 0), (1, 11, 80, 10)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE vc_sessions (
                        guild_id INTEGER,
                        user_id INTEGER,
                        left_at TEXT,
                        counted_seconds INTEGER,
                        reward_eligible INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO vc_sessions
                    VALUES (1, 10, '2026-06-25T12:00:00+00:00', 3600, 1)
                    """
                )
                connection.commit()
                connection.close()

                result = vcxp_overview()

            self.assertEqual(result["unpaid_users"], 0)
            self.assertEqual(result["unpaid_pulses"], 0)


if __name__ == "__main__":
    unittest.main()
