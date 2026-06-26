import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.settings import (
    get_bool_setting,
    get_csv_ids_setting,
    get_int_setting,
    get_setting,
    initialize_settings_from_env,
    normalize_setting_value,
    set_setting,
)


class SettingsDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database)},
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_initialization_creates_tables_and_seeds_environment(self):
        with patch.dict(
            os.environ,
            {
                "ASK_COOLDOWN_SECONDS": "40",
                "BANK_ALLOWED_ROLE_IDS": "12345678901234567",
            },
        ):
            initialize_settings_from_env()

        connection = sqlite3.connect(self.database)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        rows = dict(connection.execute("SELECT key, value FROM bot_settings"))
        connection.close()
        self.assertIn("bot_settings", tables)
        self.assertIn("bot_settings_audit", tables)
        self.assertEqual(rows["ASK_COOLDOWN_SECONDS"], "40")
        self.assertEqual(rows["BANK_ALLOWED_ROLE_IDS"], "12345678901234567")

    def test_initialization_never_seeds_secrets(self):
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "discord-secret",
                "GEMINI_API_KEY": "gemini-secret",
                "DASHBOARD_PASSWORD": "dashboard-password",
                "DASHBOARD_SECRET_KEY": "dashboard-secret",
            },
        ):
            initialize_settings_from_env()
        connection = sqlite3.connect(self.database)
        keys = {
            row[0] for row in connection.execute("SELECT key FROM bot_settings")
        }
        connection.close()
        self.assertTrue(
            keys.isdisjoint(
                {
                    "DISCORD_TOKEN",
                    "GEMINI_API_KEY",
                    "DASHBOARD_PASSWORD",
                    "DASHBOARD_SECRET_KEY",
                }
            )
        )

    def test_existing_database_value_is_not_overwritten_by_environment(self):
        with patch.dict(os.environ, {"ASK_COOLDOWN_SECONDS": "40"}):
            initialize_settings_from_env()
            set_setting("ASK_COOLDOWN_SECONDS", "55")
        with patch.dict(os.environ, {"ASK_COOLDOWN_SECONDS": "10"}):
            initialize_settings_from_env()
        self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "55")

    def test_database_wins_and_environment_is_fallback(self):
        with patch.dict(os.environ, {"ASK_COOLDOWN_SECONDS": "40"}):
            initialize_settings_from_env()
            self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "40")
            set_setting("ASK_COOLDOWN_SECONDS", "50")
            self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "50")
            connection = sqlite3.connect(self.database)
            connection.execute(
                "DELETE FROM bot_settings WHERE key = 'ASK_COOLDOWN_SECONDS'"
            )
            connection.commit()
            connection.close()
            self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "40")
        with patch.dict(os.environ, {"MODAI_MODEL": "env-model"}):
            self.assertEqual(get_setting("MODAI_MODEL"), "env-model")

    def test_typed_getters(self):
        initialize_settings_from_env()
        set_setting("VCXP_ENABLED", "true")
        set_setting("VCXP_MINUTES_PER_PULSE", "35")
        set_setting("VCXP_EXCLUDED_ROLE_IDS", "34567890123456789")
        set_setting(
            "VCSTATS_ALLOWED_ROLE_IDS",
            "12345678901234567,23456789012345678",
        )
        self.assertTrue(get_bool_setting("VCXP_ENABLED"))
        self.assertEqual(get_int_setting("VCXP_MINUTES_PER_PULSE"), 35)
        self.assertEqual(
            get_csv_ids_setting("VCSTATS_ALLOWED_ROLE_IDS"),
            [12345678901234567, 23456789012345678],
        )
        self.assertEqual(
            get_csv_ids_setting("VCXP_EXCLUDED_ROLE_IDS"),
            [34567890123456789],
        )

    def test_audit_skips_unchanged_values(self):
        initialize_settings_from_env()
        set_setting("ASK_COOLDOWN_SECONDS", "45", changed_by="admin")
        set_setting("ASK_COOLDOWN_SECONDS", "45", changed_by="admin")
        connection = sqlite3.connect(self.database)
        count = connection.execute(
            "SELECT COUNT(*) FROM bot_settings_audit"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)


class SettingsValidationTests(unittest.TestCase):
    def test_integer_validation(self):
        with self.assertRaisesRegex(ValueError, "integer"):
            normalize_setting_value("ASK_COOLDOWN_SECONDS", "nope")
        with self.assertRaisesRegex(ValueError, "at least 1"):
            normalize_setting_value("VCXP_MINUTES_PER_PULSE", "0")
        with self.assertRaisesRegex(ValueError, "at least 0"):
            normalize_setting_value("VCXP_DAILY_PULSE_CAP", "-1")

    def test_boolean_validation(self):
        self.assertEqual(normalize_setting_value("VCXP_ENABLED", "TRUE"), "true")
        self.assertEqual(normalize_setting_value("VCXP_ENABLED", "false"), "false")
        with self.assertRaisesRegex(ValueError, "true or false"):
            normalize_setting_value("VCXP_ENABLED", "yes")

    def test_csv_ids_normalize_and_allow_blank(self):
        self.assertEqual(
            normalize_setting_value(
                "BANK_ALLOWED_ROLE_IDS",
                "12345678901234567, 23456789012345678",
            ),
            "12345678901234567,23456789012345678",
        )
        self.assertEqual(normalize_setting_value("BANK_ALLOWED_ROLE_IDS", ""), "")

    def test_csv_ids_reject_invalid_values(self):
        for value in ("abc", "123", "12345678901234567,not-an-id"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "17 to 20 digits"):
                    normalize_setting_value("BANK_ALLOWED_ROLE_IDS", value)

    def test_vcxp_trigger_role_requires_one_role_id(self):
        self.assertEqual(
            normalize_setting_value("VCXP_TRIGGER_ROLE_ID", "12345678901234567"),
            "12345678901234567",
        )
        with self.assertRaisesRegex(ValueError, "one Discord role ID"):
            normalize_setting_value(
                "VCXP_TRIGGER_ROLE_ID",
                "12345678901234567,23456789012345678",
            )

    def test_forbidden_and_unknown_keys_are_rejected(self):
        for key in (
            "DISCORD_TOKEN",
            "GEMINI_API_KEY",
            "DASHBOARD_PASSWORD",
            "DASHBOARD_SECRET_KEY",
            "SOMETHING_SECRET",
            "UNKNOWN_SAFE_KEY",
        ):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "not editable"):
                    normalize_setting_value(key, "value")


if __name__ == "__main__":
    unittest.main()
