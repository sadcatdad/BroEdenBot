import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from cogs.vc_stats import VCStats
from utils.settings import initialize_settings_from_env, set_setting
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

    async def test_missing_active_row_can_be_restarted(self):
        now = datetime(2026, 6, 25, 12, tzinfo=timezone.utc)
        member = DummyMember(10, self.guild)
        companion = DummyMember(11, self.guild)
        self.guild.members = [member, companion]
        channel = DummyChannel(20, [member, companion])

        observed = await self.cog._observe_session(
            member,
            channel,
            DummyState(),
            now,
        )
        if not observed:
            await self.cog._start_session(member, channel, DummyState(), now)
        await self.database.commit()

        cursor = await self.database.execute(
            """
            SELECT guild_id, user_id, channel_id, reward_blocked_seconds,
                   reward_state_started_at
            FROM vc_active_sessions
            """
        )
        row = await cursor.fetchone()
        await cursor.close()

        self.assertEqual(row[0], self.guild.id)
        self.assertEqual(row[1], member.id)
        self.assertEqual(row[2], channel.id)
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], now.isoformat())

    async def test_stale_active_row_can_be_restarted_in_current_channel(self):
        stale_start = datetime(2026, 6, 25, 11, tzinfo=timezone.utc)
        now = datetime(2026, 6, 25, 12, tzinfo=timezone.utc)
        member = DummyMember(10, self.guild)
        companion = DummyMember(11, self.guild)
        self.guild.members = [member, companion]
        stale_channel = DummyChannel(19, [])
        channel = DummyChannel(20, [member, companion])

        await self.cog._start_session(
            member,
            stale_channel,
            DummyState(),
            stale_start,
        )
        await self.cog._start_session(member, channel, DummyState(), now)
        await self.database.commit()

        cursor = await self.database.execute(
            """
            SELECT channel_id, joined_at, last_seen_at, reward_blocked_seconds,
                   reward_state_started_at
            FROM vc_active_sessions
            WHERE guild_id = ? AND user_id = ?
            """,
            (self.guild.id, member.id),
        )
        row = await cursor.fetchone()
        await cursor.close()

        self.assertEqual(row[0], channel.id)
        self.assertEqual(row[1], now.isoformat())
        self.assertEqual(row[2], now.isoformat())
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], now.isoformat())

    def test_invalid_reward_state_timestamp_does_not_crash_tracking(self):
        now = datetime(2026, 6, 25, 12, tzinfo=timezone.utc)

        self.assertEqual(self.cog._elapsed_seconds("not-a-date", now), 0)

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

    async def test_vcxp_sync_ignores_sessions_before_reward_start(self):
        initialize_settings_from_env()
        set_setting(
            "VCXP_REWARD_START_AT",
            "2026-06-25T12:00:00+00:00",
        )
        self.guild.members = [DummyMember(10, self.guild)]
        await self.database.executemany(
            """
            INSERT INTO vc_sessions (
                guild_id, user_id, display_name, username,
                channel_id, channel_name, joined_at, left_at,
                duration_seconds, counted_seconds, was_alone,
                was_self_muted, was_self_deafened, was_server_muted,
                was_server_deafened, reward_eligible, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 1, ?)
            """,
            [
                (
                    self.guild.id,
                    10,
                    "Member 10",
                    "member10",
                    20,
                    "Lounge",
                    "2026-06-24T11:00:00+00:00",
                    "2026-06-24T12:00:00+00:00",
                    3600,
                    3600,
                    "2026-06-24T12:00:00+00:00",
                ),
                (
                    self.guild.id,
                    10,
                    "Member 10",
                    "member10",
                    20,
                    "Lounge",
                    "2026-06-25T12:00:00+00:00",
                    "2026-06-25T12:30:00+00:00",
                    1800,
                    1800,
                    "2026-06-25T12:30:00+00:00",
                ),
            ],
        )
        await self.database.commit()

        eligible_seconds, pulses_earned, pulses_paid = (
            await self.cog._sync_xp_user_state(self.guild.id, 10)
        )

        self.assertEqual(eligible_seconds, 1800)
        self.assertEqual(pulses_earned, 1)
        self.assertEqual(pulses_paid, 0)

    async def test_vcxp_bulk_sync_resets_stale_backpay_state(self):
        initialize_settings_from_env()
        set_setting(
            "VCXP_REWARD_START_AT",
            "2026-06-25T12:00:00+00:00",
        )
        self.guild.members = [DummyMember(10, self.guild)]
        await self.database.execute(
            """
            INSERT INTO vc_sessions (
                guild_id, user_id, display_name, username,
                channel_id, channel_name, joined_at, left_at,
                duration_seconds, counted_seconds, was_alone,
                was_self_muted, was_self_deafened, was_server_muted,
                was_server_deafened, reward_eligible, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 1, ?)
            """,
            (
                self.guild.id,
                10,
                "Member 10",
                "member10",
                20,
                "Lounge",
                "2026-06-24T11:00:00+00:00",
                "2026-06-24T12:00:00+00:00",
                3600,
                3600,
                "2026-06-24T12:00:00+00:00",
            ),
        )
        await self.database.execute(
            """
            INSERT INTO vc_xp_user_state (
                guild_id, user_id, eligible_seconds_total,
                pulses_earned, pulses_paid, updated_at
            ) VALUES (?, ?, 36000, 120, 0, ?)
            """,
            (self.guild.id, 10, "2026-06-25T00:00:00+00:00"),
        )
        await self.database.commit()

        await self.cog._sync_xp_states(self.guild.id)

        cursor = await self.database.execute(
            """
            SELECT eligible_seconds_total, pulses_earned, pulses_paid
            FROM vc_xp_user_state
            WHERE guild_id = ? AND user_id = ?
            """,
            (self.guild.id, 10),
        )
        row = await cursor.fetchone()
        await cursor.close()

        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0)

    async def test_vcxp_reward_start_defaults_to_startup_time(self):
        value = self.cog.vcxp_reward_start_at

        self.assertIsNotNone(value.tzinfo)
        self.assertGreaterEqual(
            value,
            datetime.now(timezone.utc) - timedelta(minutes=1),
        )


if __name__ == "__main__":
    unittest.main()
