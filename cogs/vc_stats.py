import asyncio
import csv
import io
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks


logger = logging.getLogger(__name__)

MINIMUM_ELIGIBLE_SECONDS = 5 * 60
HEARTBEAT_SECONDS = 60
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


class VCStats(commands.Cog):
    vcstats = app_commands.Group(
        name="vcstats",
        description="Private voice-channel activity statistics",
    )
    vcrewards = app_commands.Group(
        name="vcrewards",
        description="Preview future voice-channel rewards",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.allowed_role_ids = self._parse_allowed_role_ids(
            os.getenv("VCSTATS_ALLOWED_ROLE_IDS", "")
        )
        self._tracking_lock = asyncio.Lock()
        self._last_startup_reconcile = 0.0

    async def cog_load(self) -> None:
        await self._create_tables()
        self._heartbeat.start()

    async def cog_unload(self) -> None:
        self._heartbeat.cancel()

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

    @staticmethod
    def _cutoff(days: int) -> str:
        return (utc_now() - timedelta(days=days)).isoformat()

    async def _fetchall(self, query: str, parameters: Iterable = ()) -> list:
        cursor = await self.bot.db.execute(query, tuple(parameters))
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

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
    )
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 30,
        limit: app_commands.Range[int, 1, 25] = 10,
        eligible_only: bool = False,
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
            LIMIT ?
            """,
            (interaction.guild_id, self._cutoff(days), limit),
        )
        if not rows:
            await interaction.followup.send(
                "No matching VC sessions were found.", ephemeral=True
            )
            return
        label = "eligible time" if eligible_only else "tracked time"
        lines = [
            f"{index}. <@{row[0]}> — **{format_duration(row[3])}**"
            for index, row in enumerate(rows, 1)
        ]
        await self._send_lines(
            interaction,
            f"**VC leaderboard — {label}, last {days} days**",
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
    )
    @app_commands.guild_only()
    async def export_sessions(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 30,
        user: Optional[discord.Member] = None,
        channel: Optional[discord.VoiceChannel] = None,
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
        export_rows = [
            (
                *row[:8],
                format_duration(row[7]),
                row[8],
                format_duration(row[8]),
                *row[9:],
            )
            for row in rows
        ]
        headers = [
            "user_id",
            "username",
            "display_name",
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
            f"Exported **{len(rows)}** matching VC sessions.",
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
            "Reward snapshot tables were not changed.",
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

    async def _reward_preview_rows(
        self,
        guild_id: int,
        days: int,
        minutes_per_point: int,
        daily_cap_minutes: int,
    ) -> List[Tuple[int, str, str, int, int]]:
        cutoff = utc_now() - timedelta(days=days)
        rows = await self._fetchall(
            """
            SELECT user_id, username, display_name, joined_at, left_at,
                   duration_seconds, counted_seconds
            FROM vc_sessions
            WHERE guild_id = ? AND reward_eligible = 1
              AND counted_seconds > 0 AND left_at >= ?
            ORDER BY joined_at
            """,
            (guild_id, cutoff.isoformat()),
        )
        per_user_day: Dict[int, Dict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        names: Dict[int, Tuple[str, str]] = {}
        for (
            user_id,
            username,
            display_name,
            joined_raw,
            left_raw,
            duration_seconds,
            counted_seconds,
        ) in rows:
            names[user_id] = (username, display_name)
            joined = max(parse_timestamp(joined_raw), cutoff)
            left = parse_timestamp(left_raw)
            if left <= joined or duration_seconds <= 0:
                continue
            cursor = joined
            while cursor < left:
                next_day = datetime.combine(
                    cursor.date() + timedelta(days=1),
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                )
                segment_end = min(left, next_day)
                segment = (segment_end - cursor).total_seconds()
                allocated = counted_seconds * (segment / duration_seconds)
                per_user_day[user_id][cursor.date().isoformat()] += allocated
                cursor = segment_end

        cap_seconds = daily_cap_minutes * 60
        point_seconds = minutes_per_point * 60
        preview = []
        for user_id, daily_values in per_user_day.items():
            eligible_seconds = int(
                sum(min(value, cap_seconds) for value in daily_values.values())
            )
            points = eligible_seconds // point_seconds
            username, display_name = names[user_id]
            preview.append(
                (user_id, username, display_name, eligible_seconds, points)
            )
        preview.sort(key=lambda row: (row[4], row[3]), reverse=True)
        return preview

    @vcrewards.command(
        name="preview", description="Preview future VC reward points"
    )
    @app_commands.describe(
        days="Lookback period in days",
        minutes_per_point="Eligible minutes required per point",
        daily_cap_minutes="Maximum eligible minutes per member per UTC day",
    )
    @app_commands.guild_only()
    async def rewards_preview(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 7,
        minutes_per_point: app_commands.Range[int, 1, 10080] = 60,
        daily_cap_minutes: app_commands.Range[int, 1, 10080] = 180,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._reward_preview_rows(
            interaction.guild_id,
            days,
            minutes_per_point,
            daily_cap_minutes,
        )
        if not rows:
            await interaction.followup.send(
                "No reward-eligible VC time was found for that period.",
                ephemeral=True,
            )
            return
        lines = [
            f"{index}. <@{row[0]}> — {format_duration(row[3])} eligible "
            f"— **{row[4]} points**"
            for index, row in enumerate(rows[:25], 1)
        ]
        if len(rows) > 25:
            lines.append(f"…and {len(rows) - 25} more members.")
        await self._send_lines(
            interaction,
            f"**VC reward preview — last {days} days**\n"
            f"{minutes_per_point} min/point, {daily_cap_minutes} min daily cap",
            lines,
        )

    @vcrewards.command(
        name="export", description="Export the VC reward preview to CSV"
    )
    @app_commands.describe(
        days="Lookback period in days",
        minutes_per_point="Eligible minutes required per point",
        daily_cap_minutes="Maximum eligible minutes per member per UTC day",
    )
    @app_commands.guild_only()
    async def rewards_export(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 3650] = 7,
        minutes_per_point: app_commands.Range[int, 1, 10080] = 60,
        daily_cap_minutes: app_commands.Range[int, 1, 10080] = 180,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self._reward_preview_rows(
            interaction.guild_id,
            days,
            minutes_per_point,
            daily_cap_minutes,
        )
        export_rows = [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                format_duration(row[3]),
                row[4],
                days,
                minutes_per_point,
                daily_cap_minutes,
            )
            for row in rows
        ]
        headers = [
            "user_id",
            "username",
            "display_name",
            "eligible_seconds",
            "eligible_readable",
            "estimated_reward_points",
            "period_days",
            "minutes_per_point",
            "daily_cap_minutes",
        ]
        file = self._csv_file(
            f"vc_reward_preview_{days}d.csv", headers, export_rows
        )
        if file is None:
            await interaction.followup.send(
                "That export is too large for Discord. Use a shorter date range.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Exported reward previews for **{len(rows)}** members. "
            "No rewards were granted.",
            file=file,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VCStats(bot))
