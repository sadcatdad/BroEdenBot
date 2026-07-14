import os
import unittest
from unittest.mock import patch

from main import cog_is_enabled, configured_modules


class ModuleLoaderTests(unittest.TestCase):
    def test_empty_configuration_preserves_legacy_load_all_behavior(self):
        with patch.dict(os.environ, {"ENABLED_MODULES": ""}):
            self.assertIsNone(configured_modules())
        self.assertTrue(cog_is_enabled("streaks.py", None))

    def test_selected_feature_modules_are_mapped(self):
        with patch.dict(
            os.environ,
            {"ENABLED_MODULES": "bumps, reminders streaks,stats"},
        ):
            enabled = configured_modules()
        self.assertTrue(cog_is_enabled("disboard_bumps.py", enabled))
        self.assertTrue(cog_is_enabled("reminder.py", enabled))
        self.assertTrue(cog_is_enabled("streaks.py", enabled))
        self.assertTrue(cog_is_enabled("leaderboards.py", enabled))
        self.assertFalse(cog_is_enabled("poll.py", enabled))

    def test_unmapped_destination_cogs_remain_enabled(self):
        self.assertTrue(cog_is_enabled("bot_admin.py", {"stats"}))


if __name__ == "__main__":
    unittest.main()
