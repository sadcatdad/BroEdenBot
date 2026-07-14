import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from main import BotClient


class FeatureExtensionLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_transferred_extensions_load_together_in_runtime_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "data.db")
            with patch.dict(os.environ, {"DATABASE_PATH": database_path}):
                bot = BotClient()
                bot.wait_until_ready = AsyncMock()
                await bot.load_data()
                extensions = (
                    "cogs.disboard_bumps",
                    "cogs.leaderboards",
                    "cogs.reminder",
                    "cogs.stats",
                    "cogs.streaks",
                )
                try:
                    for extension in extensions:
                        await bot.load_extension(extension)
                    self.assertTrue(set(extensions) <= set(bot.extensions))
                    self.assertIsNotNone(bot.get_cog("DisboardBumps"))
                    self.assertIsNotNone(bot.get_cog("Leaderboards"))
                    self.assertIsNotNone(bot.get_cog("ReminderCog"))
                    self.assertIsNotNone(bot.get_cog("Stats"))
                    self.assertIsNotNone(bot.get_cog("Streaks"))
                finally:
                    for extension in reversed(extensions):
                        if extension in bot.extensions:
                            await bot.unload_extension(extension)
                    await bot.close()


if __name__ == "__main__":
    unittest.main()
