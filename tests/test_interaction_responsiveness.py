import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.bank import Bank
from cogs.leaderboards import Leaderboards
from cogs.streaks import Streaks
from cogs.rulecard import RulecardDraftView
from utils.settings import (
    clear_runtime_settings,
    get_setting,
    initialize_settings_from_env,
    refresh_runtime_settings,
    set_setting,
)


class RuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "data.db"
        self.environment = patch.dict(os.environ, {"DATABASE_PATH": str(self.database)})
        self.environment.start()
        initialize_settings_from_env()
        set_setting("ASK_COOLDOWN_SECONDS", "42")
        await refresh_runtime_settings()

    async def asyncTearDown(self):
        clear_runtime_settings()
        self.environment.stop()
        self.directory.cleanup()

    async def test_locked_database_does_not_block_runtime_reads_or_refresh_loop(self):
        # Rollback-journal mode deliberately models the strongest read lock.
        writer = sqlite3.connect(self.database)
        writer.execute("PRAGMA journal_mode=DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            with patch("utils.settings._connect", side_effect=AssertionError("runtime DB read")):
                for _ in range(1000):
                    self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "42")
            refresh = asyncio.create_task(refresh_runtime_settings())
            await asyncio.sleep(0.05)
            self.assertFalse(refresh.done(), "refresh should wait in a worker, not block this loop")
            writer.rollback()
            await asyncio.wait_for(refresh, timeout=2)
        finally:
            writer.close()

    async def test_failed_refresh_retains_database_permission_overrides(self):
        set_setting("BOT_OWNER_USER_IDS", "12345678901234567")
        with patch("utils.settings.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            with self.assertRaises(sqlite3.OperationalError):
                await refresh_runtime_settings()
            self.assertEqual(get_setting("BOT_OWNER_USER_IDS"), "12345678901234567")

    async def test_external_changes_and_deleted_overrides_are_refreshed(self):
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE bot_settings SET value='77' WHERE key='ASK_COOLDOWN_SECONDS'")
        self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "42")
        await refresh_runtime_settings()
        self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "77")
        with sqlite3.connect(self.database) as db:
            db.execute("DELETE FROM bot_settings WHERE key='ASK_COOLDOWN_SECONDS'")
        with patch.dict(os.environ, {"ASK_COOLDOWN_SECONDS": "15"}):
            await refresh_runtime_settings()
            self.assertEqual(get_setting("ASK_COOLDOWN_SECONDS"), "15")


class InteractionAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=42),
        )

    async def assert_acknowledged_before_work(self, interaction, work, invoke):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_result(*args, **kwargs):
            started.set()
            await release.wait()
            return discord.Embed(title="Result")

        with patch.object(work[0], work[1], side_effect=delayed_result):
            task = asyncio.create_task(invoke())
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                interaction.response.defer.assert_awaited_once()
                interaction.followup.send.assert_not_awaited()
                release.set()
                await task
                interaction.followup.send.assert_awaited_once()
                interaction.response.send_message.assert_not_awaited()
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_points_command_acknowledges_before_database_lookup(self):
        cog = Leaderboards(SimpleNamespace())
        interaction = self.interaction()
        await self.assert_acknowledged_before_work(
            interaction, (cog, "_points_embed"),
            lambda: cog.points_slash.callback(cog, interaction),
        )

    async def test_streak_profile_button_acknowledges_before_database_lookup(self):
        interaction = self.interaction()
        member = SimpleNamespace(id=42)
        guild = SimpleNamespace(id=1, get_member=lambda user_id: member)
        cog = Streaks(SimpleNamespace(get_guild=lambda guild_id: guild))
        interaction.type = discord.InteractionType.component
        interaction.data = {"custom_id": "streakpanel|me|1"}
        with patch.object(cog, "_unread_milestone", new=AsyncMock(return_value=None)):
            await self.assert_acknowledged_before_work(
                interaction, (cog, "_member_embed"), lambda: cog.on_interaction(interaction),
            )

    async def test_public_bank_leaderboard_acknowledges_before_query(self):
        interaction = self.interaction()
        cog = Bank(SimpleNamespace())
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_query(*args, **kwargs):
            started.set()
            await release.wait()
            return []

        with patch.object(cog, "get_top_contributors", side_effect=delayed_query):
            task = asyncio.create_task(cog.leaderboard.callback(cog, interaction))
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                interaction.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
                release.set()
                await task
                interaction.followup.send.assert_awaited_once()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_rulecard_post_acknowledges_slow_send_and_prevents_duplicate_clicks(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_send(**kwargs):
            started.set()
            await release.wait()

        channel = SimpleNamespace(send=AsyncMock(side_effect=slow_send))
        view = RulecardDraftView(
            creator_id=42, channel=channel, embed=discord.Embed(), mention_content=""
        )
        first, second = self.interaction(), self.interaction()
        first.edit_original_response = AsyncMock()
        second.edit_original_response = AsyncMock()
        task = asyncio.create_task(view._post(first, with_mentions=False))
        retry = None
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            first.response.defer.assert_awaited_once()
            retry = asyncio.create_task(view._post(second, with_mentions=False))
            await asyncio.sleep(0)
            second.response.defer.assert_awaited_once()
            release.set()
            await asyncio.gather(task, retry)
            channel.send.assert_awaited_once()
            first.edit_original_response.assert_awaited_once()
            second.followup.send.assert_awaited_once_with(
                "This draft was already posted.", ephemeral=True
            )
        finally:
            release.set()
            await asyncio.gather(task, *([retry] if retry else []), return_exceptions=True)
