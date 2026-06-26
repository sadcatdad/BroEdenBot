import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from cogs.vc_stats import VCStats
from utils.sqlite import configure_connection


class DummyRole:
    def __init__(self, role_id):
        self.id = role_id


class DummyState:
    def __init__(
        self,
        *,
        self_mute=False,
        self_deaf=False,
        mute=False,
        deaf=False,
    ):
        self.self_mute = self_mute
        self.self_deaf = self_deaf
        self.mute = mute
        self.deaf = deaf


class DummyMember:
    def __init__(self, member_id, guild, *, roles=None, bot=False):
        self.id = member_id
        self.guild = guild
        self.roles = roles or []
        self.bot = bot
        self.display_name = f"Member {member_id}"
        self.name = f"member{member_id}"


class DummyChannel:
    def __init__(self, channel_id, members):
        self.id = channel_id
        self.name = f"Channel {channel_id}"
        self.members = members


class DummyGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.afk_channel = None
        self.members = []

    def get_member(self, user_id):
        for member in self.members:
            if member.id == user_id:
                return member
        return None


class DummyBot:
    def __init__(self, database, guild):
        self.db = database
        self.guilds = [guild]
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if self._guild.id == guild_id else None


class VCRewardAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database_path)},
            clear=False,
        )
        self.environment.start()
        self.database = await aiosqlite.connect(":memory:")
        await configure_connection(self.database)
        self.guild = DummyGuild(123)
        self.bot = DummyBot(self.database, self.guild)
        self.cog = VCStats(self.bot)
        await self.cog._create_tables()

    async def asyncTearDown(self):
        await self.database.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    async def test_muted_interval_is_not_counted_for_vc_xp(self):
        start = datetime(2026, 6, 25, 12, tzinfo=timezone.utc)
        member = DummyMember(10, self.guild)
        companion = DummyMember(11, self.guild)
        self.guild.members = [member, companion]
        channel = DummyChannel(20, [member, companion])

        await self.cog._start_session(
            member,
            channel,
            DummyState(),
            start,
        )
        await self.cog._mark_channel_has_company(self.guild.id, channel)
        await self.cog._observe_session(
            member,
            channel,
            DummyState(self_mute=True),
            start + timedelta(minutes=10),
        )
        await self.cog._close_session(
            self.guild.id,
            member.id,
            start + timedelta(minutes=20),
        )
        await self.database.commit()

        cursor = await self.database.execute(
            """
            SELECT duration_seconds, counted_seconds, reward_eligible
            FROM vc_sessions
            """
        )
        row = await cursor.fetchone()
        await cursor.close()

        self.assertEqual(row[0], 20 * 60)
        self.assertEqual(row[1], 10 * 60)
        self.assertEqual(row[2], 1)

    async def test_deafened_time_can_make_session_too_short_for_xp(self):
        start = datetime(2026, 6, 25, 12, tzinfo=timezone.utc)
        member = DummyMember(10, self.guild)
        companion = DummyMember(11, self.guild)
        self.guild.members = [member, companion]
        channel = DummyChannel(20, [member, companion])

        await self.cog._start_session(
            member,
            channel,
            DummyState(self_deaf=True),
            start,
        )
        await self.cog._mark_channel_has_company(self.guild.id, channel)
        await self.cog._observe_session(
            member,
            channel,
            DummyState(),
            start + timedelta(minutes=10),
        )
        await self.cog._close_session(
            self.guild.id,
            member.id,
            start + timedelta(minutes=12),
        )
        await self.database.commit()

        cursor = await self.database.execute(
            """
            SELECT duration_seconds, counted_seconds, reward_eligible
            FROM vc_sessions
            """
        )
        row = await cursor.fetchone()
        await cursor.close()

        self.assertEqual(row[0], 12 * 60)
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0)

    def test_vcxp_role_exclusion_is_separate_from_vc_stats_exclusion(self):
        guild = DummyGuild(123)
        member = DummyMember(10, guild, roles=[DummyRole(55555555555555555)])
        bot = DummyBot(None, guild)
        with patch.dict(
            os.environ,
            {
                "VC_EXCLUDED_ROLE_IDS": "",
                "VCXP_EXCLUDED_ROLE_IDS": "55555555555555555",
            },
            clear=False,
        ):
            cog = VCStats(bot)
            self.assertFalse(cog._member_excluded(member))
            self.assertTrue(cog._member_xp_excluded(member))


if __name__ == "__main__":
    unittest.main()
