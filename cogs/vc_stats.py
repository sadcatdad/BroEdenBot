import asyncio
import csv
import io
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.member_filter import current_member, member_filter_warning


logger = logging.getLogger(__name__)

MINIMUM_ELIGIBLE_SECONDS = 5 * 60
HEARTBEAT_SECONDS = 60
XP_PULSE_CHECK_SECONDS = 5 * 60
MAX_EXPORT_BYTES = 24 * 1024 * 1024
MAX_CURRENT_LINES = 75


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return max(minimum, int(raw_value))
    except ValueError:
        logger.warning("Invalid integer for %s; using default=%s", name, default)
        return default


class VCStats(commands.Cog):
    vcstats = app_commands.Group(
        name="vcstats",
        description="Private voice-channel activity statistics",
    )
    vcrewards = app_commands.Group(
        name="vcrewards",
        description="Manage VC XP role pulses and reward accounting",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.allowed_role_ids = self._parse_allowed_role_ids(
            os.getenv("VCSTATS_ALLOWED_ROLE_IDS", "")
        )
        rewards_roles = os.getenv("VCREWARDS_ALLOWED_ROLE_IDS", "").strip()
        self.rewards_allowed_role_ids = self._parse_allowed_role_ids(
            rewards_roles
        ) if rewards_roles else set(self.allowed_role_ids)
        self.vcxp_enabled = env_bool("VCXP_ENABLED", False)
        self.vcxp_trigger_role_id = env_int("VCXP_TRIGGER_ROLE_ID", 0)
        self.vcxp_minutes_per_pulse = env_int(
            "VCXP_MINUTES_PER_PULSE", 30, minimum=1
        )
        self.vcxp_role_remove_delay_seconds = env_int(
            "VCXP_ROLE_REMOVE_DELAY_SECONDS", 30
        )
        self.vcxp_daily_pulse_cap = env_int("VCXP_DAILY_PULSE_CAP", 4)
        self.vcxp_weekly_pulse_cap = env_int("VCXP_WEEKLY_PULSE_CAP", 20)
        self._tracking_lock = asyncio.Lock()
        self._pulse_state_lock = asyncio.Lock()
        self._pulses_in_progress: set = set()
        self._last_startup_reconcile = 0.0

    async def cog_load(self) -> None:
        await self._create_tables()
        self._heartbeat.start()
        self._xp_pulse_loop.start()

    async def cog_unload(self) -> None:
        self._heartbeat.cancel()
        self._xp_pulse_loop.cancel()

    async def _create_tables(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                joined_at TEXT NOT NULL,
                left_at TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL,
                counted_seconds INTEGER NOT NULL,
                was_alone INTEGER DEFAULT 0,
                was_self_muted INTEGER DEFAULT 0,
                was_self_deafened INTEGER DEFAULT 0,
                was_server_muted INTEGER DEFAULT 0,
                was_server_deafened INTEGER DEFAULT 0,
                reward_eligible INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_active_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                joined_at TEXT NOT NULL,
                last_seen_at TEXT,
                self_muted INTEGER DEFAULT 0,
                self_deafened INTEGER DEFAULT 0,
                server_muted INTEGER DEFAULT 0,
                server_deafened INTEGER DEFAULT 0,
                alone_entire INTEGER DEFAULT 1,
                self_muted_entire INTEGER DEFAULT 0,
                self_deafened_entire INTEGER DEFAULT 0,
                server_muted_entire INTEGER DEFAULT 0,
                server_deafened_entire INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await self._ensure_active_observation_columns()
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_reward_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_by INTEGER,
                generated_at TEXT NOT NULL,
                notes TEXT
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_reward_snapshot_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                total_seconds INTEGER NOT NULL,
                eligible_seconds INTEGER NOT NULL,
                reward_points INTEGER DEFAULT 0,
                reward_status TEXT DEFAULT 'pending',
                FOREIGN KEY(snapshot_id) REFERENCES vc_reward_snapshots(id)
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_xp_pulses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                eligible_seconds_snapshot INTEGER NOT NULL,
                pulse_number INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                granted_at TEXT NOT NULL,
                removed_at TEXT,
                status TEXT NOT NULL,
                error TEXT
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS vc_xp_user_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                eligible_seconds_total INTEGER DEFAULT 0,
                pulses_earned INTEGER DEFAULT 0,
                pulses_paid INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vc_sessions_guild_left
            ON vc_sessions (guild_id, left_at)
            """
        )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vc_sessions_guild_user_left
            ON vc_sessions (guild_id, user_id, left_at)
            """
        )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vc_sessions_guild_channel_left
            ON vc_sessions (guild_id, channel_id, left_at)
            """
        )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vc_xp_pulses_guild_user_granted
            ON vc_xp_pulses (guild_id, user_id, granted_at)
            """
        )
        await self.bot.db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vc_xp_one_active_pulse
            ON vc_xp_pulses (guild_id, user_id)
            WHERE status IN ('pending', 'granted')
            """
        )
        settings = {
            "minimum_eligible_session_seconds": str(MINIMUM_ELIGIBLE_SECONDS),
            "exclude_afk_channel": "1",
            "exclude_alone_sessions": "1",
            "exclude_self_deafened_sessions": "1",
        }
        await self.bot.db.executemany(
            "INSERT OR IGNORE INTO vc_settings (key, value) VALUES (?, ?)",
            settings.items(),
        )
        await self.bot.db.commit()

    async def _ensure_active_observation_columns(self) -> None:
        cursor = await self.bot.db.execute("PRAGMA table_info(vc_active_sessions)")
        existing = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        additions = {
            "alone_entire": "INTEGER DEFAULT 1",
            "self_muted_entire": "INTEGER DEFAULT 0",
            "self_deafened_entire": "INTEGER DEFAULT 0",
            "server_muted_entire": "INTEGER DEFAULT 0",
            "server_deafened_entire": "INTEGER DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in existing:
                await self.bot.db.execute(
                    f"ALTER TABLE vc_active_sessions ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _parse_allowed_role_ids(raw_value: str) -> set:
        return {
            int(value)
            for value in re.split(r"[\s,]+", raw_value.strip())
            if value.isdigit()
        }

    def _has_access(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        return any(
            role.id in self.allowed_role_ids for role in interaction.user.roles
        )

    def _has_rewards_access(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        return any(
            role.id in self.rewards_allowed_role_ids
            for role in interaction.user.roles
        )

    async def _deny_if_unauthorised(
        self, interaction: discord.Interaction
    ) -> bool:
        if self._has_access(interaction):
            return False
        await interaction.response.send_message(
            "You do not have permission to use VC stats commands.",
            ephemeral=True,
        )
        return True

    @staticmethod
    def _is_administrator(interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild
            and isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )

    @staticmethod
    def _voice_flags(state: discord.VoiceState) -> Tuple[int, int, int, int]:
        return (
            int(state.self_mute),
            int(state.self_deaf),
            int(state.mute),
            int(state.deaf),
        )

    @staticmethod
    def _non_bot_members(channel: discord.abc.Connectable) -> List[discord.Member]:
        return [member for member in channel.members if not member.bot]

    async def _start_session(
        self,
        member: discord.Member,
        channel: discord.abc.Connectable,
        state: discord.VoiceState,
        started_at: datetime,
    ) -> None:
        flags = self._voice_flags(state)
        alone = int(len(self._non_bot_members(channel)) <= 1)
        timestamp = started_at.isoformat()
        await self.bot.db.execute(
            """
            INSERT INTO vc_active_sessions (
                guild_id, user_id, display_name, username,
                channel_id, channel_name, joined_at, last_seen_at,
                self_muted, self_deafened, server_muted, server_deafened,
                alone_entire, self_muted_entire, self_deafened_entire,
                server_muted_entire, server_deafened_entire
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (
                member.guild.id,
                member.id,
                member.display_name,
                member.name,
                channel.id,
                channel.name,
                timestamp,
                timestamp,
                *flags,
                alone,
                *flags,
            ),
        )

    async def _observe_session(
        self,
        member: discord.Member,
        channel: discord.abc.Connectable,
        state: discord.VoiceState,
        observed_at: datetime,
    ) -> None:
        flags = self._voice_flags(state)
        alone = int(len(self._non_bot_members(channel)) <= 1)
        await self.bot.db.execute(
            """
            UPDATE vc_active_sessions
            SET display_name = ?,
                username = ?,
                channel_name = ?,
                last_seen_at = ?,
                self_muted = ?,
                self_deafened = ?,
                server_muted = ?,
                server_deafened = ?,
                alone_entire = alone_entire AND ?,
                self_muted_entire = self_muted_entire AND ?,
                self_deafened_entire = self_deafened_entire AND ?,
                server_muted_entire = server_muted_entire AND ?,
                server_deafened_entire = server_deafened_entire AND ?
            WHERE guild_id = ? AND user_id = ? AND channel_id = ?
            """,
            (
                member.display_name,
                member.name,
                channel.name,
                observed_at.isoformat(),
                *flags,
                alone,
                *flags,
                member.guild.id,
                member.id,
                channel.id,
            ),
        )

    async def _mark_channel_has_company(
        self, guild_id: int, channel: discord.abc.Connectable
    ) -> None:
        if len(self._non_bot_members(channel)) < 2:
            return
        await self.bot.db.execute(
            """
            UPDATE vc_active_sessions
            SET alone_entire = 0
            WHERE guild_id = ? AND channel_id = ?
            """,
            (guild_id, channel.id),
        )

    async def _close_session(
        self,
        guild_id: int,
        user_id: int,
        ended_at: Optional[datetime] = None,
    ) -> bool:
        cursor = await self.bot.db.execute(
            """
            SELECT display_name, username, channel_id, channel_name,
                   joined_at, last_seen_at,
                   alone_entire, self_muted_entire, self_deafened_entire,
                   server_muted_entire, server_deafened_entire
            FROM vc_active_sessions
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return False

        (
            display_name,
            username,
            channel_id,
            channel_name,
            joined_at_raw,
            last_seen_at_raw,
            was_alone,
            was_self_muted,
            was_self_deafened,
            was_server_muted,
            was_server_deafened,
        ) = row
        joined_at = parse_timestamp(joined_at_raw)
        safe_last_seen = parse_timestamp(last_seen_at_raw or joined_at_raw)
        left_at = ended_at or safe_last_seen
        if left_at < joined_at:
            left_at = joined_at
        duration = max(0, int((left_at - joined_at).total_seconds()))

        guild = self.bot.get_guild(guild_id)
        afk_channel_id = guild.afk_channel.id if guild and guild.afk_channel else None
        reward_eligible = int(
            duration >= MINIMUM_ELIGIBLE_SECONDS
            and channel_id != afk_channel_id
            and not was_alone
            and not was_self_deafened
        )
        counted_seconds = duration if reward_eligible else 0

        await self.bot.db.execute(
            """
            INSERT INTO vc_sessions (
                guild_id, user_id, display_name, username,
                channel_id, channel_name, joined_at, left_at,
                duration_seconds, counted_seconds, was_alone,
                was_self_muted, was_self_deafened, was_server_muted,
                was_server_deafened, reward_eligible, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                display_name,
                username,
                channel_id,
                channel_name,
                joined_at.isoformat(),
                left_at.isoformat(),
                duration,
                counted_seconds,
                int(bool(was_alone)),
                int(bool(was_self_muted)),
                int(bool(was_self_deafened)),
                int(bool(was_server_muted)),
                int(bool(was_server_deafened)),
                reward_eligible,
                utc_now().isoformat(),
            ),
        )
        await self.bot.db.execute(
            "DELETE FROM vc_active_sessions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return True

    async def _load_active_rows(self) -> List[Tuple[int, int, int, str]]:
        cursor = await self.bot.db.execute(
            """
            SELECT guild_id, user_id, channel_id, last_seen_at
            FROM vc_active_sessions
            """
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    def _current_voice_members(
        self,
    ) -> Dict[Tuple[int, int], Tuple[discord.Member, discord.abc.Connectable]]:
        current = {}
        for guild in self.bot.guilds:
            for channel in guild.voice_channels + guild.stage_channels:
                for member in self._non_bot_members(channel):
                    current[(guild.id, member.id)] = (member, channel)
        return current

    async def _reconcile_sessions(self, startup: bool) -> None:
        if not self.bot.is_ready():
            return
        now = utc_now()
        current = self._current_voice_members()
        async with self._tracking_lock:
            active_rows = await self._load_active_rows()
            active = {(row[0], row[1]): row for row in active_rows}

            for key, row in active.items():
                guild_id, user_id, channel_id, last_seen_raw = row
                occupant = current.get(key)
                if startup or not occupant or occupant[1].id != channel_id:
                    safe_end = parse_timestamp(last_seen_raw) if last_seen_raw else now
                    await self._close_session(guild_id, user_id, safe_end)

            if startup:
                active = {}
            else:
                active = {
                    key: row
                    for key, row in active.items()
                    if key in current and current[key][1].id == row[2]
                }

            for key, (member, channel) in current.items():
                state = member.voice
                if state is None:
                    continue
                if key not in active:
                    await self._start_session(member, channel, state, now)
                else:
                    await self._observe_session(member, channel, state, now)
                await self._mark_channel_has_company(member.guild.id, channel)
            await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        now = time.monotonic()
        if now - self._last_startup_reconcile < 5:
            return
        self._last_startup_reconcile = now
        try:
            await self._reconcile_sessions(startup=True)
        except Exception:
            logger.exception("VC startup reconciliation failed")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        now = utc_now()
        try:
            async with self._tracking_lock:
                if before.channel == after.channel:
                    if after.channel:
                        await self._observe_session(
                            member, after.channel, after, now
                        )
                        await self._mark_channel_has_company(
                            member.guild.id, after.channel
                        )
                    await self.bot.db.commit()
                    return

                if before.channel:
                    await self._observe_session(
                        member, before.channel, before, now
                    )
                    await self._close_session(member.guild.id, member.id, now)

                if after.channel:
                    await self._start_session(member, after.channel, after, now)
                    await self._mark_channel_has_company(
                        member.guild.id, after.channel
                    )

                if before.channel:
                    await self._mark_channel_has_company(
                        member.guild.id, before.channel
                    )
                await self.bot.db.commit()
        except Exception:
            logger.exception(
                "VC state tracking failed for guild=%s user=%s",
                member.guild.id,
                member.id,
            )

    @tasks.loop(seconds=HEARTBEAT_SECONDS)
    async def _heartbeat(self) -> None:
        try:
            await self._reconcile_sessions(startup=False)
        except Exception:
            logger.exception("VC heartbeat reconciliation failed")

    @_heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
        await self.bot.wait_until_ready()

    @property
    def _xp_seconds_per_pulse(self) -> int:
        return self.vcxp_minutes_per_pulse * 60

    async def _sync_xp_user_state(
        self, guild_id: int, user_id: int
    ) -> Tuple[int, int, int]:
        cursor = await self.bot.db.execute(
            """
            SELECT COALESCE(SUM(counted_seconds), 0)
            FROM vc_sessions
            WHERE guild_id = ? AND user_id = ? AND reward_eligible = 1
            """,
            (guild_id, user_id),
        )
        eligible_seconds = int((await cursor.fetchone())[0] or 0)
        await cursor.close()
        pulses_earned = eligible_seconds // self._xp_seconds_per_pulse
        now = utc_now().isoformat()
        await self.bot.db.execute(
            """
            INSERT INTO vc_xp_user_state (
                guild_id, user_id, eligible_seconds_total,
                pulses_earned, pulses_paid, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                eligible_seconds_total = excluded.eligible_seconds_total,
                pulses_earned = excluded.pulses_earned,
                updated_at = excluded.updated_at
            """,
            (guild_id, user_id, eligible_seconds, pulses_earned, now),
        )
        cursor = await self.bot.db.execute(
            """
            SELECT eligible_seconds_total, pulses_earned, pulses_paid
            FROM vc_xp_user_state
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]), int(row[1]), int(row[2])

    async def _sync_xp_states(self, guild_id: int) -> None:
        rows = await self._fetchall(
            """
            SELECT user_id, COALESCE(SUM(counted_seconds), 0)
            FROM vc_sessions
            WHERE guild_id = ? AND reward_eligible = 1
            GROUP BY user_id
            """,
            (guild_id,),
        )
        now = utc_now().isoformat()
        await self.bot.db.execute(
            """
            UPDATE vc_xp_user_state
            SET eligible_seconds_total = 0,
                pulses_earned = 0,
                updated_at = ?
            WHERE guild_id = ?
            """,
            (now, guild_id),
        )
        if rows:
            await self.bot.db.executemany(
                """
                INSERT INTO vc_xp_user_state (
                    guild_id, user_id, eligible_seconds_total,
                    pulses_earned, pulses_paid, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    eligible_seconds_total = excluded.eligible_seconds_total,
                    pulses_earned = excluded.pulses_earned,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        guild_id,
                        user_id,
                        int(eligible_seconds),
                        int(eligible_seconds) // self._xp_seconds_per_pulse,
                        now,
                    )
                    for user_id, eligible_seconds in rows
                ],
            )
        await self.bot.db.commit()

    async def _pulse_cap_counts(
        self, guild_id: int, user_id: int
    ) -> Tuple[int, int]:
        now = utc_now()
        day_start = datetime.combine(
            now.date(), datetime.min.time(), tzinfo=timezone.utc
        )
        week_start = now - timedelta(days=7)
        cursor = await self.bot.db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN granted_at >= ? THEN 1 ELSE 0 END), 0),
                COUNT(*)
            FROM vc_xp_pulses
            WHERE guild_id = ? AND user_id = ?
              AND granted_at >= ?
              AND status IN (
                  'paid',
                  'remove_failed_assumed_paid',
                  'stale_assumed_paid',
                  'marked_paid'
              )
            """,
            (day_start.isoformat(), guild_id, user_id, week_start.isoformat()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]), int(row[1])

    async def _set_pulse_failure(
        self, pulse_id: int, status: str, exc: Exception
    ) -> None:
        await self.bot.db.execute(
            """
            UPDATE vc_xp_pulses
            SET status = ?, error = ?
            WHERE id = ?
            """,
            (status, type(exc).__name__, pulse_id),
        )
        await self.bot.db.commit()

    async def _recover_stale_pulses(
        self, guild: discord.Guild, role: discord.Role
    ) -> None:
        stale_before = utc_now() - timedelta(
            seconds=self.vcxp_role_remove_delay_seconds + 60
        )
        rows = await self._fetchall(
            """
            SELECT id, user_id
            FROM vc_xp_pulses
            WHERE guild_id = ?
              AND status IN ('pending', 'granted')
              AND granted_at <= ?
            ORDER BY id
            """,
            (guild.id, stale_before.isoformat()),
        )
        for pulse_id, user_id in rows:
            key = (guild.id, user_id)
            async with self._pulse_state_lock:
                if key in self._pulses_in_progress:
                    continue
                self._pulses_in_progress.add(key)
            try:
                await self._sync_xp_user_state(guild.id, user_id)
                member = guild.get_member(user_id)
                removed_at = None
                error = "Recovered interrupted pulse; assumed paid"
                if member and role in member.roles:
                    try:
                        await member.remove_roles(
                            role,
                            reason="BroEdenBot recovered interrupted VC XP pulse",
                        )
                        removed_at = utc_now()
                    except Exception as exc:
                        error = (
                            "Recovered interrupted pulse; role removal failed: "
                            f"{type(exc).__name__}"
                        )
                await self._record_paid_pulse(
                    guild.id,
                    user_id,
                    pulse_id,
                    "stale_assumed_paid",
                    removed_at,
                    error,
                )
                logger.warning(
                    "Recovered interrupted VC XP pulse as paid: "
                    "guild=%s user=%s role=%s pulse_id=%s",
                    guild.id,
                    user_id,
                    role.id,
                    pulse_id,
                )
            finally:
                async with self._pulse_state_lock:
                    self._pulses_in_progress.discard(key)

    async def _record_paid_pulse(
        self,
        guild_id: int,
        user_id: int,
        pulse_id: int,
        status: str,
        removed_at: Optional[datetime],
        error: Optional[str],
    ) -> None:
        await self.bot.db.execute(
            """
            UPDATE vc_xp_pulses
            SET removed_at = ?, status = ?, error = ?
            WHERE id = ?
            """,
            (
                removed_at.isoformat() if removed_at else None,
                status,
                error,
                pulse_id,
            ),
        )
        await self.bot.db.execute(
            """
            UPDATE vc_xp_user_state
            SET pulses_paid = pulses_paid + 1,
                updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (utc_now().isoformat(), guild_id, user_id),
        )
        await self.bot.db.commit()

    async def _pay_one_pulse(
        self,
        member: discord.Member,
        role: discord.Role,
        automatic: bool,
    ) -> Tuple[bool, str]:
        key = (member.guild.id, member.id)
        async with self._pulse_state_lock:
            if key in self._pulses_in_progress:
                return False, "A pulse is already in progress for that member."
            self._pulses_in_progress.add(key)

        pulse_id = None
        try:
            if member.bot:
                return False, "Bots cannot receive VC XP pulses."
            if role.managed:
                return False, "The configured trigger role is managed by an integration."
            bot_member = member.guild.me
            if bot_member is None or role >= bot_member.top_role:
                return (
                    False,
                    "BroEdenBot's highest role must be above the VC XP trigger role.",
                )
            if role in member.roles:
                logger.info(
                    "VC XP pulse skipped: role already present guild=%s user=%s role=%s",
                    member.guild.id,
                    member.id,
                    role.id,
                )
                return False, "The member already has the trigger role."

            eligible_seconds, pulses_earned, pulses_paid = (
                await self._sync_xp_user_state(member.guild.id, member.id)
            )
            if automatic:
                if pulses_earned <= pulses_paid:
                    return False, "The member has no unpaid pulses."
                daily_paid, weekly_paid = await self._pulse_cap_counts(
                    member.guild.id, member.id
                )
                if daily_paid >= self.vcxp_daily_pulse_cap:
                    return False, "The member has reached the daily pulse cap."
                if weekly_paid >= self.vcxp_weekly_pulse_cap:
                    return False, "The member has reached the weekly pulse cap."

            pulse_number = pulses_paid + 1
            granted_at = utc_now()
            try:
                cursor = await self.bot.db.execute(
                    """
                    INSERT INTO vc_xp_pulses (
                        guild_id, user_id, eligible_seconds_snapshot,
                        pulse_number, role_id, granted_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        member.guild.id,
                        member.id,
                        eligible_seconds,
                        pulse_number,
                        role.id,
                        granted_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False, "A pulse is already reserved for that member."
            pulse_id = cursor.lastrowid
            await cursor.close()
            await self.bot.db.commit()

            try:
                await member.add_roles(
                    role,
                    reason="BroEdenBot eligible VC time XP role pulse",
                )
            except Exception as exc:
                await self._set_pulse_failure(pulse_id, "add_failed", exc)
                logger.warning(
                    "VC XP role add failed: guild=%s user=%s role=%s error_type=%s",
                    member.guild.id,
                    member.id,
                    role.id,
                    type(exc).__name__,
                )
                return False, f"Role add failed ({type(exc).__name__})."

            await self.bot.db.execute(
                "UPDATE vc_xp_pulses SET status = 'granted' WHERE id = ?",
                (pulse_id,),
            )
            await self.bot.db.commit()
            logger.info(
                "VC XP role granted: guild=%s user=%s role=%s pulse=%s",
                member.guild.id,
                member.id,
                role.id,
                pulse_number,
            )

            cancellation = None
            removal_error = None
            try:
                await asyncio.sleep(self.vcxp_role_remove_delay_seconds)
                await member.remove_roles(
                    role,
                    reason="BroEdenBot VC XP role pulse completed",
                )
            except asyncio.CancelledError as exc:
                cancellation = exc
                try:
                    await member.remove_roles(
                        role,
                        reason="BroEdenBot VC XP pulse cancelled during shutdown",
                    )
                except Exception as remove_exc:
                    removal_error = remove_exc
            except Exception as exc:
                removal_error = exc

            if removal_error is None:
                await self._record_paid_pulse(
                    member.guild.id,
                    member.id,
                    pulse_id,
                    "paid",
                    utc_now(),
                    None,
                )
                logger.info(
                    "VC XP role removed and pulse paid: "
                    "guild=%s user=%s role=%s pulse=%s",
                    member.guild.id,
                    member.id,
                    role.id,
                    pulse_number,
                )
            else:
                await self._record_paid_pulse(
                    member.guild.id,
                    member.id,
                    pulse_id,
                    "remove_failed_assumed_paid",
                    None,
                    type(removal_error).__name__,
                )
                logger.warning(
                    "VC XP role removal failed; pulse counted paid to avoid duplicate: "
                    "guild=%s user=%s role=%s pulse=%s error_type=%s",
                    member.guild.id,
                    member.id,
                    role.id,
                    pulse_number,
                    type(removal_error).__name__,
                )

            if cancellation is not None:
                raise cancellation
            if removal_error is not None:
                return (
                    True,
                    "The role was added, but removal failed. The pulse was counted "
                    "as paid to prevent a duplicate.",
                )
            return True, f"Pulse {pulse_number} completed."
        finally:
            async with self._pulse_state_lock:
                self._pulses_in_progress.discard(key)

    async def _run_automatic_pulses(self) -> None:
        if not self.vcxp_enabled or not self.vcxp_trigger_role_id:
            return
        pulse_tasks = []
        for guild in self.bot.guilds:
            role = guild.get_role(self.vcxp_trigger_role_id)
            if role is None:
                logger.warning(
                    "VC XP trigger role not found: guild=%s role=%s",
                    guild.id,
                    self.vcxp_trigger_role_id,
                )
                continue
            await self._sync_xp_states(guild.id)
            await self._recover_stale_pulses(guild, role)
            rows = await self._fetchall(
                """
                SELECT user_id
                FROM vc_xp_user_state
                WHERE guild_id = ? AND pulses_earned > pulses_paid
                ORDER BY (pulses_earned - pulses_paid) DESC, user_id
                """,
                (guild.id,),
            )
            for (user_id,) in rows:
                member = guild.get_member(user_id)
                if member is None or member.bot:
                    logger.info(
                        "VC XP pulse skipped: member unavailable guild=%s user=%s",
                        guild.id,
                        user_id,
                    )
                    continue
                pulse_tasks.append(
                    self._pay_one_pulse(member, role, automatic=True)
                )
        if pulse_tasks:
            await asyncio.gather(*pulse_tasks)

    @tasks.loop(seconds=XP_PULSE_CHECK_SECONDS)
    async def _xp_pulse_loop(self) -> None:
        try:
            await self._run_automatic_pulses()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("VC XP pulse task failed")

    @_xp_pulse_loop.before_loop
    async def _before_xp_pulse_loop(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _cutoff(days: int) -> str:
        return (utc_now() - timedelta(days=days)).isoformat()

    async def _fetchall(self, query: str, parameters: Iterable = ()) -> list:
        cursor = await self.bot.db.execute(query, tuple(parameters))
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _table_exists(self, table_name: str) -> bool:
        rows = await self._fetchall(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return bool(rows)

    async def _send_lines(
        self,
        interaction: discord.Interaction,
        heading: str,
        lines: List[str],
    ) -> None:
        pages = []
        current = heading
        for line in lines:
            candidate = f"{current}\n{line}"
            if len(candidate) > 1_950:
                pages.append(current)
                current = f"{heading} (continued)\n{line}"
            else:
                current = candidate
        pages.append(current)
        await interaction.followup.send(pages[0], ephemeral=True)
        for page in pages[1:]:
            await interaction.followup.send(page, ephemeral=True)

    @vcstats.command(name="user", description="Show VC stats for a member")
    @app_commands.describe(user="Member to inspect", days="Lookback period in days")
    @app_commands.guild_only()
    async def user_stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        days: app_commands.Range[int, 1, 3650] = 30,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._fetchall(
            """
            SELECT COALESCE(SUM(duration_seconds), 0),
                   COALESCE(SUM(counted_seconds), 0),
                   COUNT(*),
                   COALESCE(AVG(duration_seconds), 0)
            FROM vc_sessions
            WHERE guild_id = ? AND user_id = ? AND left_at >= ?
            """,
            (interaction.guild_id, user.id, self._cutoff(days)),
        )
        total, eligible, sessions, average = rows[0]
        top_rows = await self._fetchall(
            """
            SELECT channel_name, channel_id, SUM(duration_seconds) AS total
            FROM vc_sessions
            WHERE guild_id = ? AND user_id = ? AND left_at >= ?
            GROUP BY channel_id, channel_name
            ORDER BY total DESC
            LIMIT 1
            """,
            (interaction.guild_id, user.id, self._cutoff(days)),
        )
        top_channel = (
            top_rows[0][0] or f"Deleted channel ({top_rows[0][1]})"
            if top_rows
            else "None"
        )
        await interaction.followup.send(
            f"**VC stats for {user.mention} — last {days} days**\n"
            f"Total tracked: **{format_duration(total)}**\n"
            f"Reward-eligible: **{format_duration(eligible)}**\n"
            f"Sessions: **{sessions}**\n"
            f"Top channel: **{discord.utils.escape_markdown(top_channel)}**\n"
            f"Average session: **{format_duration(average)}**",
            ephemeral=True,
        )

    @vcstats.command(name="leaderboard", description="Show the VC leaderboard")
    @app_commands.describe(
        days="Lookback period in days",
        limit="Number of members to show",
        eligible_only="Rank using reward-eligible time only",
        include_left_members="Include users who are no longer in the server",
    )
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 30,
        limit: app_commands.Range[int, 1, 25] = 10,
        eligible_only: bool = False,
        include_left_members: bool = False,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        time_column = "counted_seconds" if eligible_only else "duration_seconds"
        rows = await self._fetchall(
            f"""
            SELECT user_id, MAX(display_name), MAX(username),
                   SUM({time_column}) AS ranked_seconds
            FROM vc_sessions
            WHERE guild_id = ? AND left_at >= ?
            GROUP BY user_id
            HAVING ranked_seconds > 0
            ORDER BY ranked_seconds DESC
            """,
            (interaction.guild_id, self._cutoff(days)),
        )
        filtered_rows = []
        for user_id, display_name, username, ranked_seconds in rows:
            member = current_member(interaction.guild, user_id)
            if member is None and not include_left_members:
                continue
            filtered_rows.append(
                (
                    user_id,
                    member.display_name if member else display_name,
                    member.name if member else username,
                    ranked_seconds,
                    member is not None,
                )
            )
            if len(filtered_rows) >= limit:
                break
        if not filtered_rows:
            await interaction.followup.send(
                "No matching VC sessions were found.", ephemeral=True
            )
            return
        label = "eligible time" if eligible_only else "tracked time"
        lines = [
            (
                f"{index}. "
                f"{f'<@{row[0]}>' if row[4] else discord.utils.escape_markdown(row[1] or row[2] or str(row[0])) + ' — Left server'} "
                f"— **{format_duration(row[3])}**"
            )
            for index, row in enumerate(filtered_rows, 1)
        ]
        scope = "Includes left members" if include_left_members else "Current members only"
        warning = member_filter_warning(self.bot, interaction.guild)
        if warning and not include_left_members:
            lines.append(f"⚠️ {warning}")
        await self._send_lines(
            interaction,
            f"**VC leaderboard — {label}, last {days} days**\n{scope}.",
            lines,
        )

    @vcstats.command(
        name="current", description="Show members currently tracked in VC"
    )
    @app_commands.guild_only()
    async def current(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._fetchall(
            """
            SELECT user_id, display_name, channel_id, channel_name, joined_at,
                   self_muted, self_deafened, server_muted, server_deafened
            FROM vc_active_sessions
            WHERE guild_id = ?
            ORDER BY channel_name, display_name
            """,
            (interaction.guild_id,),
        )
        if not rows:
            await interaction.followup.send(
                "No members are currently being tracked in VC.", ephemeral=True
            )
            return
        now = utc_now()
        lines = []
        for row in rows[:MAX_CURRENT_LINES]:
            duration = int((now - parse_timestamp(row[4])).total_seconds())
            statuses = []
            if row[5]:
                statuses.append("self-muted")
            if row[6]:
                statuses.append("self-deafened")
            if row[7]:
                statuses.append("server-muted")
            if row[8]:
                statuses.append("server-deafened")
            status = f" ({', '.join(statuses)})" if statuses else ""
            channel = row[3] or f"Deleted channel ({row[2]})"
            lines.append(
                f"<@{row[0]}> — **{discord.utils.escape_markdown(channel)}** "
                f"— {format_duration(duration)}{status}"
            )
        if len(rows) > MAX_CURRENT_LINES:
            lines.append(f"…and {len(rows) - MAX_CURRENT_LINES} more.")
        await self._send_lines(interaction, "**Currently tracked in VC**", lines)

    @vcstats.command(name="channel", description="Show voice-channel stats")
    @app_commands.describe(
        channel="Specific voice channel, or leave blank for top channels",
        days="Lookback period in days",
    )
    @app_commands.guild_only()
    async def channel_stats(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.VoiceChannel] = None,
        days: app_commands.Range[int, 1, 3650] = 30,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cutoff = self._cutoff(days)
        if channel:
            rows = await self._fetchall(
                """
                SELECT COALESCE(SUM(duration_seconds), 0),
                       COALESCE(SUM(counted_seconds), 0),
                       COUNT(*), COUNT(DISTINCT user_id)
                FROM vc_sessions
                WHERE guild_id = ? AND channel_id = ? AND left_at >= ?
                """,
                (interaction.guild_id, channel.id, cutoff),
            )
            total, eligible, sessions, members = rows[0]
            await interaction.followup.send(
                f"**VC stats for {channel.mention} — last {days} days**\n"
                f"Total tracked: **{format_duration(total)}**\n"
                f"Reward-eligible: **{format_duration(eligible)}**\n"
                f"Sessions: **{sessions}**\n"
                f"Unique members: **{members}**",
                ephemeral=True,
            )
            return

        rows = await self._fetchall(
            """
            SELECT channel_id, MAX(channel_name), SUM(duration_seconds) AS total,
                   COUNT(*)
            FROM vc_sessions
            WHERE guild_id = ? AND left_at >= ?
            GROUP BY channel_id
            ORDER BY total DESC
            LIMIT 10
            """,
            (interaction.guild_id, cutoff),
        )
        if not rows:
            await interaction.followup.send(
                "No matching VC sessions were found.", ephemeral=True
            )
            return
        lines = [
            f"{index}. **{discord.utils.escape_markdown(row[1] or f'Deleted channel ({row[0]})')}** "
            f"— {format_duration(row[2])} across {row[3]} sessions"
            for index, row in enumerate(rows, 1)
        ]
        await self._send_lines(
            interaction, f"**Top voice channels — last {days} days**", lines
        )

    async def _session_export_rows(
        self,
        guild_id: int,
        days: int,
        user_id: Optional[int],
        channel_id: Optional[int],
    ) -> list:
        conditions = ["guild_id = ?", "left_at >= ?"]
        parameters: List[object] = [guild_id, self._cutoff(days)]
        if user_id is not None:
            conditions.append("user_id = ?")
            parameters.append(user_id)
        if channel_id is not None:
            conditions.append("channel_id = ?")
            parameters.append(channel_id)
        return await self._fetchall(
            f"""
            SELECT user_id, username, display_name, channel_id, channel_name,
                   joined_at, left_at, duration_seconds, counted_seconds,
                   reward_eligible, was_alone, was_self_muted,
                   was_self_deafened, was_server_muted, was_server_deafened
            FROM vc_sessions
            WHERE {' AND '.join(conditions)}
            ORDER BY joined_at DESC
            """,
            parameters,
        )

    @staticmethod
    def _csv_file(
        filename: str, headers: List[str], rows: Iterable[Iterable]
    ) -> Optional[discord.File]:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(rows)
        data = output.getvalue().encode("utf-8-sig")
        if len(data) > MAX_EXPORT_BYTES:
            return None
        return discord.File(io.BytesIO(data), filename=filename)

    @vcstats.command(name="export", description="Export VC sessions to CSV")
    @app_commands.describe(
        days="Lookback period in days",
        user="Optional member filter",
        channel="Optional voice-channel filter",
        include_left_members="Include users who are no longer in the server",
    )
    @app_commands.guild_only()
    async def export_sessions(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 30,
        user: Optional[discord.Member] = None,
        channel: Optional[discord.VoiceChannel] = None,
        include_left_members: bool = False,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._session_export_rows(
            interaction.guild_id,
            days,
            user.id if user else None,
            channel.id if channel else None,
        )
        export_rows = []
        for row in rows:
            member = current_member(interaction.guild, row[0])
            if member is None and not include_left_members:
                continue
            export_rows.append(
                (
                    row[0],
                    member.name if member else row[1],
                    member.display_name if member else row[2],
                    member is not None,
                    *row[3:8],
                    format_duration(row[7]),
                    row[8],
                    format_duration(row[8]),
                    *row[9:],
                )
            )
        headers = [
            "user_id",
            "username",
            "display_name",
            "is_current_member",
            "channel_id",
            "channel_name",
            "joined_at",
            "left_at",
            "duration_seconds",
            "duration_readable",
            "counted_seconds",
            "counted_readable",
            "reward_eligible",
            "was_alone",
            "was_self_muted",
            "was_self_deafened",
            "was_server_muted",
            "was_server_deafened",
        ]
        file = self._csv_file(
            f"vc_sessions_{days}d.csv", headers, export_rows
        )
        if file is None:
            await interaction.followup.send(
                "That export is too large for Discord. Use a shorter date range.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Exported **{len(export_rows)}** matching VC sessions."
            + (
                f"\n⚠️ {member_filter_warning(self.bot, interaction.guild)}"
                if not include_left_members
                and member_filter_warning(self.bot, interaction.guild)
                else ""
            ),
            file=file,
            ephemeral=True,
        )

    @vcstats.command(name="reset", description="Clear VC tracking data")
    @app_commands.describe(confirm="Must be true to clear session data")
    @app_commands.guild_only()
    async def reset(
        self, interaction: discord.Interaction, confirm: bool
    ) -> None:
        if not self._is_administrator(interaction):
            await interaction.response.send_message(
                "Only administrators can reset VC stats.", ephemeral=True
            )
            return
        if not confirm:
            await interaction.response.send_message(
                "Nothing was reset. Set confirm to true to clear VC session data.",
                ephemeral=True,
            )
            return
        async with self._tracking_lock:
            await self.bot.db.execute(
                "DELETE FROM vc_sessions WHERE guild_id = ?",
                (interaction.guild_id,),
            )
            await self.bot.db.execute(
                "DELETE FROM vc_active_sessions WHERE guild_id = ?",
                (interaction.guild_id,),
            )
            await self.bot.db.commit()
        await interaction.response.send_message(
            "VC sessions and active tracking rows were cleared for this server. "
            "Reward snapshots and VC XP pulse accounting were not changed.",
            ephemeral=True,
        )

    @vcstats.command(
        name="settings", description="Show current VC tracking settings"
    )
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        rows = await self._fetchall("SELECT key, value FROM vc_settings")
        values = dict(rows)
        minimum = int(
            values.get(
                "minimum_eligible_session_seconds",
                MINIMUM_ELIGIBLE_SECONDS,
            )
        )
        enabled = lambda key: "Yes" if values.get(key, "1") == "1" else "No"
        await interaction.response.send_message(
            "**VC tracking/reward-prep settings**\n"
            f"Minimum eligible session: **{format_duration(minimum)}**\n"
            f"Exclude AFK channel: **{enabled('exclude_afk_channel')}**\n"
            f"Exclude alone sessions: **{enabled('exclude_alone_sessions')}**\n"
            f"Exclude self-deafened sessions: "
            f"**{enabled('exclude_self_deafened_sessions')}**",
            ephemeral=True,
        )

    async def _xp_preview_rows(
        self,
        guild: discord.Guild,
        days: int,
        include_left_members: bool = False,
    ) -> List[Tuple[int, str, str, int, int, int, int, int, bool]]:
        await self._sync_xp_states(guild.id)
        period_rows = await self._fetchall(
            """
            SELECT user_id, MAX(username), MAX(display_name),
                   COALESCE(SUM(counted_seconds), 0)
            FROM vc_sessions
            WHERE guild_id = ? AND reward_eligible = 1 AND left_at >= ?
            GROUP BY user_id
            """,
            (guild.id, self._cutoff(days)),
        )
        period_by_user = {
            row[0]: (row[1] or "", row[2] or "", int(row[3] or 0))
            for row in period_rows
        }
        state_rows = await self._fetchall(
            """
            SELECT user_id, eligible_seconds_total, pulses_earned, pulses_paid
            FROM vc_xp_user_state
            WHERE guild_id = ?
            """,
            (guild.id,),
        )
        preview = []
        for user_id, eligible_total, pulses_earned, pulses_paid in state_rows:
            member = current_member(guild, user_id)
            if member is None and not include_left_members:
                continue
            username, display_name, period_seconds = period_by_user.get(
                user_id, ("", "", 0)
            )
            if member:
                username = member.name
                display_name = member.display_name
            unpaid = max(0, int(pulses_earned) - int(pulses_paid))
            if period_seconds <= 0 and unpaid <= 0:
                continue
            preview.append(
                (
                    user_id,
                    username,
                    display_name,
                    period_seconds,
                    int(eligible_total),
                    int(pulses_earned),
                    int(pulses_paid),
                    unpaid,
                    member is not None,
                )
            )
        preview.sort(key=lambda row: (row[7], row[3]), reverse=True)
        return preview

    def _xp_configuration_issue(self, guild: discord.Guild) -> Optional[str]:
        if not self.vcxp_enabled:
            return "VC XP pulses are disabled by `VCXP_ENABLED=false`."
        if not self.vcxp_trigger_role_id:
            return "`VCXP_TRIGGER_ROLE_ID` is not configured."
        if guild.get_role(self.vcxp_trigger_role_id) is None:
            return "The configured VC XP trigger role was not found in this server."
        return None

    @vcrewards.command(name="settings", description="Show VC XP pulse settings")
    @app_commands.guild_only()
    async def rewards_settings(
        self, interaction: discord.Interaction
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        issue = self._xp_configuration_issue(interaction.guild)
        role = (
            interaction.guild.get_role(self.vcxp_trigger_role_id)
            if self.vcxp_trigger_role_id
            else None
        )
        role_label = (
            role.mention
            if role
            else str(self.vcxp_trigger_role_id or "Not configured")
        )
        await interaction.response.send_message(
            "**VC XP role-pulse settings**\n"
            f"Automatic pulses enabled: **{'Yes' if self.vcxp_enabled else 'No'}**\n"
            f"Trigger role: **{role_label}**\n"
            f"Eligible time per pulse: **{self.vcxp_minutes_per_pulse} minutes**\n"
            f"Role removal delay: **{self.vcxp_role_remove_delay_seconds} seconds**\n"
            f"Daily pulse cap: **{self.vcxp_daily_pulse_cap}**\n"
            f"Weekly pulse cap: **{self.vcxp_weekly_pulse_cap}** "
            "(rolling seven days)\n"
            f"Status: {issue or '**Ready**'}\n\n"
            "BroEdenBot only adds and removes the configured role. It does not "
            "call MEE6 or grant MEE6 XP directly.",
            ephemeral=True,
        )

    @vcrewards.command(
        name="audit",
        description="Audit whether the VC XP role-pulse bridge is safe",
    )
    @app_commands.describe(
        include_left_members="Include users who are no longer in the server",
    )
    @app_commands.guild_only()
    async def rewards_audit(
        self,
        interaction: discord.Interaction,
        include_left_members: bool = False,
    ) -> None:
        if not self._has_rewards_access(interaction):
            await interaction.response.send_message(
                "You do not have permission to audit VC rewards.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        role = (
            guild.get_role(self.vcxp_trigger_role_id)
            if self.vcxp_trigger_role_id
            else None
        )
        bot_member = guild.me
        manage_roles = bool(
            bot_member and bot_member.guild_permissions.manage_roles
        )
        hierarchy_ok = bool(role and bot_member and bot_member.top_role > role)
        role_usable = bool(role and not role.managed and hierarchy_ok and manage_roles)

        state_exists = await self._table_exists("vc_xp_user_state")
        pulses_exists = await self._table_exists("vc_xp_pulses")
        unpaid_users = 0
        paid_last_day = 0
        recent_rows = []
        if state_exists:
            rows = await self._fetchall(
                """
                SELECT user_id
                FROM vc_xp_user_state
                WHERE guild_id = ? AND pulses_earned > pulses_paid
                """,
                (guild.id,),
            )
            unpaid_users = sum(
                1
                for (user_id,) in rows
                if include_left_members or current_member(guild, user_id)
            )
        if pulses_exists:
            day_ago = (utc_now() - timedelta(hours=24)).isoformat()
            rows = await self._fetchall(
                """
                SELECT COUNT(*)
                FROM vc_xp_pulses
                WHERE guild_id = ? AND granted_at >= ?
                  AND status IN (
                      'paid', 'remove_failed_assumed_paid',
                      'stale_assumed_paid', 'marked_paid'
                  )
                """,
                (guild.id, day_ago),
            )
            paid_last_day = int(rows[0][0] or 0)
            recent_rows = await self._fetchall(
                """
                SELECT user_id, status, error, granted_at
                FROM vc_xp_pulses
                WHERE guild_id = ?
                ORDER BY granted_at DESC, id DESC
                LIMIT 5
                """,
                (guild.id,),
            )

        stuck_members = []
        if role:
            stuck_members = [member for member in role.members if not member.bot]

        def label(passed: bool, *, warning: bool = False) -> str:
            if passed:
                return "✅ PASS"
            return "⚠️ WARN" if warning else "❌ FAIL"

        embed = discord.Embed(
            title="VC rewards safety audit",
            color=discord.Color.green() if role_usable else discord.Color.orange(),
            timestamp=utc_now(),
            description=(
                "Read-only audit. No roles, payouts, or VCXP state were changed."
            ),
        )
        embed.add_field(
            name="Configuration",
            value=(
                f"{label(self.vcxp_enabled, warning=True)} `VCXP_ENABLED`: "
                f"**{'true' if self.vcxp_enabled else 'false'}**\n"
                f"{label(bool(self.vcxp_trigger_role_id))} Trigger role configured\n"
                f"{label(bool(role))} Trigger role found\n"
                f"{label(manage_roles)} Bot has Manage Roles\n"
                f"{label(hierarchy_ok)} Bot role is above trigger role"
            ),
            inline=False,
        )
        embed.add_field(
            name="Pulse limits",
            value=(
                f"Minutes per pulse: **{self.vcxp_minutes_per_pulse}**\n"
                f"Role removal delay: **{self.vcxp_role_remove_delay_seconds}s**\n"
                f"Daily cap: **{self.vcxp_daily_pulse_cap}**\n"
                f"Weekly cap: **{self.vcxp_weekly_pulse_cap}**"
            ),
            inline=True,
        )
        state_text = (
            f"Users with unpaid pulses "
            f"({'includes left members' if include_left_members else 'current members only'}): "
            f"**{unpaid_users:,}**\n"
            f"Pulses paid in last 24h: **{paid_last_day:,}**"
            if state_exists and pulses_exists
            else "VCXP state tables not found yet."
        )
        embed.add_field(name="Accounting", value=state_text, inline=True)
        warning = member_filter_warning(self.bot, guild)
        if warning and not include_left_members:
            embed.add_field(
                name="Member filtering",
                value=f"⚠️ {warning}",
                inline=False,
            )
        embed.add_field(
            name=f"Members currently holding trigger role ({len(stuck_members)})",
            value=(
                "\n".join(
                    f"<@{member.id}>" for member in stuck_members[:10]
                )
                + (
                    f"\n…and {len(stuck_members) - 10} more."
                    if len(stuck_members) > 10
                    else ""
                )
                if stuck_members
                else "None found."
            )[:1024],
            inline=False,
        )
        if recent_rows:
            embed.add_field(
                name="Most recent pulse statuses",
                value="\n".join(
                    f"<@{user_id}> — **{status}**"
                    f"{f' ({error})' if error else ''} — `{granted_at}`"
                    for user_id, status, error, granted_at in recent_rows
                )[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Most recent pulse statuses",
                value="No pulse records found.",
                inline=False,
            )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @vcrewards.command(
        name="preview", description="Preview eligible time and VC XP pulses"
    )
    @app_commands.describe(
        days="Lookback period for displayed eligible time",
        include_left_members="Include users who are no longer in the server",
    )
    @app_commands.guild_only()
    async def rewards_preview(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 7,
        include_left_members: bool = False,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._xp_preview_rows(
            interaction.guild,
            days,
            include_left_members,
        )
        if not rows:
            await interaction.followup.send(
                "No eligible VC time or unpaid VC XP pulses were found.",
                ephemeral=True,
            )
            return
        lines = [
            f"{index}. "
            f"{f'<@{row[0]}>' if row[8] else discord.utils.escape_markdown(row[2] or row[1] or str(row[0])) + ' — Left server'} "
            f"— {format_duration(row[3])} eligible "
            f"in period — earned **{row[5]}** / paid **{row[6]}** / "
            f"unpaid **{row[7]}**"
            for index, row in enumerate(rows[:25], 1)
        ]
        if len(rows) > 25:
            lines.append(f"…and {len(rows) - 25} more members.")
        warning = member_filter_warning(self.bot, interaction.guild)
        if warning and not include_left_members:
            lines.append(f"⚠️ {warning}")
        await self._send_lines(
            interaction,
            f"**VC XP pulse preview — last {days} days**\n"
            "Earned, paid, and unpaid counts are cumulative.\n"
            f"{'Includes left members' if include_left_members else 'Current members only'}.",
            lines,
        )

    @vcrewards.command(name="unpaid", description="Show unpaid VC XP pulses")
    @app_commands.describe(
        include_left_members="Include users who are no longer in the server",
    )
    @app_commands.guild_only()
    async def rewards_unpaid(
        self,
        interaction: discord.Interaction,
        include_left_members: bool = False,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._sync_xp_states(interaction.guild_id)
        rows = await self._fetchall(
            """
            SELECT user_id, eligible_seconds_total, pulses_earned, pulses_paid,
                   (pulses_earned - pulses_paid) AS unpaid
            FROM vc_xp_user_state
            WHERE guild_id = ? AND pulses_earned > pulses_paid
            ORDER BY unpaid DESC, eligible_seconds_total DESC
            """,
            (interaction.guild_id,),
        )
        filtered_rows = []
        for row in rows:
            member = current_member(interaction.guild, row[0])
            if member is None and not include_left_members:
                continue
            filtered_rows.append((*row, member))
            if len(filtered_rows) >= 75:
                break
        if not filtered_rows:
            await interaction.followup.send(
                "No members currently have unpaid VC XP pulses.",
                ephemeral=True,
            )
            return
        lines = [
            f"{index}. "
            f"{f'<@{row[0]}>' if row[5] else f'`{row[0]}` — Left server'} "
            f"— {format_duration(row[1])} eligible "
            f"— earned **{row[2]}** / paid **{row[3]}** / unpaid **{row[4]}**"
            for index, row in enumerate(filtered_rows, 1)
        ]
        warning = member_filter_warning(self.bot, interaction.guild)
        if warning and not include_left_members:
            lines.append(f"⚠️ {warning}")
        await self._send_lines(
            interaction,
            "**Unpaid VC XP pulses**\n"
            f"{'Includes left members' if include_left_members else 'Current members only'}.",
            lines,
        )

    @vcrewards.command(
        name="pulse", description="Manually test the VC XP trigger role"
    )
    @app_commands.describe(
        user="Member who should receive the test role pulse",
        pulses="Number of sequential test pulses",
    )
    @app_commands.guild_only()
    async def rewards_pulse(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        pulses: app_commands.Range[int, 1, 10] = 1,
    ) -> None:
        if not self._is_administrator(interaction):
            await interaction.response.send_message(
                "Only administrators can manually trigger VC XP pulses.",
                ephemeral=True,
            )
            return
        issue = self._xp_configuration_issue(interaction.guild)
        if issue:
            await interaction.response.send_message(issue, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        role = interaction.guild.get_role(self.vcxp_trigger_role_id)
        completed = 0
        messages = []
        for _ in range(pulses):
            succeeded, message = await self._pay_one_pulse(
                user, role, automatic=False
            )
            messages.append(message)
            if not succeeded:
                break
            completed += 1
        await interaction.followup.send(
            f"Completed **{completed} of {pulses}** requested VC XP role pulses "
            f"for {user.mention}.\n{messages[-1]}",
            ephemeral=True,
        )

    @vcrewards.command(
        name="markpaid", description="Mark VC XP pulses paid without adding a role"
    )
    @app_commands.describe(
        user="Member whose accounting should be updated",
        pulses="Number of pulses to mark paid",
    )
    @app_commands.guild_only()
    async def rewards_markpaid(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        pulses: app_commands.Range[int, 1, 100],
    ) -> None:
        if not self._is_administrator(interaction):
            await interaction.response.send_message(
                "Only administrators can mark VC XP pulses paid.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        eligible_seconds, _, pulses_paid = await self._sync_xp_user_state(
            interaction.guild_id, user.id
        )
        now = utc_now().isoformat()
        role_id = self.vcxp_trigger_role_id or 0
        await self.bot.db.executemany(
            """
            INSERT INTO vc_xp_pulses (
                guild_id, user_id, eligible_seconds_snapshot, pulse_number,
                role_id, granted_at, removed_at, status, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'marked_paid', ?)
            """,
            [
                (
                    interaction.guild_id,
                    user.id,
                    eligible_seconds,
                    pulses_paid + index,
                    role_id,
                    now,
                    now,
                    f"Manually marked paid by administrator {interaction.user.id}",
                )
                for index in range(1, pulses + 1)
            ],
        )
        await self.bot.db.execute(
            """
            UPDATE vc_xp_user_state
            SET pulses_paid = pulses_paid + ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (pulses, now, interaction.guild_id, user.id),
        )
        await self.bot.db.commit()
        logger.info(
            "VC XP pulses marked paid: guild=%s user=%s pulses=%s admin=%s",
            interaction.guild_id,
            user.id,
            pulses,
            interaction.user.id,
        )
        await interaction.followup.send(
            f"Marked **{pulses}** VC XP pulses paid for {user.mention} without "
            "adding the trigger role.",
            ephemeral=True,
        )

    @vcrewards.command(
        name="export", description="Export the VC XP pulse preview to CSV"
    )
    @app_commands.describe(
        days="Lookback period for eligible VC time",
        include_left_members="Include users who are no longer in the server",
    )
    @app_commands.guild_only()
    async def rewards_export(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 7,
        include_left_members: bool = False,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._xp_preview_rows(
            interaction.guild,
            days,
            include_left_members,
        )
        export_rows = [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                format_duration(row[3]),
                row[4],
                format_duration(row[4]),
                row[5],
                row[6],
                row[7],
                row[8],
                days,
                self.vcxp_minutes_per_pulse,
            )
            for row in rows
        ]
        headers = [
            "user_id",
            "username",
            "display_name",
            "period_eligible_seconds",
            "period_eligible_readable",
            "total_eligible_seconds",
            "total_eligible_readable",
            "pulses_earned",
            "pulses_paid",
            "unpaid_pulses",
            "is_current_member",
            "period_days",
            "minutes_per_pulse",
        ]
        file = self._csv_file(
            f"vc_xp_pulse_preview_{days}d.csv", headers, export_rows
        )
        if file is None:
            await interaction.followup.send(
                "That export is too large for Discord. Use a shorter date range.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Exported VC XP pulse data for **{len(rows)}** members."
            + (
                f"\n⚠️ {member_filter_warning(self.bot, interaction.guild)}"
                if not include_left_members
                and member_filter_warning(self.bot, interaction.guild)
                else ""
            ),
            file=file,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VCStats(bot))
