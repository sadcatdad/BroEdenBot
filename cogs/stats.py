import asyncio
import csv
import datetime
import io
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import COLOR
from utils.compact_roster import CompactRosterItem, render_compact_roster_pngs
from utils.member_filter import current_member, member_filter_warning
from utils.knowledge_manager import process_knowledge_reindex
from utils.ranked_graphic import (
    RankedGraphicItem,
    RankedGraphicSection,
    render_ranked_graphic,
)
from utils.settings import get_csv_ids_setting
from utils.exclusions import member_is_excluded
from utils.stats_reports import (
    render_missingrole_report,
    render_report_error,
    render_rolecompare_report,
)
from utils.stats_manager import (
    complete_action,
    get_stat,
    initialize_stats_manager_schema,
    mark_action_processing,
    parse_stat_id,
    pending_dashboard_actions,
    replace_member_snapshot,
    update_stat_result,
)


PERMISSION_DENIED_MESSAGE = "You do not have permission to use stats commands."
DEBOUNCE_SECONDS = 2.0
MAX_ACTIVITY_EXPORT_BYTES = 24 * 1024 * 1024
ACTIVITY_SOURCE_CHOICES = [
    app_commands.Choice(name="All", value="all"),
    app_commands.Choice(name="Live", value="live"),
    app_commands.Choice(name="Imported", value="imported"),
]
ACTIVITY_PERIOD_CHOICES = [
    app_commands.Choice(name="7 days", value="7"),
    app_commands.Choice(name="30 days", value="30"),
    app_commands.Choice(name="90 days", value="90"),
    app_commands.Choice(name="365 days", value="365"),
    app_commands.Choice(name="All time", value="all_time"),
]
ACTIVITY_FIXED_PERIOD_CHOICES = ACTIVITY_PERIOD_CHOICES[:-1]
ACTIVITY_GRAPHIC_REPORTS = {"channels", "quiet", "members", "vc"}
CHANNEL_CATEGORIES_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "channel_categories.json"
)
logger = logging.getLogger(__name__)


def load_channel_categories() -> Dict[str, dict]:
    try:
        data = json.loads(CHANNEL_CATEGORIES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_channel_category(
    channel_id: int,
    categories: Optional[Dict[str, dict]] = None,
) -> Tuple[str, bool]:
    entries = categories if categories is not None else load_channel_categories()
    entry = entries.get(str(channel_id), {})
    if not isinstance(entry, dict):
        return "Uncategorized", True
    category = str(entry.get("category") or "Uncategorized").strip()
    return category or "Uncategorized", entry.get("include_in_activity") is not False


def _percent_change(current: int, previous: int) -> str:
    if previous == 0:
        return "N/A" if current else "0.0%"
    change = (current - previous) / previous * 100
    return f"{change:+.1f}%"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
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


def safe_channel_display(channel_id: int, channel_name: Optional[str]) -> str:
    if channel_id:
        return f"<#{channel_id}>"
    return discord.utils.escape_markdown(channel_name or "Unknown channel")


def allowed_stats_role_ids() -> Set[int]:
    return set(get_csv_ids_setting("STATS_ALLOWED_ROLE_IDS"))


async def has_stats_access(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    if interaction.user.guild_permissions.administrator:
        return True

    permitted_roles = allowed_stats_role_ids()
    return any(role.id in permitted_roles for role in interaction.user.roles)


async def has_stats_delete_access(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


class RoleStatsModal(discord.ui.Modal):
    def __init__(
        self,
        cog,
        role: discord.Role,
        channel,
        image: Optional[discord.Attachment],
    ):
        super().__init__(title="Create role roster")
        self.cog = cog
        self.role = role
        self.channel = channel
        self.image = image
        self.header = discord.ui.TextInput(
            label="Header",
            placeholder=f"{role.name} Members",
            style=discord.TextStyle.short,
            required=False,
            max_length=100,
        )
        self.body = discord.ui.TextInput(
            label="Body",
            placeholder=(
                "Add supporting text. Line breaks are preserved; "
                "basic Markdown markers are removed."
            ),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.header)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._create_role_embed(
            interaction=interaction,
            role=self.role,
            title=str(self.header.value or "").strip() or f"{self.role.name} Members",
            body=str(self.body.value or ""),
            target_channel=self.channel,
            image=self.image,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        message = "The stats embed could not be created."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class StatsDeleteSelect(discord.ui.Select):
    def __init__(self, cog, options):
        self.cog = cog
        super().__init__(
            placeholder="Choose a tracked stats page to delete",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await has_stats_delete_access(interaction):
            await interaction.response.send_message(
                "Only administrators can use /stats delete.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        summary = await self.cog._delete_tracked_pages(
            interaction.guild,
            self.values[0],
        )
        for item in self.view.children:
            item.disabled = True
        await interaction.edit_original_response(view=self.view)
        await interaction.followup.send(summary, ephemeral=True)


class StatsDeleteView(discord.ui.View):
    def __init__(self, cog, options):
        super().__init__(timeout=120)
        self.add_item(StatsDeleteSelect(cog, options))


class StatsExportView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Export Members to CSV",
        style=discord.ButtonStyle.secondary,
        emoji="📄",
        custom_id="stats:export_members_csv",
    )
    async def export_csv(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await has_stats_access(interaction):
            await interaction.response.send_message(
                PERMISSION_DENIED_MESSAGE,
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        file = await self.cog._report_csv_file(
            interaction.guild,
            interaction.message.id,
        )
        if file is None:
            await interaction.followup.send(
                "This tracked stats report could not be found.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file, ephemeral=True)


class Stats(commands.Cog):
    stats = app_commands.Group(
        name="stats",
        description="Create and manage live role membership embeds",
    )
    activity = app_commands.Group(
        name="activity",
        description="Community activity reports",
        parent=stats,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._export_view = StatsExportView(self)

    @property
    def activity_excluded_user_ids(self) -> Set[int]:
        return set(get_csv_ids_setting("ACTIVITY_EXCLUDED_USER_IDS"))

    @property
    def activity_excluded_role_ids(self) -> Set[int]:
        return set(get_csv_ids_setting("ACTIVITY_EXCLUDED_ROLE_IDS"))

    @property
    def vc_excluded_user_ids(self) -> Set[int]:
        return set(get_csv_ids_setting("VC_EXCLUDED_USER_IDS"))

    @property
    def vc_excluded_role_ids(self) -> Set[int]:
        return set(get_csv_ids_setting("VC_EXCLUDED_ROLE_IDS"))

    def _activity_excluded_ids_for_guild(
        self,
        guild: Optional[discord.Guild],
    ) -> Set[int]:
        excluded = set(self.activity_excluded_user_ids)
        role_ids = self.activity_excluded_role_ids
        if guild and role_ids:
            for member in guild.members:
                if member_is_excluded(
                    member,
                    user_ids=(),
                    role_ids=role_ids,
                ):
                    excluded.add(member.id)
        return excluded

    def _vc_excluded_ids_for_guild(
        self,
        guild: Optional[discord.Guild],
    ) -> Set[int]:
        excluded = set(self.vc_excluded_user_ids)
        role_ids = self.vc_excluded_role_ids
        if guild and role_ids:
            for member in guild.members:
                if member_is_excluded(member, user_ids=(), role_ids=role_ids):
                    excluded.add(member.id)
        return excluded

    @staticmethod
    def _excluded_user_sql(user_ids: Iterable[int]) -> Tuple[str, Tuple[int, ...]]:
        ids = tuple(sorted({int(user_id) for user_id in user_ids if user_id}))
        if not ids:
            return "", ()
        placeholders = ", ".join("?" for _ in ids)
        return f"AND user_id NOT IN ({placeholders})", ids

    def _activity_member_excluded(self, member: Optional[discord.Member]) -> bool:
        return bool(
            member
            and member_is_excluded(
                member,
                user_ids=self.activity_excluded_user_ids,
                role_ids=self.activity_excluded_role_ids,
            )
        )

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS role_stat_embeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                image_url TEXT,
                image_data BLOB,
                graphic_enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_stats_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                report_type TEXT NOT NULL,
                role_1_id INTEGER,
                role_2_id INTEGER,
                has_role_id INTEGER,
                missing_role_id INTEGER,
                title TEXT,
                body TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_message_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                activity_date TEXT NOT NULL,
                activity_hour TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                source TEXT DEFAULT 'live',
                imported_at TEXT,
                import_batch_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_member_joins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                joined_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_member_leaves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT,
                username TEXT,
                left_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_activity_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO stats_activity_settings (key, value)
            VALUES ('activity_tracking_started_at', ?)
            """,
            (self._utcnow().isoformat(),),
        )
        cursor = await self.bot.db.execute("PRAGMA table_info(role_stat_embeds)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "image_url" not in columns:
            await self.bot.db.execute(
                "ALTER TABLE role_stat_embeds ADD COLUMN image_url TEXT"
            )
        if "image_data" not in columns:
            await self.bot.db.execute(
                "ALTER TABLE role_stat_embeds ADD COLUMN image_data BLOB"
            )
        if "graphic_enabled" not in columns:
            await self.bot.db.execute(
                """
                ALTER TABLE role_stat_embeds
                ADD COLUMN graphic_enabled INTEGER NOT NULL DEFAULT 1
                """
            )
        cursor = await self.bot.db.execute(
            "PRAGMA table_info(stats_message_activity)"
        )
        activity_columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for column_name, definition in (
            ("source", "TEXT DEFAULT 'live'"),
            ("imported_at", "TEXT"),
            ("import_batch_id", "TEXT"),
        ):
            if column_name not in activity_columns:
                await self.bot.db.execute(
                    f"""
                    ALTER TABLE stats_message_activity
                    ADD COLUMN {column_name} {definition}
                    """
                )
        await self.bot.db.execute(
            """
            UPDATE stats_message_activity
            SET source = COALESCE(NULLIF(source, ''), 'live'),
                import_batch_id = CASE
                    WHEN COALESCE(NULLIF(source, ''), 'live') = 'live'
                    THEN COALESCE(import_batch_id, 'live')
                    ELSE import_batch_id
                END
            """
        )
        await self.bot.db.execute(
            "DROP INDEX IF EXISTS idx_stats_message_activity_hour"
        )
        await self.bot.db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stats_message_activity_bucket
            ON stats_message_activity (
                guild_id, channel_id, user_id, activity_hour, source,
                import_batch_id
            )
            """
        )
        for index_name, columns_sql in (
            ("idx_stats_message_activity_guild_date", "guild_id, activity_date"),
            (
                "idx_stats_message_activity_channel_date",
                "guild_id, channel_id, activity_date",
            ),
            (
                "idx_stats_message_activity_user_date",
                "guild_id, user_id, activity_date",
            ),
            (
                "idx_stats_message_activity_source_date",
                "guild_id, source, activity_date",
            ),
        ):
            await self.bot.db.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {index_name}
                ON stats_message_activity ({columns_sql})
                """
            )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_activity_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                import_batch_id TEXT NOT NULL,
                filename TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                imported_by INTEGER,
                imported_at TEXT NOT NULL,
                messages_seen INTEGER DEFAULT 0,
                messages_imported INTEGER DEFAULT 0,
                messages_skipped INTEGER DEFAULT 0,
                duplicates_skipped INTEGER DEFAULT 0,
                earliest_message_at TEXT,
                latest_message_at TEXT,
                status TEXT DEFAULT 'completed',
                notes TEXT,
                source_file TEXT,
                source_format TEXT,
                imported_for_activity INTEGER DEFAULT 1,
                imported_for_context INTEGER DEFAULT 0
            )
            """
        )
        cursor = await self.bot.db.execute(
            "PRAGMA table_info(stats_activity_imports)"
        )
        import_columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for name, definition in (
            ("source_file", "TEXT"),
            ("source_format", "TEXT"),
            ("imported_for_activity", "INTEGER DEFAULT 1"),
            ("imported_for_context", "INTEGER DEFAULT 0"),
        ):
            if name not in import_columns:
                await self.bot.db.execute(
                    f"ALTER TABLE stats_activity_imports "
                    f"ADD COLUMN {name} {definition}"
                )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stats_activity_imports_guild_date
            ON stats_activity_imports (guild_id, imported_at)
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS stats_activity_imported_messages (
                guild_id INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                import_batch_id TEXT,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, message_id)
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_activity_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                report_type TEXT NOT NULL,
                config_json TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_activity_message
            ON tracked_activity_reports (guild_id, message_id)
            """
        )
        await self._ensure_dashboard_manager_columns()
        await self.bot.db.commit()
        initialize_stats_manager_schema()
        self.bot.add_view(self._export_view)
        if not self.daily_stats_refresh.is_running():
            self.daily_stats_refresh.start()
        if not self.dashboard_action_worker.is_running():
            self.dashboard_action_worker.start()

    async def cog_unload(self) -> None:
        self.daily_stats_refresh.cancel()
        self.dashboard_action_worker.cancel()
        for task in self._refresh_tasks.values():
            task.cancel()
        self._refresh_tasks.clear()

    async def _ensure_dashboard_manager_columns(self) -> None:
        for table in (
            "role_stat_embeds",
            "tracked_stats_reports",
            "tracked_activity_reports",
        ):
            cursor = await self.bot.db.execute(f'PRAGMA table_info("{table}")')
            columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            if "status" not in columns:
                await self.bot.db.execute(
                    f"""
                    ALTER TABLE "{table}"
                    ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
                    """
                )
            if "last_error" not in columns:
                await self.bot.db.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN last_error TEXT'
                )

    @stats.command(name="role", description="Create a live role roster graphic")
    @app_commands.describe(
        role="Role whose current members should be listed",
        channel="Channel where the embed should be sent",
        image="Optional banner image rendered into the roster card",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        channel: Optional[discord.TextChannel] = None,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if target_channel is None or not hasattr(target_channel, "send"):
            await interaction.response.send_message(
                "I cannot send a stats embed in that channel.", ephemeral=True
            )
            return

        target_guild = getattr(target_channel, "guild", None)
        if target_guild is None or target_guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "The stats embed must be sent in this server.", ephemeral=True
            )
            return

        if image is not None and not self._is_image_attachment(image):
            await interaction.response.send_message(
                "The image option must be an image attachment.", ephemeral=True
            )
            return
        if image is not None and image.size > 8_000_000:
            await interaction.response.send_message(
                "The banner image must be 8 MB or smaller.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            RoleStatsModal(
                cog=self,
                role=role,
                channel=target_channel,
                image=image,
            )
        )

    async def _create_role_embed(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        title: Optional[str],
        body: str,
        target_channel,
        image: Optional[discord.Attachment],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        now = self._utcnow()
        image_url = None
        image_data = None

        if image is not None:
            try:
                image_data = await image.read()
                image_url = image.url
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "I could not download the selected image.", ephemeral=True
                )
                return

        try:
            roster_files = await self._build_roster_files(
                role,
                title,
                body,
                now,
                image_data,
            )
        except Exception:
            await interaction.followup.send(
                "I could not generate the roster graphic.", ephemeral=True
            )
            return
        try:
            message = await target_channel.send(
                files=roster_files,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not send the stats embed in that channel. "
                "Please check my channel permissions.",
                ephemeral=True,
            )
            return

        timestamp = now.isoformat()
        try:
            await self.bot.db.execute(
                """
                INSERT INTO role_stat_embeds (
                    guild_id,
                    channel_id,
                    message_id,
                    role_id,
                    title,
                    body,
                    image_url,
                    image_data,
                    graphic_enabled,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.guild_id,
                    target_channel.id,
                    message.id,
                    role.id,
                    title or "",
                    body,
                    image_url,
                    image_data,
                    1,
                    interaction.user.id,
                    timestamp,
                    timestamp,
                ),
            )
            await self.bot.db.commit()
        except Exception:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            raise

        await interaction.followup.send(
            f"Created a tracked stats embed for {role.mention} in "
            f"{target_channel.mention}.",
            ephemeral=True,
        )

    @stats.command(
        name="refresh",
        description="Refresh every tracked stats page in this server",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self._tracked_rows(guild_id=interaction.guild_id)
        report_rows = await self._tracked_report_rows(
            guild_id=interaction.guild_id
        )
        activity_rows = await self._tracked_activity_rows(
            guild_id=interaction.guild_id
        )

        refreshed = 0
        failed = 0
        for row in rows:
            if await self._refresh_row(row):
                refreshed += 1
            else:
                failed += 1
        for row in report_rows:
            if await self._refresh_report_row(row):
                refreshed += 1
            else:
                failed += 1
        for row in activity_rows:
            if await self._refresh_activity_report_row(row):
                refreshed += 1
            else:
                failed += 1

        total = len(rows) + len(report_rows) + len(activity_rows)
        if not total:
            message = "There are no tracked stats pages in this server."
        else:
            message = f"Refreshed {refreshed} tracked stats page(s)."
            if failed:
                message += f" {failed} could not be refreshed."

        await interaction.followup.send(message, ephemeral=True)

    @activity.command(
        name="overview",
        description="Show a community activity overview",
    )
    @app_commands.describe(
        period="Preset reporting period; takes priority over days",
        days="Number of days to include",
        source="Include all, live, or imported message activity",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_overview(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        days: Optional[app_commands.Range[int, 1, 3650]] = None,
        source: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        selected_days = self._activity_period_days(period, days, default_days=7)
        source_value = self._activity_source_value(source)
        config = {"days": selected_days, "source": source_value}
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "overview",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="overview",
            config=config,
        )

    @activity.command(
        name="channels",
        description="Show top text channels by message activity",
    )
    @app_commands.describe(
        period="Preset reporting period; takes priority over days",
        days="Number of days to include",
        limit="Number of channels to show",
        source="Include all, live, or imported message activity",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_channels(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        days: Optional[app_commands.Range[int, 1, 3650]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
        source: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_days = self._activity_period_days(period, days, default_days=7)
        source_value = self._activity_source_value(source)
        config = {
            "days": selected_days,
            "source": source_value,
            "limit": limit,
        }
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "channels",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="channels",
            config=config,
        )

    @activity.command(
        name="quiet",
        description="Show visible text channels with low activity",
    )
    @app_commands.describe(
        period="Preset reporting period; takes priority over days",
        days="Number of days to include",
        limit="Number of channels to show",
        source="Include all, live, or imported message activity",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_quiet(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        days: Optional[app_commands.Range[int, 1, 3650]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
        source: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_days = self._activity_period_days(period, days, default_days=14)
        source_value = self._activity_source_value(source)
        config = {
            "days": selected_days,
            "source": source_value,
            "limit": limit,
        }
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "quiet",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="quiet",
            config=config,
        )

    @activity.command(
        name="members",
        description="Show members with the most tracked text activity",
    )
    @app_commands.describe(
        period="Preset reporting period; takes priority over days",
        days="Number of days to include",
        limit="Number of members to show",
        source="Include all, live, or imported message activity",
        include_left_members="Include users who are no longer in the server",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_members(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        days: Optional[app_commands.Range[int, 1, 3650]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
        source: Optional[app_commands.Choice[str]] = None,
        include_left_members: bool = False,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_days = self._activity_period_days(period, days, default_days=7)
        source_value = self._activity_source_value(source)
        config = {
            "days": selected_days,
            "source": source_value,
            "limit": limit,
            "include_left_members": include_left_members,
        }
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "members",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="members",
            config=config,
        )

    @activity.command(
        name="trends",
        description="Compare activity with the immediately previous period",
    )
    @app_commands.describe(
        period="Fixed period to compare",
        source="Include all, live, or imported message activity",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_FIXED_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_trends(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        source: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        days = int(period.value) if period else 30
        source_value = self._activity_source_value(source)
        config = {"days": days, "source": source_value}
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "trends",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="trends",
            config=config,
        )

    @activity.command(
        name="categories",
        description="Group message activity by configured channel category",
    )
    @app_commands.describe(
        period="Preset reporting period",
        source="Include all, live, or imported message activity",
        limit="Number of categories to show",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_categories(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        source: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 25] = 10,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_days = self._activity_period_days(period, None, default_days=30)
        source_value = self._activity_source_value(source)
        config = {
            "days": selected_days,
            "source": source_value,
            "limit": limit,
        }
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "categories",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="categories",
            config=config,
        )

    @activity.command(
        name="heatmap",
        description="Show the most and least active times",
    )
    @app_commands.describe(
        period="Preset reporting period",
        source="Include all, live, or imported message activity",
        timezone="IANA timezone, such as America/Chicago",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_heatmap(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        source: Optional[app_commands.Choice[str]] = None,
        timezone: app_commands.Range[str, 1, 64] = "America/Chicago",
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_days = self._activity_period_days(period, None, default_days=30)
        source_value = self._activity_source_value(source)
        config = {
            "days": selected_days,
            "source": source_value,
            "timezone": timezone.strip(),
        }
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "heatmap",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="heatmap",
            config=config,
        )

    @activity.command(
        name="vc",
        description="Show voice-channel activity from VC tracking",
    )
    @app_commands.describe(
        period="Preset reporting period; takes priority over days",
        days="Number of days to include",
        limit="Number of channels and members to show",
        include_left_members="Include users who are no longer in the server",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.choices(period=ACTIVITY_PERIOD_CHOICES)
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_vc(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        days: Optional[app_commands.Range[int, 1, 3650]] = None,
        limit: app_commands.Range[int, 1, 20] = 10,
        include_left_members: bool = False,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        selected_days = self._activity_period_days(period, days, default_days=7)
        config = {
            "days": selected_days,
            "limit": limit,
            "include_left_members": include_left_members,
        }
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "vc",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="vc",
            config=config,
        )

    @activity.command(
        name="export",
        description="Export tracked community activity to CSV",
    )
    @app_commands.describe(
        period="Preset reporting period; takes priority over days",
        days="Number of days to include",
        include_vc="Include VC sessions when available",
        source="Include all, live, or imported message activity",
        include_left_members="Include users who are no longer in the server",
        channel="Optional staff channel where the CSV should be posted",
    )
    @app_commands.choices(
        period=ACTIVITY_PERIOD_CHOICES,
        source=ACTIVITY_SOURCE_CHOICES,
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_export(
        self,
        interaction: discord.Interaction,
        period: Optional[app_commands.Choice[str]] = None,
        days: Optional[app_commands.Range[int, 1, 3650]] = None,
        include_vc: bool = True,
        source: Optional[app_commands.Choice[str]] = None,
        include_left_members: bool = False,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        selected_days = self._activity_period_days(period, days, default_days=7)
        period_label = self._activity_period_label(selected_days)
        source_value = self._activity_source_value(source)
        file = await self._activity_export_file(
            interaction.guild,
            selected_days,
            include_vc,
            source_value,
            include_left_members,
        )
        if file is None:
            await interaction.followup.send(
                "That activity export is too large for Discord. "
                "Try a shorter date range.",
                ephemeral=True,
            )
            return
        summary = (
            f"Exported **{source_value}** activity metadata for "
            f"**{period_label}** "
            f"({'includes left members' if include_left_members else 'current members only for user rows'})."
            + (
                f"\n⚠️ {member_filter_warning(self.bot, interaction.guild)}"
                if not include_left_members
                and member_filter_warning(self.bot, interaction.guild)
                else ""
            )
        )
        await self._send_activity_file(
            interaction,
            summary,
            file,
            channel,
        )

    @activity.command(
        name="importinfo",
        description="Show recent historical activity import batches",
    )
    @app_commands.describe(
        limit="Number of recent import files to show",
        channel="Optional channel where the report should be posted",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def activity_importinfo(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        config = {"limit": limit}
        embed = await self._build_activity_report_embed(
            interaction.guild,
            "importinfo",
            config,
        )
        await self._send_activity_report(
            interaction,
            embed,
            channel,
            report_type="importinfo",
            config=config,
        )

    @stats.command(
        name="rolecompare",
        description="Create a tracked visual comparison of two roles",
    )
    @app_commands.describe(
        role_1="First role to compare",
        role_2="Second role to compare",
        title="Optional report title",
        body="Optional report description",
        channel="Channel where the report should be posted",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def rolecompare(
        self,
        interaction: discord.Interaction,
        role_1: discord.Role,
        role_2: discord.Role,
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        body: Optional[app_commands.Range[str, 1, 500]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if not self._valid_target_channel(interaction, target_channel):
            await interaction.response.send_message(
                "The stats report must be sent in a text channel in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        report_title = title or f"{role_1.name} vs {role_2.name}"
        created = await self._create_tracked_report(
            interaction=interaction,
            target_channel=target_channel,
            report_type="rolecompare",
            title=report_title,
            body=body or "",
            role_1=role_1,
            role_2=role_2,
        )
        if created:
            await interaction.followup.send(
                f"Created the tracked role comparison in {target_channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I could not create the role comparison report.",
                ephemeral=True,
            )

    @stats.command(
        name="missingrole",
        description="Create a tracked visual missing-role audit",
    )
    @app_commands.describe(
        has_role="Role members must currently have",
        missing_role="Role members must not currently have",
        title="Optional report title",
        body="Optional report description",
        channel="Channel where the report should be posted",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def missingrole(
        self,
        interaction: discord.Interaction,
        has_role: discord.Role,
        missing_role: discord.Role,
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        body: Optional[app_commands.Range[str, 1, 500]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if not self._valid_target_channel(interaction, target_channel):
            await interaction.response.send_message(
                "The stats report must be sent in a text channel in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        report_title = title or f"Missing {missing_role.name}"
        created = await self._create_tracked_report(
            interaction=interaction,
            target_channel=target_channel,
            report_type="missingrole",
            title=report_title,
            body=body or "",
            has_role=has_role,
            missing_role=missing_role,
        )
        if created:
            await interaction.followup.send(
                f"Created the tracked missing-role report in {target_channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I could not create the missing-role report.",
                ephemeral=True,
            )

    async def _create_tracked_report(
        self,
        *,
        interaction: discord.Interaction,
        target_channel,
        report_type: str,
        title: str,
        body: str,
        role_1: Optional[discord.Role] = None,
        role_2: Optional[discord.Role] = None,
        has_role: Optional[discord.Role] = None,
        missing_role: Optional[discord.Role] = None,
    ) -> bool:
        now = self._utcnow()
        png = await self._render_report_png(
            report_type=report_type,
            title=title,
            body=body,
            updated_at=now,
            role_1=role_1,
            role_2=role_2,
            has_role=has_role,
            missing_role=missing_role,
        )
        file = discord.File(
            io.BytesIO(png),
            filename=f"{report_type}_report.png",
        )
        try:
            message = await target_channel.send(
                file=file,
                view=StatsExportView(self),
            )
        except (discord.Forbidden, discord.HTTPException):
            return False

        timestamp = now.isoformat()
        try:
            await self.bot.db.execute(
                """
                INSERT INTO tracked_stats_reports (
                    guild_id,
                    channel_id,
                    message_id,
                    report_type,
                    role_1_id,
                    role_2_id,
                    has_role_id,
                    missing_role_id,
                    title,
                    body,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.guild_id,
                    target_channel.id,
                    message.id,
                    report_type,
                    role_1.id if role_1 else None,
                    role_2.id if role_2 else None,
                    has_role.id if has_role else None,
                    missing_role.id if missing_role else None,
                    title,
                    body,
                    interaction.user.id,
                    timestamp,
                    timestamp,
                ),
            )
            await self.bot.db.commit()
        except Exception:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            raise
        return True

    async def _render_report_png(
        self,
        *,
        report_type: str,
        title: str,
        body: str,
        updated_at: datetime.datetime,
        role_1: Optional[discord.Role] = None,
        role_2: Optional[discord.Role] = None,
        has_role: Optional[discord.Role] = None,
        missing_role: Optional[discord.Role] = None,
    ) -> bytes:
        if report_type == "rolecompare" and role_1 and role_2:
            data = self._calculate_rolecompare(role_1, role_2)
            return await asyncio.to_thread(
                render_rolecompare_report,
                title=title,
                body=body,
                role_1_name=role_1.name,
                role_2_name=role_2.name,
                counts=data["counts"],
                updated_at=updated_at,
                accent_color=role_1.color.value or COLOR,
            )

        if report_type == "missingrole" and has_role and missing_role:
            data = self._calculate_missingrole(has_role, missing_role)
            return await asyncio.to_thread(
                render_missingrole_report,
                title=title,
                body=body,
                has_role_name=has_role.name,
                missing_role_name=missing_role.name,
                has_role_total=data["has_role_total"],
                missing_role_total=data["missing_role_total"],
                missing_count=len(data["members"]),
                missing_percent=data["missing_percent"],
                updated_at=updated_at,
                accent_color=has_role.color.value or COLOR,
            )

        return await asyncio.to_thread(
            render_report_error,
            title=title or "Stats report",
            message="One or more configured roles no longer exist.",
            updated_at=updated_at,
            accent_color=COLOR,
        )

    @staticmethod
    def _calculate_rolecompare(
        role_1: discord.Role,
        role_2: discord.Role,
    ):
        role_1_members = {member.id: member for member in role_1.members}
        role_2_members = {member.id: member for member in role_2.members}
        both_ids = role_1_members.keys() & role_2_members.keys()
        role_1_only_ids = role_1_members.keys() - role_2_members.keys()
        role_2_only_ids = role_2_members.keys() - role_1_members.keys()
        return {
            "counts": {
                "role_1_total": len(role_1_members),
                "role_2_total": len(role_2_members),
                "both": len(both_ids),
                "role_1_only": len(role_1_only_ids),
                "role_2_only": len(role_2_only_ids),
            },
            "both": [role_1_members[user_id] for user_id in both_ids],
            "role_1_only": [
                role_1_members[user_id] for user_id in role_1_only_ids
            ],
            "role_2_only": [
                role_2_members[user_id] for user_id in role_2_only_ids
            ],
        }

    @staticmethod
    def _calculate_missingrole(
        has_role: discord.Role,
        missing_role: discord.Role,
    ):
        missing_role_member_ids = {member.id for member in missing_role.members}
        members = [
            member
            for member in has_role.members
            if member.id not in missing_role_member_ids
        ]
        has_role_total = len(has_role.members)
        return {
            "has_role_total": has_role_total,
            "missing_role_total": len(missing_role.members),
            "members": members,
            "missing_percent": (
                len(members) / has_role_total * 100 if has_role_total else 0
            ),
        }

    async def _tracked_report_rows(
        self,
        *,
        guild_id: Optional[int] = None,
        report_id: Optional[int] = None,
    ):
        clauses = ["status = 'active'"]
        parameters = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            parameters.append(guild_id)
        if report_id is not None:
            clauses.append("id = ?")
            parameters.append(report_id)

        query = """
            SELECT
                id,
                guild_id,
                channel_id,
                message_id,
                report_type,
                role_1_id,
                role_2_id,
                has_role_id,
                missing_role_id,
                title,
                body
            FROM tracked_stats_reports
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        cursor = await self.bot.db.execute(query, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _refresh_report_row(self, row) -> bool:
        (
            record_id,
            guild_id,
            channel_id,
            message_id,
            report_type,
            role_1_id,
            role_2_id,
            has_role_id,
            missing_role_id,
            title,
            body,
        ) = row
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        channel = self._get_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return False

        role_1 = guild.get_role(role_1_id) if role_1_id else None
        role_2 = guild.get_role(role_2_id) if role_2_id else None
        has_role = guild.get_role(has_role_id) if has_role_id else None
        missing_role = guild.get_role(missing_role_id) if missing_role_id else None
        now = self._utcnow()
        png = await self._render_report_png(
            report_type=report_type,
            title=title or "Stats report",
            body=body or "",
            updated_at=now,
            role_1=role_1,
            role_2=role_2,
            has_role=has_role,
            missing_role=missing_role,
        )
        asset = (f"{report_type}_report.png", png)
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            return False

        file = discord.File(io.BytesIO(png), filename=asset[0])
        if message is not None:
            try:
                await message.edit(
                    content=None,
                    embeds=[],
                    attachments=[file],
                    view=StatsExportView(self),
                )
                await self.bot.db.execute(
                    "UPDATE tracked_stats_reports SET updated_at = ? WHERE id = ?",
                    (now.isoformat(), record_id),
                )
                await self.bot.db.commit()
                return True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        replacement = discord.File(io.BytesIO(png), filename=asset[0])
        try:
            new_message = await channel.send(
                file=replacement,
                view=StatsExportView(self),
            )
        except (discord.Forbidden, discord.HTTPException):
            return False

        if message is not None:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await new_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return False

        await self.bot.db.execute(
            """
            UPDATE tracked_stats_reports
            SET message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_message.id, now.isoformat(), record_id),
        )
        await self.bot.db.commit()
        return True

    async def _report_csv_file(
        self,
        guild: discord.Guild,
        message_id: int,
    ) -> Optional[discord.File]:
        cursor = await self.bot.db.execute(
            """
            SELECT
                report_type,
                role_1_id,
                role_2_id,
                has_role_id,
                missing_role_id
            FROM tracked_stats_reports
            WHERE guild_id = ? AND message_id = ?
            """,
            (guild.id, message_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None

        report_type, role_1_id, role_2_id, has_role_id, missing_role_id = row
        generated_at = self._utcnow().isoformat()
        output = io.StringIO(newline="")
        writer = csv.writer(output)

        if report_type == "rolecompare":
            role_1 = guild.get_role(role_1_id)
            role_2 = guild.get_role(role_2_id)
            if role_1 is None or role_2 is None:
                return None
            data = self._calculate_rolecompare(role_1, role_2)
            writer.writerow(
                [
                    "category",
                    "user_id",
                    "username",
                    "display_name",
                    "role_1_name",
                    "role_2_name",
                    "generated_at",
                ]
            )
            for category in ("role_1_only", "role_2_only", "both"):
                members = sorted(
                    data[category],
                    key=lambda member: self._member_username(member).casefold(),
                )
                for member in members:
                    writer.writerow(
                        [
                            category,
                            member.id,
                            self._member_username(member),
                            member.display_name,
                            role_1.name,
                            role_2.name,
                            generated_at,
                        ]
                    )
            filename = "rolecompare_members.csv"
        elif report_type == "missingrole":
            has_role = guild.get_role(has_role_id)
            missing_role = guild.get_role(missing_role_id)
            if has_role is None or missing_role is None:
                return None
            data = self._calculate_missingrole(has_role, missing_role)
            writer.writerow(
                [
                    "user_id",
                    "username",
                    "display_name",
                    "has_role_name",
                    "missing_role_name",
                    "generated_at",
                ]
            )
            for member in sorted(
                data["members"],
                key=lambda member: self._member_username(member).casefold(),
            ):
                writer.writerow(
                    [
                        member.id,
                        self._member_username(member),
                        member.display_name,
                        has_role.name,
                        missing_role.name,
                        generated_at,
                    ]
                )
            filename = "missingrole_members.csv"
        else:
            return None

        return discord.File(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            filename=filename,
        )

    @staticmethod
    def _valid_target_channel(interaction, channel) -> bool:
        return bool(
            channel
            and hasattr(channel, "send")
            and getattr(channel, "guild", None)
            and channel.guild.id == interaction.guild_id
        )

    @stats.command(
        name="reset",
        description="Delete all tracked stats pages in this server",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_delete_access)
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = await self._delete_tracked_pages(interaction.guild, "all")
        await interaction.followup.send(summary, ephemeral=True)

    @stats.command(
        name="delete",
        description="Delete one or all tracked stats pages",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_delete_access)
    async def delete(
        self,
        interaction: discord.Interaction,
    ) -> None:
        rows = await self._delete_menu_rows(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "There are no tracked stats pages in this server.",
                ephemeral=True,
            )
            return

        options = [
            discord.SelectOption(
                label="Delete all tracked stats pages",
                value="all",
                description=f"Remove all {len(rows)} tracked page(s)",
                emoji="🗑️",
            )
        ]
        for source, message_id, channel_id, role_id, title in rows[:24]:
            channel = self._get_channel(interaction.guild, channel_id)
            role = interaction.guild.get_role(role_id) if role_id else None
            page_title = title or (
                f"{role.name} Members" if role else f"Role {role_id} Members"
            )
            channel_name = getattr(channel, "name", f"channel-{channel_id}")
            option_label = f"{page_title} - #{channel_name}"
            options.append(
                discord.SelectOption(
                    label=option_label[:100],
                    value=f"{source}:{message_id}",
                    description="Tracked stats page",
                    emoji="📊",
                )
            )

        await interaction.response.send_message(
            "Select the tracked stats page you want to delete.",
            view=StatsDeleteView(self, options),
            ephemeral=True,
        )

    async def _delete_menu_rows(self, guild_id: int):
        cursor = await self.bot.db.execute(
            """
            SELECT source, message_id, channel_id, role_id, title, created_at
            FROM (
                SELECT
                    'roster' AS source,
                    message_id,
                    channel_id,
                    role_id,
                    title,
                    created_at
                FROM role_stat_embeds
                WHERE guild_id = ?
                UNION ALL
                SELECT
                    'report' AS source,
                    message_id,
                    channel_id,
                    COALESCE(role_1_id, has_role_id) AS role_id,
                    title,
                    created_at
                FROM tracked_stats_reports
                WHERE guild_id = ?
                UNION ALL
                SELECT
                    'activity' AS source,
                    message_id,
                    channel_id,
                    NULL AS role_id,
                    report_type || ' activity report' AS title,
                    created_at
                FROM tracked_activity_reports
                WHERE guild_id = ?
            )
            ORDER BY created_at DESC
            """,
            (guild_id, guild_id, guild_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row[:5] for row in rows]

    async def _delete_tracked_pages(
        self,
        guild: discord.Guild,
        selection: str,
    ) -> str:
        if selection == "all":
            cursor = await self.bot.db.execute(
                """
                SELECT 'roster', id, channel_id, message_id
                FROM role_stat_embeds
                WHERE guild_id = ?
                UNION ALL
                SELECT 'report', id, channel_id, message_id
                FROM tracked_stats_reports
                WHERE guild_id = ?
                UNION ALL
                SELECT 'activity', id, channel_id, message_id
                FROM tracked_activity_reports
                WHERE guild_id = ?
                """,
                (guild.id, guild.id, guild.id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        elif ":" in selection:
            source, raw_message_id = selection.split(":", 1)
            if (
                source not in {"roster", "report", "activity"}
                or not raw_message_id.isdigit()
            ):
                return "That tracked stats selection is no longer valid."
            table = {
                "roster": "role_stat_embeds",
                "report": "tracked_stats_reports",
                "activity": "tracked_activity_reports",
            }[source]
            cursor = await self.bot.db.execute(
                f"""
                SELECT ?, id, channel_id, message_id
                FROM {table}
                WHERE guild_id = ? AND message_id = ?
                """,
                (source, guild.id, int(raw_message_id)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        else:
            return "That tracked stats selection is no longer valid."

        if not rows:
            return "No matching tracked stats pages were found."

        roster_ids = [row[1] for row in rows if row[0] == "roster"]
        report_ids = [row[1] for row in rows if row[0] == "report"]
        activity_ids = [row[1] for row in rows if row[0] == "activity"]
        if roster_ids:
            placeholders = ", ".join("?" for _ in roster_ids)
            await self.bot.db.execute(
                f"DELETE FROM role_stat_embeds WHERE id IN ({placeholders})",
                roster_ids,
            )
        if report_ids:
            placeholders = ", ".join("?" for _ in report_ids)
            await self.bot.db.execute(
                f"DELETE FROM tracked_stats_reports WHERE id IN ({placeholders})",
                report_ids,
            )
        if activity_ids:
            placeholders = ", ".join("?" for _ in activity_ids)
            await self.bot.db.execute(
                f"""
                DELETE FROM tracked_activity_reports
                WHERE id IN ({placeholders})
                """,
                activity_ids,
            )
        await self.bot.db.commit()

        deleted_messages = 0
        failed_messages = 0
        for _, _, channel_id, message_id in rows:
            channel = self._get_channel(guild, channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                failed_messages += 1
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
                deleted_messages += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                failed_messages += 1

        return (
            f"Removed {len(rows)} tracked page(s). "
            f"Deleted {deleted_messages} Discord message(s). "
            f"Failed to delete {failed_messages}."
        )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        changed_role_ids = before_role_ids.symmetric_difference(after_role_ids)
        await self._queue_tracked_role_refreshes(after.guild.id, changed_role_ids)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is None
            or message.author.bot
        ):
            return
        if getattr(message.author, "id", None) in self.activity_excluded_user_ids:
            return
        if isinstance(message.author, discord.Member) and self._activity_member_excluded(
            message.author
        ):
            return
        now = self._utcnow()
        activity_hour = now.replace(
            minute=0,
            second=0,
            microsecond=0,
        ).isoformat()
        channel_name = getattr(message.channel, "name", None)
        await self.bot.db.execute(
            """
            INSERT INTO stats_message_activity (
                guild_id,
                channel_id,
                channel_name,
                user_id,
                display_name,
                username,
                activity_date,
                activity_hour,
                message_count,
                source,
                import_batch_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'live', 'live', ?, ?)
            ON CONFLICT(
                guild_id, channel_id, user_id, activity_hour, source,
                import_batch_id
            )
            DO UPDATE SET
                channel_name = excluded.channel_name,
                display_name = excluded.display_name,
                username = excluded.username,
                message_count = message_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                message.guild.id,
                message.channel.id,
                channel_name,
                message.author.id,
                message.author.display_name,
                self._member_username(message.author),
                now.date().isoformat(),
                activity_hour,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not member.bot:
            now = self._utcnow().isoformat()
            joined_at = (
                member.joined_at.astimezone(datetime.timezone.utc).isoformat()
                if member.joined_at
                else now
            )
            await self.bot.db.execute(
                """
                INSERT INTO stats_member_joins (
                    guild_id,
                    user_id,
                    display_name,
                    username,
                    joined_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    member.guild.id,
                    member.id,
                    member.display_name,
                    self._member_username(member),
                    joined_at,
                    now,
                ),
            )
            await self.bot.db.commit()
        await self._queue_tracked_role_refreshes(
            member.guild.id, (role.id for role in member.roles)
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not member.bot:
            now = self._utcnow().isoformat()
            await self.bot.db.execute(
                """
                INSERT INTO stats_member_leaves (
                    guild_id,
                    user_id,
                    display_name,
                    username,
                    left_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    member.guild.id,
                    member.id,
                    member.display_name,
                    self._member_username(member),
                    now,
                    now,
                ),
            )
            await self.bot.db.commit()
        await self._queue_tracked_role_refreshes(
            member.guild.id, (role.id for role in member.roles)
        )

    async def _queue_tracked_role_refreshes(
        self, guild_id: int, role_ids: Iterable[int]
    ) -> None:
        role_ids = set(role_ids)
        if not role_ids:
            return

        placeholders = ", ".join("?" for _ in role_ids)
        cursor = await self.bot.db.execute(
            f"""
            SELECT DISTINCT role_id
            FROM role_stat_embeds
            WHERE guild_id = ?
              AND status = 'active'
              AND role_id IN ({placeholders})
            """,
            (guild_id, *role_ids),
        )
        tracked_role_ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        cursor = await self.bot.db.execute(
            f"""
            SELECT DISTINCT id
            FROM tracked_stats_reports
            WHERE guild_id = ?
              AND status = 'active'
              AND (
                    role_1_id IN ({placeholders})
                 OR role_2_id IN ({placeholders})
                 OR has_role_id IN ({placeholders})
                 OR missing_role_id IN ({placeholders})
              )
            """,
            (
                guild_id,
                *role_ids,
                *role_ids,
                *role_ids,
                *role_ids,
            ),
        )
        tracked_report_ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()

        for role_id in tracked_role_ids:
            key = (guild_id, role_id)
            existing_task = self._refresh_tasks.get(key)
            if existing_task:
                existing_task.cancel()
            self._refresh_tasks[key] = asyncio.create_task(
                self._debounced_role_refresh(guild_id, role_id)
            )
        for report_id in tracked_report_ids:
            key = (guild_id, -report_id)
            existing_task = self._refresh_tasks.get(key)
            if existing_task:
                existing_task.cancel()
            self._refresh_tasks[key] = asyncio.create_task(
                self._debounced_report_refresh(guild_id, report_id)
            )

    async def _debounced_role_refresh(self, guild_id: int, role_id: int) -> None:
        key = (guild_id, role_id)
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            rows = await self._tracked_rows(guild_id=guild_id, role_id=role_id)
            for row in rows:
                await self._refresh_row(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Debounced role-stat refresh failed guild=%s role=%s",
                guild_id,
                role_id,
            )
        finally:
            current_task = asyncio.current_task()
            if self._refresh_tasks.get(key) is current_task:
                self._refresh_tasks.pop(key, None)

    async def _debounced_report_refresh(
        self,
        guild_id: int,
        report_id: int,
    ) -> None:
        key = (guild_id, -report_id)
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            rows = await self._tracked_report_rows(report_id=report_id)
            for row in rows:
                await self._refresh_report_row(row)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Debounced stats report refresh failed guild=%s report=%s",
                guild_id,
                report_id,
            )
        finally:
            current_task = asyncio.current_task()
            if self._refresh_tasks.get(key) is current_task:
                self._refresh_tasks.pop(key, None)

    async def _tracked_rows(
        self,
        guild_id: Optional[int] = None,
        role_id: Optional[int] = None,
    ):
        clauses = ["status = 'active'"]
        parameters = []

        if guild_id is not None:
            clauses.append("guild_id = ?")
            parameters.append(guild_id)
        if role_id is not None:
            clauses.append("role_id = ?")
            parameters.append(role_id)

        query = """
            SELECT
                id,
                guild_id,
                channel_id,
                message_id,
                role_id,
                title,
                body,
                image_url,
                image_data,
                graphic_enabled
            FROM role_stat_embeds
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        cursor = await self.bot.db.execute(query, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _refresh_row(self, row) -> bool:
        (
            record_id,
            guild_id,
            channel_id,
            message_id,
            role_id,
            title,
            body,
            image_url,
            image_data,
            graphic_enabled,
        ) = row

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False

        role = guild.get_role(role_id)
        channel = self._get_channel(guild, channel_id)
        if role is None or channel is None or not hasattr(channel, "fetch_message"):
            return False

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            return False

        if image_data is None and message is not None:
            legacy_banner = next(
                (
                    attachment
                    for attachment in message.attachments
                    if not self._is_roster_filename(attachment.filename, role.id)
                    and self._is_image_attachment(attachment)
                ),
                None,
            )
            if legacy_banner is not None:
                try:
                    image_data = await legacy_banner.read()
                    image_url = legacy_banner.url
                    await self.bot.db.execute(
                        """
                        UPDATE role_stat_embeds
                        SET image_url = ?, image_data = ?
                        WHERE id = ?
                        """,
                        (image_url, image_data, record_id),
                    )
                    await self.bot.db.commit()
                except (discord.Forbidden, discord.HTTPException):
                    image_data = None

        now = self._utcnow()
        try:
            roster_files = await self._build_roster_files(
                role,
                title,
                body,
                now,
                image_data,
            )
        except Exception:
            return False
        roster_assets = [
            (roster_file.filename, roster_file.fp.getvalue())
            for roster_file in roster_files
        ]
        if message is None:
            return await self._recreate_tracked_message(
                record_id=record_id,
                channel=channel,
                old_message=None,
                roster_assets=roster_assets,
                updated_at=now,
            )

        try:
            await message.edit(
                content=None,
                embeds=[],
                attachments=roster_files,
            )
        except discord.NotFound:
            return await self._recreate_tracked_message(
                record_id=record_id,
                channel=channel,
                old_message=None,
                roster_assets=roster_assets,
                updated_at=now,
            )
        except (discord.Forbidden, discord.HTTPException):
            return await self._recreate_tracked_message(
                record_id=record_id,
                channel=channel,
                old_message=message,
                roster_assets=roster_assets,
                updated_at=now,
            )

        await self.bot.db.execute(
            "UPDATE role_stat_embeds SET updated_at = ? WHERE id = ?",
            (now.isoformat(), record_id),
        )
        await self.bot.db.commit()
        return True

    async def _recreate_tracked_message(
        self,
        record_id: int,
        channel,
        old_message,
        roster_assets,
        updated_at: datetime.datetime,
    ) -> bool:
        replacement_files = [
            discord.File(io.BytesIO(data), filename=filename)
            for filename, data in roster_assets
        ]

        try:
            new_message = await channel.send(files=replacement_files)
        except (discord.Forbidden, discord.HTTPException):
            return False

        if old_message is not None:
            try:
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await new_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return False

        await self.bot.db.execute(
            """
            UPDATE role_stat_embeds
            SET message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_message.id, updated_at.isoformat(), record_id),
        )
        await self.bot.db.commit()
        return True

    async def _build_roster_files(
        self,
        role: discord.Role,
        title: Optional[str],
        body: Optional[str],
        updated_at: datetime.datetime,
        banner_bytes: Optional[bytes],
    ):
        members = sorted(
            role.members,
            key=lambda member: self._member_username(member).casefold(),
        )
        items = [
            CompactRosterItem(
                label=self._member_username(member),
                avatar_url=str(member.display_avatar.replace(size=32).url),
            )
            for member in members
        ]
        pngs = await render_compact_roster_pngs(
            title=title or f"{role.name} Members",
            body=body or "",
            role_name=role.name,
            items=items,
            updated_at=updated_at,
            accent_color=role.color.value or COLOR,
            include_avatars=True,
            banner_bytes=banner_bytes,
        )
        return [
            discord.File(
                fp=io.BytesIO(png),
                filename=self._roster_filename(page_number, len(pngs)),
            )
            for page_number, png in enumerate(pngs, start=1)
        ]

    @staticmethod
    def _roster_filename(page_number: int, page_count: int) -> str:
        if page_count == 1:
            return "role_roster.png"
        return f"role_roster_{page_number}.png"

    @staticmethod
    def _is_roster_filename(filename: str, role_id: int) -> bool:
        return (
            filename == f"role-roster-{role_id}.png"
            or filename == "role_roster.png"
            or (
                filename.startswith("role_roster_")
                and filename.endswith(".png")
            )
        )

    async def _fetchone(self, query: str, parameters=()):
        cursor = await self.bot.db.execute(query, parameters)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _fetchall(self, query: str, parameters=()):
        cursor = await self.bot.db.execute(query, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _table_exists(self, table_name: str) -> bool:
        row = await self._fetchone(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        )
        return row is not None

    def _activity_cutoff(self, days: int) -> str:
        return (
            self._utcnow() - datetime.timedelta(days=days)
        ).replace(minute=0, second=0, microsecond=0).isoformat()

    @staticmethod
    def _activity_period_days(
        period: Optional[app_commands.Choice[str]],
        days: Optional[int],
        *,
        default_days: int,
    ) -> Optional[int]:
        if period:
            if period.value == "all_time":
                return None
            return int(period.value)
        return days if days is not None else default_days

    @staticmethod
    def _activity_period_label(days: Optional[int]) -> str:
        return "All time" if days is None else f"Last {days} days"

    @staticmethod
    def _hour_label(hour: int) -> str:
        suffix = "AM" if hour < 12 else "PM"
        display_hour = hour % 12 or 12
        return f"{display_hour}:00 {suffix}"

    def _activity_date_filter(
        self,
        days: Optional[int],
        *,
        column: str = "activity_hour",
    ) -> Tuple[str, Tuple[str, ...]]:
        if days is None:
            return "", ()
        return f"AND {column} >= ?", (self._activity_cutoff(days),)

    @staticmethod
    def _activity_source_value(
        source: Optional[app_commands.Choice[str]],
    ) -> str:
        return source.value if source else "all"

    @staticmethod
    def _activity_source_filter(source: str) -> Tuple[str, Tuple[str, ...]]:
        if source == "live":
            return "AND source = ?", ("live",)
        if source == "imported":
            return (
                "AND source IN (?, ?, ?)",
                ("imported", "imported_csv", "csv_backfill"),
            )
        return "", ()

    async def _tracking_started_datetime(self):
        row = await self._fetchone(
            """
            SELECT value
            FROM stats_activity_settings
            WHERE key = 'activity_tracking_started_at'
            """
        )
        if not row:
            return None
        try:
            value = datetime.datetime.fromisoformat(row[0])
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            return value.astimezone(datetime.timezone.utc)
        except (TypeError, ValueError):
            return None

    async def _activity_overview_data(
        self,
        guild_id: int,
        days: Optional[int],
        source: str = "all",
        excluded_user_ids: Iterable[int] = (),
    ):
        source_sql, source_parameters = self._activity_source_filter(source)
        date_sql, date_parameters = self._activity_date_filter(days)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            excluded_user_ids
        )
        message_summary = await self._fetchone(
            f"""
            SELECT COALESCE(SUM(message_count), 0), COUNT(DISTINCT user_id)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        total_messages, text_active_count = message_summary or (0, 0)
        text_users = {
            row[0]
            for row in await self._fetchall(
                f"""
                SELECT DISTINCT user_id
                FROM stats_message_activity
                WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
                """,
                (
                    guild_id,
                    *date_parameters,
                    *source_parameters,
                    *excluded_parameters,
                ),
            )
        }
        event_date_sql, event_date_parameters = self._activity_date_filter(
            days,
            column="joined_at",
        )
        joins = await self._fetchone(
            f"""
            SELECT COUNT(*)
            FROM stats_member_joins
            WHERE guild_id = ? {event_date_sql}
            """,
            (guild_id, *event_date_parameters),
        )
        leave_date_sql, leave_date_parameters = self._activity_date_filter(
            days,
            column="left_at",
        )
        leaves = await self._fetchone(
            f"""
            SELECT COUNT(*)
            FROM stats_member_leaves
            WHERE guild_id = ? {leave_date_sql}
            """,
            (guild_id, *leave_date_parameters),
        )
        top_text = await self._fetchone(
            f"""
            SELECT channel_id, MAX(channel_name), SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY channel_id
            ORDER BY SUM(message_count) DESC
            LIMIT 1
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        daily_rows = await self._fetchall(
            f"""
            SELECT activity_date, SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY activity_date
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        range_row = await self._fetchone(
            f"""
            SELECT MIN(activity_date), MAX(activity_date)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        daily = {}
        if days is not None:
            today = self._utcnow().date()
            for offset in range(days):
                day = today - datetime.timedelta(days=offset)
                daily[day.isoformat()] = 0
        for date_value, count in daily_rows:
            daily[date_value] = count
        if daily:
            busiest_date, busiest_count = max(
                daily.items(),
                key=lambda item: (item[1], item[0]),
            )
            quietest_date, quietest_count = min(
                daily.items(),
                key=lambda item: (item[1], item[0]),
            )
        else:
            busiest_date = quietest_date = "No tracked data"
            busiest_count = quietest_count = 0

        vc_available = await self._table_exists("vc_sessions")
        vc_seconds = 0
        top_vc_channel = "No tracked VC activity"
        if vc_available:
            vc_excluded_sql, vc_excluded_parameters = self._excluded_user_sql(
                self._vc_excluded_ids_for_guild(self.bot.get_guild(guild_id))
            )
            vc_date_sql, vc_date_parameters = self._activity_date_filter(
                days,
                column="left_at",
            )
            vc_total = await self._fetchone(
                f"""
                SELECT COALESCE(SUM(duration_seconds), 0)
                FROM vc_sessions
                WHERE guild_id = ? {vc_date_sql} {vc_excluded_sql}
                """,
                (guild_id, *vc_date_parameters, *vc_excluded_parameters),
            )
            vc_seconds = vc_total[0] if vc_total else 0
            top_vc = await self._fetchone(
                f"""
                SELECT channel_id, MAX(channel_name), SUM(duration_seconds)
                FROM vc_sessions
                WHERE guild_id = ? {vc_date_sql} {vc_excluded_sql}
                GROUP BY channel_id
                ORDER BY SUM(duration_seconds) DESC
                LIMIT 1
                """,
                (guild_id, *vc_date_parameters, *vc_excluded_parameters),
            )
            if top_vc:
                top_vc_channel = (
                    f"{safe_channel_display(top_vc[0], top_vc[1])} "
                    f"({format_duration(top_vc[2])})"
                )
            vc_users = await self._fetchall(
                f"""
                SELECT DISTINCT user_id
                FROM vc_sessions
                WHERE guild_id = ? {vc_date_sql} {vc_excluded_sql}
                """,
                (guild_id, *vc_date_parameters, *vc_excluded_parameters),
            )
            if source != "imported":
                text_users.update(row[0] for row in vc_users)

        if days is not None:
            average_days = days
        elif range_row and range_row[0] and range_row[1]:
            first_day = datetime.date.fromisoformat(range_row[0])
            last_day = datetime.date.fromisoformat(range_row[1])
            average_days = (last_day - first_day).days + 1
        else:
            average_days = 0
        average = total_messages / average_days if average_days else 0
        concentration = (
            top_text[2] / total_messages * 100
            if top_text and total_messages
            else 0
        )
        if not total_messages:
            vibe = "No text activity has been tracked during this period."
        elif average < 25:
            vibe = (
                f"Low-volume period, averaging {average:.1f} tracked messages "
                "per day."
            )
        elif concentration >= 60:
            vibe = (
                f"Conversation was concentrated: the top channel accounted for "
                f"{concentration:.1f}% of tracked messages."
            )
        else:
            vibe = (
                f"Activity was distributed across channels, averaging "
                f"{average:.1f} tracked messages per day."
            )

        tracking_started = await self._tracking_started_datetime()
        return {
            "total_messages": total_messages,
            "active_members": len(text_users) or text_active_count,
            "joins": joins[0] if joins else 0,
            "leaves": leaves[0] if leaves else 0,
            "top_text_channel": (
                f"{safe_channel_display(top_text[0], top_text[1])} "
                f"({top_text[2]:,})"
                if top_text
                else "No tracked text activity"
            ),
            "busiest_day": f"{busiest_date} ({busiest_count:,})",
            "quietest_day": f"{quietest_date} ({quietest_count:,})",
            "data_range": (
                f"{range_row[0]} to {range_row[1]}"
                if range_row and range_row[0] and range_row[1]
                else "No tracked data"
            ),
            "vc_available": vc_available,
            "vc_seconds": vc_seconds,
            "top_vc_channel": top_vc_channel,
            "vibe": vibe,
            "tracking_started": (
                tracking_started.date().isoformat()
                if tracking_started
                else "when this feature was deployed"
            ),
        }

    async def _activity_export_file(
        self,
        guild: discord.Guild,
        days: Optional[int],
        include_vc: bool,
        source: str = "all",
        include_left_members: bool = False,
    ) -> Optional[discord.File]:
        guild_id = guild.id
        source_sql, source_parameters = self._activity_source_filter(source)
        date_sql, date_parameters = self._activity_date_filter(days)
        excluded_ids = self._activity_excluded_ids_for_guild(guild)
        excluded_sql, excluded_parameters = self._excluded_user_sql(excluded_ids)
        output = io.StringIO(newline="")
        headers = [
            "section",
            "guild_id",
            "channel_id",
            "channel_name",
            "user_id",
            "username",
            "display_name",
            "is_current_member",
            "activity_date",
            "activity_hour",
            "message_count",
            "source",
            "import_batch_id",
            "event_time",
            "duration_seconds",
            "duration_readable",
            "counted_seconds",
            "counted_readable",
            "reward_eligible",
            "metric",
            "value",
        ]
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()

        overview = await self._activity_overview_data(
            guild_id,
            days,
            source,
            excluded_ids,
        )
        for metric in (
            "total_messages",
            "active_members",
            "joins",
            "leaves",
            "vc_seconds",
        ):
            writer.writerow(
                {
                    "section": "overview",
                    "guild_id": guild_id,
                    "metric": metric,
                    "value": overview[metric],
                }
            )

        message_rows = await self._fetchall(
            f"""
            SELECT channel_id, channel_name, user_id, username, display_name,
                   activity_date, activity_hour, message_count, source,
                   import_batch_id
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            ORDER BY activity_hour, channel_id, user_id
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        for row in message_rows:
            member = current_member(guild, row[2])
            if self._activity_member_excluded(member):
                continue
            if member is None and not include_left_members:
                continue
            writer.writerow(
                {
                    "section": "message_activity",
                    "guild_id": guild_id,
                    "channel_id": row[0],
                    "channel_name": row[1],
                    "user_id": row[2],
                    "username": member.name if member else row[3],
                    "display_name": member.display_name if member else row[4],
                    "is_current_member": member is not None,
                    "activity_date": row[5],
                    "activity_hour": row[6],
                    "message_count": row[7],
                    "source": row[8],
                    "import_batch_id": row[9],
                }
            )

        channel_rows = await self._fetchall(
            f"""
            SELECT channel_id, MAX(channel_name), SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY channel_id
            ORDER BY SUM(message_count) DESC
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        for channel_id, channel_name, count in channel_rows:
            writer.writerow(
                {
                    "section": "channel_summary",
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "metric": "message_count",
                    "value": count,
                }
            )

        member_rows = await self._fetchall(
            f"""
            SELECT user_id, MAX(username), MAX(display_name),
                   SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY user_id
            ORDER BY SUM(message_count) DESC
            """,
            (
                guild_id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        for user_id, username, display_name, count in member_rows:
            member = current_member(guild, user_id)
            if self._activity_member_excluded(member):
                continue
            if member is None and not include_left_members:
                continue
            writer.writerow(
                {
                    "section": "member_summary",
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "username": member.name if member else username,
                    "display_name": (
                        member.display_name if member else display_name
                    ),
                    "is_current_member": member is not None,
                    "metric": "message_count",
                    "value": count,
                }
            )

        for section, table, time_column in (
            ("join", "stats_member_joins", "joined_at"),
            ("leave", "stats_member_leaves", "left_at"),
        ):
            event_sql, event_parameters = self._activity_date_filter(
                days,
                column=time_column,
            )
            rows = await self._fetchall(
                f"""
                SELECT user_id, username, display_name, {time_column}
                FROM {table}
                WHERE guild_id = ? {event_sql}
                ORDER BY {time_column}
                """,
                (guild_id, *event_parameters),
            )
            for user_id, username, display_name, event_time in rows:
                member = current_member(guild, user_id)
                if member is None and not include_left_members:
                    continue
                writer.writerow(
                    {
                        "section": section,
                        "guild_id": guild_id,
                        "user_id": user_id,
                        "username": member.name if member else username,
                        "display_name": (
                            member.display_name if member else display_name
                        ),
                        "is_current_member": member is not None,
                        "event_time": event_time,
                    }
                )

        if include_vc and await self._table_exists("vc_sessions"):
            vc_sql, vc_parameters = self._activity_date_filter(
                days,
                column="left_at",
            )
            rows = await self._fetchall(
                f"""
                SELECT user_id, username, display_name, channel_id,
                       channel_name, duration_seconds, counted_seconds,
                       reward_eligible
                FROM vc_sessions
                WHERE guild_id = ? {vc_sql}
                ORDER BY left_at
                """,
                (guild_id, *vc_parameters),
            )
            for row in rows:
                member = current_member(guild, row[0])
                if member is None and not include_left_members:
                    continue
                writer.writerow(
                    {
                        "section": "vc_session",
                        "guild_id": guild_id,
                        "user_id": row[0],
                        "username": member.name if member else row[1],
                        "display_name": (
                            member.display_name if member else row[2]
                        ),
                        "is_current_member": member is not None,
                        "channel_id": row[3],
                        "channel_name": row[4],
                        "duration_seconds": row[5],
                        "duration_readable": format_duration(row[5]),
                        "counted_seconds": row[6],
                        "counted_readable": format_duration(row[6]),
                        "reward_eligible": row[7],
                    }
                )

        data = output.getvalue().encode("utf-8-sig")
        if len(data) > MAX_ACTIVITY_EXPORT_BYTES:
            return None
        return discord.File(
            io.BytesIO(data),
            filename=(
                f"stats_activity_{source}_all_time.csv"
                if days is None
                else f"stats_activity_{source}_{days}d.csv"
            ),
        )

    async def _build_activity_report_assets(
        self,
        guild: discord.Guild,
        report_type: str,
        config: dict,
    ) -> list:
        if report_type not in ACTIVITY_GRAPHIC_REPORTS:
            return []

        days = config.get("days")
        period_label = self._activity_period_label(days)
        source = config.get("source", "all")
        limit = int(config.get("limit", 10))
        excluded_ids = self._activity_excluded_ids_for_guild(guild)
        excluded_sql, excluded_parameters = self._excluded_user_sql(excluded_ids)
        sections = []
        title = "Activity leaderboard"
        subtitle = f"{period_label} • {source}"

        if report_type == "channels":
            source_sql, source_parameters = self._activity_source_filter(source)
            date_sql, date_parameters = self._activity_date_filter(days)
            rows = await self._fetchall(
                f"""
                SELECT channel_id, MAX(channel_name), SUM(message_count),
                       COUNT(DISTINCT user_id)
                FROM stats_message_activity
                WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
                GROUP BY channel_id
                ORDER BY SUM(message_count) DESC
                LIMIT ?
                """,
                (
                    guild.id,
                    *date_parameters,
                    *source_parameters,
                    *excluded_parameters,
                    limit,
                ),
            )
            total_row = await self._fetchone(
                f"""
                SELECT COALESCE(SUM(message_count), 0)
                FROM stats_message_activity
                WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
                """,
                (
                    guild.id,
                    *date_parameters,
                    *source_parameters,
                    *excluded_parameters,
                ),
            )
            total = int(total_row[0] if total_row else 0)
            items = [
                RankedGraphicItem(
                    label=f"#{channel_name or channel_id}",
                    value=f"{messages:,}",
                    subtitle=(
                        f"{posters:,} unique posters • "
                        f"{messages / total * 100 if total else 0:.1f}% of activity"
                    ),
                    score=float(messages or 0),
                )
                for channel_id, channel_name, messages, posters in rows
            ]
            title = "Top Text Channels"
            subtitle = f"{period_label} • {source} activity • {total:,} messages"
            sections = [RankedGraphicSection("Channel leaderboard", items)]

        elif report_type == "quiet":
            source_sql, source_parameters = self._activity_source_filter(source)
            if days is None:
                rows = await self._fetchall(
                    f"""
                    SELECT channel_id, MAX(channel_name), SUM(message_count),
                           MAX(activity_hour)
                    FROM stats_message_activity
                    WHERE guild_id = ? {source_sql} {excluded_sql}
                    GROUP BY channel_id
                    """,
                    (guild.id, *source_parameters, *excluded_parameters),
                )
            else:
                cutoff = self._activity_cutoff(days)
                rows = await self._fetchall(
                    f"""
                    SELECT channel_id, MAX(channel_name),
                           SUM(CASE WHEN activity_hour >= ?
                               THEN message_count ELSE 0 END),
                           MAX(CASE WHEN activity_hour >= ?
                               THEN activity_hour END)
                    FROM stats_message_activity
                    WHERE guild_id = ? {source_sql} {excluded_sql}
                    GROUP BY channel_id
                    """,
                    (
                        cutoff,
                        cutoff,
                        guild.id,
                        *source_parameters,
                        *excluded_parameters,
                    ),
                )
            row_map = {
                channel_id: (channel_name, int(messages or 0), last_activity)
                for channel_id, channel_name, messages, last_activity in rows
            }
            candidates = []
            for channel in guild.text_channels:
                if guild.me and not channel.permissions_for(guild.me).view_channel:
                    continue
                channel_name, messages, last_activity = row_map.get(
                    channel.id,
                    (channel.name, 0, None),
                )
                candidates.append(
                    (messages, channel.id, channel_name, last_activity)
                )
            candidates.sort(key=lambda item: (item[0], item[2] or ""))
            candidates = candidates[:limit]
            maximum = max((row[0] for row in candidates), default=0)
            items = [
                RankedGraphicItem(
                    label=f"#{channel_name or channel_id}",
                    value=f"{messages:,}",
                    subtitle=(
                        f"Last activity {str(last_activity)[:10]}"
                        if last_activity
                        else "No tracked activity"
                    ),
                    score=float(maximum - messages + 1),
                )
                for messages, channel_id, channel_name, last_activity in candidates
            ]
            title = "Quiet Channel Watch"
            subtitle = f"{period_label} • {source} activity • quietest first"
            sections = [RankedGraphicSection("Low-activity channels", items)]

        elif report_type == "members":
            source_sql, source_parameters = self._activity_source_filter(source)
            date_sql, date_parameters = self._activity_date_filter(days)
            rows = await self._fetchall(
                f"""
                SELECT user_id, MAX(username), MAX(display_name),
                       SUM(message_count) AS total
                FROM stats_message_activity
                WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
                GROUP BY user_id
                ORDER BY total DESC
                """,
                (
                    guild.id,
                    *date_parameters,
                    *source_parameters,
                    *excluded_parameters,
                ),
            )
            include_left = bool(config.get("include_left_members", False))
            items = []
            for user_id, username, display_name, total in rows:
                member = current_member(guild, user_id)
                if self._activity_member_excluded(member):
                    continue
                if member is None and not include_left:
                    continue
                items.append(
                    RankedGraphicItem(
                        label=(
                            member.display_name
                            if member
                            else display_name or username or str(user_id)
                        ),
                        value=f"{total:,}",
                        subtitle=(
                            f"@{member.name}"
                            if member
                            else f"@{username or user_id} • Left server"
                        ),
                        avatar_url=(
                            str(member.display_avatar.replace(size=64).url)
                            if member
                            else None
                        ),
                        score=float(total or 0),
                    )
                )
                if len(items) >= limit:
                    break
            title = "Top Text Participants"
            subtitle = (
                f"{period_label} • {source} activity • "
                f"{'includes former members' if include_left else 'current members'}"
            )
            sections = [RankedGraphicSection("Member leaderboard", items)]

        elif report_type == "vc":
            if not await self._table_exists("vc_sessions"):
                sections = [RankedGraphicSection("Voice activity", [])]
            else:
                vc_excluded_sql, vc_excluded_parameters = self._excluded_user_sql(
                    self._vc_excluded_ids_for_guild(guild)
                )
                date_sql, date_parameters = self._activity_date_filter(
                    days,
                    column="left_at",
                )
                channel_rows = await self._fetchall(
                    f"""
                    SELECT channel_id, MAX(channel_name), SUM(duration_seconds)
                    FROM vc_sessions
                    WHERE guild_id = ? {date_sql} {vc_excluded_sql}
                    GROUP BY channel_id
                    ORDER BY SUM(duration_seconds) DESC
                    LIMIT ?
                    """,
                    (
                        guild.id,
                        *date_parameters,
                        *vc_excluded_parameters,
                        limit,
                    ),
                )
                member_rows = await self._fetchall(
                    f"""
                    SELECT user_id, MAX(username), MAX(display_name),
                           SUM(duration_seconds)
                    FROM vc_sessions
                    WHERE guild_id = ? {date_sql} {vc_excluded_sql}
                    GROUP BY user_id
                    ORDER BY SUM(duration_seconds) DESC
                    """,
                    (guild.id, *date_parameters, *vc_excluded_parameters),
                )
                channel_items = [
                    RankedGraphicItem(
                        label=channel_name or str(channel_id),
                        value=format_duration(seconds),
                        subtitle="Voice channel",
                        score=float(seconds or 0),
                    )
                    for channel_id, channel_name, seconds in channel_rows
                ]
                include_left = bool(config.get("include_left_members", False))
                member_items = []
                for user_id, username, display_name, seconds in member_rows:
                    member = current_member(guild, user_id)
                    if member_is_excluded(
                        member,
                        user_ids=self.vc_excluded_user_ids,
                        role_ids=self.vc_excluded_role_ids,
                    ):
                        continue
                    if member is None and not include_left:
                        continue
                    member_items.append(
                        RankedGraphicItem(
                            label=(
                                member.display_name
                                if member
                                else display_name or username or str(user_id)
                            ),
                            value=format_duration(seconds),
                            subtitle=(
                                f"@{member.name}"
                                if member
                                else f"@{username or user_id} • Left server"
                            ),
                            avatar_url=(
                                str(member.display_avatar.replace(size=64).url)
                                if member
                                else None
                            ),
                            score=float(seconds or 0),
                        )
                    )
                    if len(member_items) >= limit:
                        break
                sections = [
                    RankedGraphicSection("Top voice channels", channel_items),
                    RankedGraphicSection("Top voice members", member_items),
                ]
            title = "Voice Activity Leaders"
            subtitle = f"{period_label} • completed VC sessions"

        png = await render_ranked_graphic(
            title=title,
            subtitle=subtitle,
            sections=sections,
            updated_at=self._utcnow(),
            accent_color=COLOR,
        )
        return [(f"stats_{report_type}_leaderboard.png", png)]

    async def _tracked_activity_rows(
        self,
        *,
        guild_id: Optional[int] = None,
        report_id: Optional[int] = None,
    ):
        query = """
            SELECT id, guild_id, channel_id, message_id, report_type, config_json
            FROM tracked_activity_reports
        """
        clauses = ["status = 'active'"]
        parameters = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            parameters.append(guild_id)
        if report_id is not None:
            clauses.append("id = ?")
            parameters.append(report_id)
        query += " WHERE " + " AND ".join(clauses)
        return await self._fetchall(query, tuple(parameters))

    async def _refresh_activity_report_row(self, row) -> bool:
        record_id, guild_id, channel_id, message_id, report_type, config_json = row
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        channel = self._get_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return False
        try:
            config = json.loads(config_json)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(config, dict):
            return False
        try:
            embed = await self._build_activity_report_embed(
                guild,
                report_type,
                config,
            )
            assets = await self._build_activity_report_assets(
                guild,
                report_type,
                config,
            )
        except Exception:
            return False

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            return False

        now = self._utcnow().isoformat()
        if message is not None:
            try:
                if assets:
                    files = [
                        discord.File(io.BytesIO(data), filename=filename)
                        for filename, data in assets
                    ]
                    await message.edit(
                        content=None,
                        embeds=[],
                        attachments=files,
                    )
                else:
                    await message.edit(
                        content=None,
                        embed=embed,
                        attachments=[],
                    )
                await self.bot.db.execute(
                    """
                    UPDATE tracked_activity_reports
                    SET updated_at = ?
                    WHERE id = ?
                    """,
                    (now, record_id),
                )
                await self.bot.db.commit()
                return True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            if assets:
                files = [
                    discord.File(io.BytesIO(data), filename=filename)
                    for filename, data in assets
                ]
                new_message = await channel.send(files=files)
            else:
                new_message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return False
        if message is not None:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await new_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return False
        await self.bot.db.execute(
            """
            UPDATE tracked_activity_reports
            SET message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_message.id, now, record_id),
        )
        await self.bot.db.commit()
        return True

    @tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone.utc))
    async def daily_stats_refresh(self) -> None:
        try:
            refresh_groups = (
                (await self._tracked_rows(), self._refresh_row),
                (await self._tracked_report_rows(), self._refresh_report_row),
                (
                    await self._tracked_activity_rows(),
                    self._refresh_activity_report_row,
                ),
            )
        except Exception:
            logger.exception("Could not load tracked reports for daily refresh")
            return
        for rows, refresher in refresh_groups:
            for row in rows:
                try:
                    await refresher(row)
                except Exception:
                    logger.exception(
                        "Daily stats refresh failed refresher=%s row_id=%s",
                        getattr(refresher, "__name__", type(refresher).__name__),
                        row[0] if row else "unknown",
                    )
                await asyncio.sleep(0.25)

    @daily_stats_refresh.before_loop
    async def before_daily_stats_refresh(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=5)
    async def dashboard_action_worker(self) -> None:
        actions = await asyncio.to_thread(pending_dashboard_actions, 10)
        for action in actions:
            action_id = int(action["id"])
            if not await asyncio.to_thread(mark_action_processing, action_id):
                continue
            try:
                payload = json.loads(action["payload_json"])
                if action["action_type"] == "refresh_stat":
                    stat_id = str(payload.get("stat_id", ""))
                    success, message = await self._process_dashboard_stat_refresh(
                        stat_id
                    )
                elif action["action_type"] == "reindex_knowledge":
                    success, message = await asyncio.to_thread(
                        process_knowledge_reindex,
                        payload,
                    )
                else:
                    success = False
                    message = "Unsupported dashboard action."
            except Exception as exc:
                logger.exception(
                    "Dashboard action failed action_id=%s action_type=%s",
                    action_id,
                    action.get("action_type"),
                )
                success = False
                message = f"Dashboard action failed: {type(exc).__name__}"
            await asyncio.to_thread(
                complete_action,
                action_id,
                success,
                message,
            )

    @dashboard_action_worker.before_loop
    async def before_dashboard_action_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_dashboard_stat_refresh(
        self,
        stat_id: str,
    ) -> tuple[bool, str]:
        source, record_id = parse_stat_id(stat_id)
        record = await asyncio.to_thread(get_stat, stat_id)
        if record is None:
            return False, "Stat was not found."
        if record["status"] != "active":
            return False, "Archived stats cannot be refreshed."

        if source == "roster":
            rows = await self._tracked_rows()
            row = next((item for item in rows if item[0] == record_id), None)
            refresher = self._refresh_row
        elif source == "report":
            rows = await self._tracked_report_rows(report_id=record_id)
            row = rows[0] if rows else None
            refresher = self._refresh_report_row
        else:
            rows = await self._tracked_activity_rows(report_id=record_id)
            row = rows[0] if rows else None
            refresher = self._refresh_activity_report_row

        if row is None:
            await asyncio.to_thread(
                update_stat_result,
                stat_id,
                False,
                "Active stat record was not found.",
            )
            return False, "Active stat record was not found."
        success = await refresher(row)
        if success:
            await self._snapshot_dashboard_stat_members(stat_id, record)
            await asyncio.to_thread(
                update_stat_result,
                stat_id,
                True,
                "Refreshed successfully.",
            )
            return True, f"{stat_id} refreshed successfully."
        await asyncio.to_thread(
            update_stat_result,
            stat_id,
            False,
            "Discord message refresh failed.",
        )
        return False, f"{stat_id} could not be refreshed."

    async def _snapshot_dashboard_stat_members(
        self,
        stat_id: str,
        record: dict,
    ) -> None:
        guild = self.bot.get_guild(record["guild_id"])
        if guild is None:
            await asyncio.to_thread(replace_member_snapshot, stat_id, [])
            return
        members = []
        if record["source"] == "roster":
            role = guild.get_role(record["role_id"])
            if role:
                members = [
                    self._dashboard_member_row(member, role.id, "member")
                    for member in role.members
                ]
        elif record["source"] == "report":
            rows = await self._tracked_report_rows(report_id=record["id"])
            if not rows:
                await asyncio.to_thread(replace_member_snapshot, stat_id, [])
                return
            row = rows[0]
            report_type = row[4]
            if report_type == "rolecompare":
                role_1 = guild.get_role(row[5])
                role_2 = guild.get_role(row[6])
                if role_1 and role_2:
                    data = self._calculate_rolecompare(role_1, role_2)
                    for category in ("role_1_only", "role_2_only", "both"):
                        for member in data[category]:
                            role_id = (
                                role_2.id
                                if category == "role_2_only"
                                else role_1.id
                            )
                            members.append(
                                self._dashboard_member_row(
                                    member,
                                    role_id,
                                    category,
                                )
                            )
            elif report_type == "missingrole":
                has_role = guild.get_role(row[7])
                missing_role = guild.get_role(row[8])
                if has_role and missing_role:
                    data = self._calculate_missingrole(has_role, missing_role)
                    members = [
                        self._dashboard_member_row(
                            member,
                            has_role.id,
                            "missing_role",
                        )
                        for member in data["members"]
                    ]
        await asyncio.to_thread(replace_member_snapshot, stat_id, members)

    @staticmethod
    def _dashboard_member_row(
        member: discord.Member,
        role_id: int,
        category: str,
    ) -> dict:
        return {
            "discord_user_id": member.id,
            "username": getattr(member, "name", None),
            "display_name": member.display_name,
            "role_id": role_id,
            "joined_at": (
                member.joined_at.astimezone(datetime.timezone.utc).isoformat()
                if member.joined_at
                else None
            ),
            "category": category,
        }

    async def _build_activity_report_embed(
        self,
        guild: discord.Guild,
        report_type: str,
        config: dict,
    ) -> discord.Embed:
        builders = {
            "overview": self._build_activity_overview_embed,
            "channels": self._build_activity_channels_embed,
            "quiet": self._build_activity_quiet_embed,
            "members": self._build_activity_members_embed,
            "trends": self._build_activity_trends_embed,
            "categories": self._build_activity_categories_embed,
            "heatmap": self._build_activity_heatmap_embed,
            "vc": self._build_activity_vc_embed,
            "importinfo": self._build_activity_importinfo_embed,
        }
        builder = builders.get(report_type)
        if builder is None:
            raise ValueError(f"Unsupported activity report type: {report_type}")
        return await builder(guild, config)

    async def _build_activity_overview_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        source = config.get("source", "all")
        period_label = self._activity_period_label(days)
        data = await self._activity_overview_data(
            guild.id,
            days,
            source,
            self._activity_excluded_ids_for_guild(guild),
        )
        embed = discord.Embed(
            title=f"Community activity overview — {period_label} ({source})",
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
            description=(
                "This includes all currently tracked and imported activity data."
                if days is None
                else None
            ),
        )
        for name, value in (
            ("Period", period_label),
            ("Data range", data["data_range"]),
            ("Messages tracked", f"{data['total_messages']:,}"),
            ("Unique active members", f"{data['active_members']:,}"),
            ("Joins / Leaves", f"{data['joins']:,} / {data['leaves']:,}"),
            ("Top text channel", data["top_text_channel"]),
            ("Busiest day", data["busiest_day"]),
            ("Quietest day", data["quietest_day"]),
        ):
            embed.add_field(name=name, value=value, inline=True)
        if data["vc_available"]:
            embed.add_field(
                name="Tracked VC time",
                value=format_duration(data["vc_seconds"]),
                inline=True,
            )
            embed.add_field(
                name="Top VC channel",
                value=data["top_vc_channel"],
                inline=True,
            )
        else:
            embed.add_field(
                name="Voice activity",
                value="VC activity tracking is not available yet.",
                inline=False,
            )
        embed.add_field(
            name="Deterministic vibe summary",
            value=data["vibe"],
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Activity tracking began {data['tracking_started']}. "
                "Older periods may be incomplete."
            )
        )
        return embed

    async def _build_activity_channels_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        source = config.get("source", "all")
        limit = int(config.get("limit", 10))
        source_sql, source_parameters = self._activity_source_filter(source)
        date_sql, date_parameters = self._activity_date_filter(days)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            self._activity_excluded_ids_for_guild(guild)
        )
        rows = await self._fetchall(
            f"""
            SELECT channel_id, MAX(channel_name), SUM(message_count),
                   COUNT(DISTINCT user_id)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY channel_id
            ORDER BY SUM(message_count) DESC
            LIMIT ?
            """,
            (
                guild.id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
                limit,
            ),
        )
        total_row = await self._fetchone(
            f"""
            SELECT COALESCE(SUM(message_count), 0)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            """,
            (
                guild.id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        total = total_row[0] if total_row else 0
        embed = discord.Embed(
            title=(
                f"Top text channels — {self._activity_period_label(days)} "
                f"({source})"
            ),
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
            description=(
                "This includes all currently tracked and imported activity data."
                if days is None
                else None
            ),
        )
        if not rows:
            embed.description = "No text activity has been tracked for this period."
        for index, (channel_id, channel_name, messages, posters) in enumerate(
            rows,
            start=1,
        ):
            percentage = messages / total * 100 if total else 0
            embed.add_field(
                name=f"{index}. {safe_channel_display(channel_id, channel_name)}",
                value=(
                    f"**{messages:,}** messages • **{posters:,}** unique posters "
                    f"• **{percentage:.1f}%** of tracked messages"
                ),
                inline=False,
            )
        embed.set_footer(text="Use /stats activity export for complete data.")
        return embed

    async def _build_activity_quiet_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        source = config.get("source", "all")
        limit = int(config.get("limit", 10))
        source_sql, source_parameters = self._activity_source_filter(source)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            self._activity_excluded_ids_for_guild(guild)
        )
        if days is None:
            rows = await self._fetchall(
                f"""
                SELECT channel_id, SUM(message_count), MAX(activity_hour)
                FROM stats_message_activity
                WHERE guild_id = ? {source_sql} {excluded_sql}
                GROUP BY channel_id
                """,
                (guild.id, *source_parameters, *excluded_parameters),
            )
        else:
            cutoff = self._activity_cutoff(days)
            rows = await self._fetchall(
                f"""
                SELECT channel_id,
                       SUM(CASE WHEN activity_hour >= ?
                           THEN message_count ELSE 0 END),
                       MAX(CASE WHEN activity_hour >= ?
                           THEN activity_hour END)
                FROM stats_message_activity
                WHERE guild_id = ? {source_sql} {excluded_sql}
                GROUP BY channel_id
                """,
                (
                    cutoff,
                    cutoff,
                    guild.id,
                    *source_parameters,
                    *excluded_parameters,
                ),
            )
        tracked = {
            row[0]: {"messages": row[1] or 0, "last": row[2]} for row in rows
        }
        tracking_started = await self._tracking_started_datetime()
        candidates = []
        for text_channel in guild.text_channels:
            if guild.me and not text_channel.permissions_for(guild.me).view_channel:
                continue
            data = tracked.get(text_channel.id)
            if (
                data is None
                and tracking_started
                and text_channel.created_at >= tracking_started
            ):
                continue
            candidates.append(
                (
                    data["messages"] if data else 0,
                    data["last"] if data else None,
                    text_channel,
                )
            )
        candidates.sort(key=lambda item: (item[0], item[1] or ""))
        embed = discord.Embed(
            title=(
                f"Low-activity text channels — "
                f"{self._activity_period_label(days)} ({source})"
            ),
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
            description=(
                "Neutral activity signal for channel planning. "
                "This does not evaluate individual members."
                + (
                    "\n\nThis includes all currently tracked and imported "
                    "activity data."
                    if days is None
                    else ""
                )
            ),
        )
        for messages, last_activity, text_channel in candidates[:limit]:
            last_text = (
                f"<t:{int(datetime.datetime.fromisoformat(last_activity).timestamp())}:R>"
                if last_activity
                else "No tracked activity yet"
            )
            embed.add_field(
                name=text_channel.mention,
                value=f"**{messages:,}** messages • Last tracked: {last_text}",
                inline=False,
            )
        if not candidates:
            embed.add_field(
                name="No results",
                value="No eligible visible text channels were found.",
                inline=False,
            )
        return embed

    async def _build_activity_members_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        source = config.get("source", "all")
        limit = int(config.get("limit", 10))
        include_left = bool(config.get("include_left_members", False))
        source_sql, source_parameters = self._activity_source_filter(source)
        date_sql, date_parameters = self._activity_date_filter(days)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            self._activity_excluded_ids_for_guild(guild)
        )
        rows = await self._fetchall(
            f"""
            SELECT user_id, MAX(username), MAX(display_name),
                   SUM(message_count) AS total
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY user_id
            ORDER BY total DESC
            """,
            (
                guild.id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        filtered_rows = []
        for user_id, username, display_name, total in rows:
            member = current_member(guild, user_id)
            if self._activity_member_excluded(member):
                continue
            if member is None and not include_left:
                continue
            filtered_rows.append(
                (
                    user_id,
                    member.name if member else username,
                    member.display_name if member else display_name,
                    total,
                    member is not None,
                )
            )
            if len(filtered_rows) >= limit:
                break
        scope = "Includes left members" if include_left else "Current members only"
        embed = discord.Embed(
            title=(
                f"Top text participants — {self._activity_period_label(days)} "
                f"({source})"
            ),
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
            description=(
                "Message counts only; no activity score or inactivity judgment."
                f"\n**{scope}.**"
                + (
                    "\n\nThis includes all currently tracked and imported "
                    "activity data."
                    if days is None
                    else ""
                )
            ),
        )
        warning = member_filter_warning(self.bot, guild)
        if warning and not include_left:
            embed.description += f"\n⚠️ {warning}"
        if not filtered_rows:
            embed.add_field(
                name="No results",
                value="No matching member activity was found for this period.",
                inline=False,
            )
        for index, (user_id, username, display_name, total, is_current) in enumerate(
            filtered_rows,
            start=1,
        ):
            label = display_name or username or str(user_id)
            identity = f"<@{user_id}>" if is_current else f"`{user_id}` • Left server"
            embed.add_field(
                name=f"{index}. {discord.utils.escape_markdown(label)}",
                value=f"{identity} • **{total:,}** messages",
                inline=False,
            )
        return embed

    async def _build_activity_trends_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = int(config.get("days", 30))
        source = config.get("source", "all")
        source_sql, source_parameters = self._activity_source_filter(source)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            self._activity_excluded_ids_for_guild(guild)
        )
        current_start = self._activity_cutoff(days)
        previous_start = self._activity_cutoff(days * 2)
        totals = await self._fetchone(
            f"""
            SELECT
                COALESCE(SUM(CASE WHEN activity_hour >= ?
                    THEN message_count ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN activity_hour >= ? AND activity_hour < ?
                    THEN message_count ELSE 0 END), 0),
                COUNT(DISTINCT CASE WHEN activity_hour >= ? THEN user_id END),
                COUNT(DISTINCT CASE WHEN activity_hour >= ? AND activity_hour < ?
                    THEN user_id END)
            FROM stats_message_activity
            WHERE guild_id = ? AND activity_hour >= ? {source_sql} {excluded_sql}
            """,
            (
                current_start,
                previous_start,
                current_start,
                current_start,
                previous_start,
                current_start,
                guild.id,
                previous_start,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        channel_rows = await self._fetchall(
            f"""
            SELECT channel_id, MAX(channel_name),
                   COALESCE(SUM(CASE WHEN activity_hour >= ?
                       THEN message_count ELSE 0 END), 0),
                   COALESCE(SUM(CASE WHEN activity_hour >= ? AND activity_hour < ?
                       THEN message_count ELSE 0 END), 0)
            FROM stats_message_activity
            WHERE guild_id = ? AND activity_hour >= ? {source_sql} {excluded_sql}
            GROUP BY channel_id
            """,
            (
                current_start,
                previous_start,
                current_start,
                guild.id,
                previous_start,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        day_rows = await self._fetchall(
            f"""
            SELECT activity_date, SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? AND activity_hour >= ? {source_sql} {excluded_sql}
            GROUP BY activity_date
            ORDER BY SUM(message_count) DESC
            """,
            (guild.id, current_start, *source_parameters, *excluded_parameters),
        )
        current_messages, previous_messages, current_members, previous_members = (
            int(value or 0) for value in totals
        )
        changes = [
            (int(current or 0) - int(previous or 0), channel_id, channel_name)
            for channel_id, channel_name, current, previous in channel_rows
        ]
        growing = sorted((row for row in changes if row[0] > 0), reverse=True)[:5]
        declining = sorted((row for row in changes if row[0] < 0))[:5]
        embed = discord.Embed(
            title=f"Activity trends — {days} days ({source})",
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
        )
        if previous_messages == 0 and previous_members == 0:
            embed.description = (
                "Previous-period comparison is limited because no matching "
                "activity was found in that window."
            )
        embed.add_field(
            name="Messages",
            value=(
                f"Current: **{current_messages:,}**\n"
                f"Previous: **{previous_messages:,}**\n"
                f"Change: **{_percent_change(current_messages, previous_messages)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Active members",
            value=(
                f"Current: **{current_members:,}**\n"
                f"Previous: **{previous_members:,}**\n"
                f"Change: **{_percent_change(current_members, previous_members)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Current-period days",
            value=(
                f"Busiest: **{day_rows[0][0]}** — {day_rows[0][1]:,}\n"
                f"Quietest: **{day_rows[-1][0]}** — {day_rows[-1][1]:,}"
                if day_rows
                else "No matching activity."
            ),
            inline=False,
        )
        for field_name, rows, empty_text in (
            ("Top growing channels", growing, "No channels increased."),
            ("Top declining channels", declining, "No channels declined."),
        ):
            embed.add_field(
                name=field_name,
                value=(
                    "\n".join(
                        f"{safe_channel_display(channel_id, name)} — "
                        f"**{change:+,}**"
                        for change, channel_id, name in rows
                    )
                    or empty_text
                )[:1024],
                inline=True,
            )
        embed.set_footer(
            text=f"Current {days} days compared with the preceding {days} days."
        )
        return embed

    async def _build_activity_categories_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        source = config.get("source", "all")
        limit = int(config.get("limit", 10))
        source_sql, source_parameters = self._activity_source_filter(source)
        date_sql, date_parameters = self._activity_date_filter(days)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            self._activity_excluded_ids_for_guild(guild)
        )
        rows = await self._fetchall(
            f"""
            SELECT channel_id, MAX(channel_name), user_id, SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY channel_id, user_id
            """,
            (
                guild.id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        category_config = load_channel_categories()
        categories = defaultdict(
            lambda: {"messages": 0, "members": set(), "channels": defaultdict(int)}
        )
        for channel_id, channel_name, user_id, messages in rows:
            category, included = get_channel_category(channel_id, category_config)
            if not included:
                continue
            item = categories[category]
            item["messages"] += int(messages or 0)
            item["members"].add(user_id)
            item["channels"][(channel_id, channel_name)] += int(messages or 0)
        ranked = sorted(
            categories.items(),
            key=lambda item: item[1]["messages"],
            reverse=True,
        )
        total = sum(item["messages"] for _, item in ranked)
        embed = discord.Embed(
            title=(
                f"Activity by category — {self._activity_period_label(days)} "
                f"({source})"
            ),
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
        )
        if not category_config:
            embed.description = (
                "No channel category config found. Channels are grouped as "
                "Uncategorized."
            )
        if not ranked:
            embed.add_field(
                name="No results",
                value="No included message activity was found for this period.",
                inline=False,
            )
        for category, item in ranked[:limit]:
            top_channel, top_messages = max(
                item["channels"].items(),
                key=lambda channel_item: channel_item[1],
            )
            percentage = item["messages"] / total * 100 if total else 0
            embed.add_field(
                name=category[:256],
                value=(
                    f"**{item['messages']:,}** messages • "
                    f"**{len(item['members']):,}** active members • "
                    f"**{percentage:.1f}%**\n"
                    f"Top: {safe_channel_display(*top_channel)} "
                    f"({top_messages:,})"
                )[:1024],
                inline=False,
            )
        return embed

    async def _build_activity_heatmap_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        source = config.get("source", "all")
        timezone_name = str(config.get("timezone", "America/Chicago"))
        source_sql, source_parameters = self._activity_source_filter(source)
        date_sql, date_parameters = self._activity_date_filter(days)
        excluded_sql, excluded_parameters = self._excluded_user_sql(
            self._activity_excluded_ids_for_guild(guild)
        )
        rows = await self._fetchall(
            f"""
            SELECT activity_hour, SUM(message_count)
            FROM stats_message_activity
            WHERE guild_id = ? {date_sql} {source_sql} {excluded_sql}
            GROUP BY activity_hour
            """,
            (
                guild.id,
                *date_parameters,
                *source_parameters,
                *excluded_parameters,
            ),
        )
        try:
            selected_timezone = ZoneInfo(timezone_name)
            timezone_label = timezone_name
        except (ZoneInfoNotFoundError, ValueError):
            selected_timezone = ZoneInfo("America/Chicago")
            timezone_label = "America/Chicago (fallback)"
        day_totals = {day: 0 for day in range(7)}
        hour_totals = {hour: 0 for hour in range(24)}
        combo_totals = defaultdict(int)
        for activity_hour, messages in rows:
            try:
                parsed = datetime.datetime.fromisoformat(
                    str(activity_hour).replace("Z", "+00:00")
                )
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
                local_hour = parsed.astimezone(selected_timezone)
            except (TypeError, ValueError, OverflowError):
                continue
            count = int(messages or 0)
            day_totals[local_hour.weekday()] += count
            hour_totals[local_hour.hour] += count
            combo_totals[(local_hour.weekday(), local_hour.hour)] += count
        day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        embed = discord.Embed(
            title=(
                f"Activity heatmap — {self._activity_period_label(days)} "
                f"({source})"
            ),
            color=discord.Color(COLOR),
            description=f"Times shown in **{timezone_label}**.",
            timestamp=self._utcnow(),
        )
        if not rows or not any(hour_totals.values()):
            embed.add_field(
                name="No results",
                value="No matching hourly activity was found.",
                inline=False,
            )
            return embed
        busiest_day = max(day_totals, key=day_totals.get)
        busiest_hour = max(hour_totals, key=hour_totals.get)
        quietest_hour = min(hour_totals, key=hour_totals.get)
        embed.add_field(
            name="Highlights",
            value=(
                f"Most active day: **{day_names[busiest_day]}** "
                f"({day_totals[busiest_day]:,})\n"
                f"Most active hour: **{self._hour_label(busiest_hour)}** "
                f"({hour_totals[busiest_hour]:,})\n"
                f"Quietest hour: **{self._hour_label(quietest_hour)}** "
                f"({hour_totals[quietest_hour]:,})"
            ),
            inline=False,
        )
        embed.add_field(
            name="Top day/hour combinations",
            value="\n".join(
                f"{day_names[day]} {self._hour_label(hour)} — **{count:,}**"
                for (day, hour), count in sorted(
                    combo_totals.items(),
                    key=lambda item: item[1],
                    reverse=True,
                )[:3]
            ),
            inline=False,
        )
        embed.add_field(
            name="By day",
            value="\n".join(
                f"`{day_names[day]}` {day_totals[day]:,}" for day in range(7)
            ),
            inline=True,
        )
        blocks = {
            "Overnight": sum(hour_totals[hour] for hour in range(0, 6)),
            "Morning": sum(hour_totals[hour] for hour in range(6, 12)),
            "Afternoon": sum(hour_totals[hour] for hour in range(12, 18)),
            "Evening": sum(hour_totals[hour] for hour in range(18, 24)),
        }
        embed.add_field(
            name="By time block",
            value="\n".join(
                f"`{name:<9}` {count:,}" for name, count in blocks.items()
            ),
            inline=True,
        )
        return embed

    async def _build_activity_vc_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        days = config.get("days")
        limit = int(config.get("limit", 10))
        include_left = bool(config.get("include_left_members", False))
        if not await self._table_exists("vc_sessions"):
            return discord.Embed(
                title="Voice activity",
                description="VC activity tracking is not available yet.",
                color=discord.Color(COLOR),
                timestamp=self._utcnow(),
            )
        date_sql, date_parameters = self._activity_date_filter(
            days,
            column="left_at",
        )
        vc_excluded_sql, vc_excluded_parameters = self._excluded_user_sql(
            self._vc_excluded_ids_for_guild(guild)
        )
        total = await self._fetchone(
            f"""
            SELECT COALESCE(SUM(duration_seconds), 0), COUNT(*)
            FROM vc_sessions
            WHERE guild_id = ? {date_sql} {vc_excluded_sql}
            """,
            (guild.id, *date_parameters, *vc_excluded_parameters),
        )
        channel_rows = await self._fetchall(
            f"""
            SELECT channel_id, MAX(channel_name), SUM(duration_seconds)
            FROM vc_sessions
            WHERE guild_id = ? {date_sql} {vc_excluded_sql}
            GROUP BY channel_id
            ORDER BY SUM(duration_seconds) DESC
            LIMIT ?
            """,
            (guild.id, *date_parameters, *vc_excluded_parameters, limit),
        )
        member_rows = await self._fetchall(
            f"""
            SELECT user_id, MAX(username), MAX(display_name),
                   SUM(duration_seconds)
            FROM vc_sessions
            WHERE guild_id = ? {date_sql} {vc_excluded_sql}
            GROUP BY user_id
            ORDER BY SUM(duration_seconds) DESC
            """,
            (guild.id, *date_parameters, *vc_excluded_parameters),
        )
        filtered = []
        for user_id, username, display_name, seconds in member_rows:
            member = current_member(guild, user_id)
            if member_is_excluded(
                member,
                user_ids=self.vc_excluded_user_ids,
                role_ids=self.vc_excluded_role_ids,
            ):
                continue
            if member is None and not include_left:
                continue
            filtered.append(
                (
                    user_id,
                    member.name if member else username,
                    member.display_name if member else display_name,
                    seconds,
                    member is not None,
                )
            )
            if len(filtered) >= limit:
                break
        embed = discord.Embed(
            title=f"Voice activity — {self._activity_period_label(days)}",
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
            description=(
                f"**{format_duration(total[0])}** tracked across "
                f"**{total[1]:,}** completed sessions."
                + (
                    "\n\nThis includes all currently tracked voice activity data."
                    if days is None
                    else ""
                )
            ),
        )
        if channel_rows:
            embed.add_field(
                name="Top VC channels",
                value="\n".join(
                    f"{index}. {safe_channel_display(row[0], row[1])} — "
                    f"**{format_duration(row[2])}**"
                    for index, row in enumerate(channel_rows, start=1)
                )[:1024],
                inline=False,
            )
        if filtered:
            embed.add_field(
                name="Top VC members",
                value="\n".join(
                    f"{index}. "
                    f"{f'<@{row[0]}>' if row[4] else discord.utils.escape_markdown(row[2] or row[1] or str(row[0])) + ' — Left server'} "
                    f"— **{format_duration(row[3])}**"
                    for index, row in enumerate(filtered, start=1)
                )[:1024],
                inline=False,
            )
        scope = "Includes left members" if include_left else "Current members only"
        warning = member_filter_warning(self.bot, guild)
        embed.set_footer(
            text=(
                f"Member ranking: {scope}."
                + (f" {warning}" if warning and not include_left else "")
            )
        )
        return embed

    async def _build_activity_importinfo_embed(
        self,
        guild: discord.Guild,
        config: dict,
    ) -> discord.Embed:
        limit = int(config.get("limit", 10))
        rows = await self._fetchall(
            """
            SELECT import_batch_id, filename, channel_id, channel_name,
                   messages_seen, messages_imported, duplicates_skipped,
                   messages_skipped, earliest_message_at, latest_message_at,
                   imported_at, status
            FROM stats_activity_imports
            WHERE guild_id = ?
            ORDER BY imported_at DESC, id DESC
            LIMIT ?
            """,
            (guild.id, limit),
        )
        embed = discord.Embed(
            title="Recent historical activity imports",
            color=discord.Color(COLOR),
            timestamp=self._utcnow(),
        )
        if not rows:
            embed.description = "No historical activity imports have been recorded."
        for row in rows:
            (
                batch_id,
                filename,
                channel_id,
                channel_name,
                seen,
                imported,
                duplicates,
                skipped,
                earliest,
                latest,
                imported_at,
                status,
            ) = row
            embed.add_field(
                name=(
                    f"{os.path.basename(filename or 'Unknown file')[:80]} • "
                    f"{status or 'unknown'}"
                ),
                value=(
                    f"Batch `{(batch_id or 'unknown')[:8]}` • "
                    f"{safe_channel_display(channel_id or 0, channel_name)} "
                    f"(`{channel_id or 0}`)\n"
                    f"Seen **{seen or 0:,}** • Imported **{imported or 0:,}** • "
                    f"Duplicates **{duplicates or 0:,}** • "
                    f"Skipped **{skipped or 0:,}**\n"
                    f"Messages: {(earliest or 'n/a')[:10]} to "
                    f"{(latest or 'n/a')[:10]}\n"
                    f"Imported: {imported_at or 'n/a'}"
                )[:1024],
                inline=False,
            )
        return embed

    async def _send_activity_report(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        channel: Optional[discord.TextChannel],
        *,
        report_type: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> None:
        assets = []
        if report_type:
            try:
                assets = await self._build_activity_report_assets(
                    interaction.guild,
                    report_type,
                    config or {},
                )
            except Exception:
                await interaction.followup.send(
                    "I could not generate the activity graphic.",
                    ephemeral=True,
                )
                return
        if channel is None:
            if assets:
                files = [
                    discord.File(io.BytesIO(data), filename=filename)
                    for filename, data in assets
                ]
                await interaction.followup.send(files=files, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not self._valid_target_channel(interaction, channel):
            await interaction.followup.send(
                "The activity report must be posted in a text channel "
                "in this server.",
                ephemeral=True,
            )
            return
        try:
            if assets:
                files = [
                    discord.File(io.BytesIO(data), filename=filename)
                    for filename, data in assets
                ]
                message = await channel.send(files=files)
            else:
                message = await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not post the activity report in that channel. "
                "Please check my channel permissions.",
                ephemeral=True,
            )
            return
        if report_type:
            now = self._utcnow().isoformat()
            try:
                await self.bot.db.execute(
                    """
                    INSERT INTO tracked_activity_reports (
                        guild_id,
                        channel_id,
                        message_id,
                        report_type,
                        config_json,
                        created_by,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        interaction.guild_id,
                        channel.id,
                        message.id,
                        report_type,
                        json.dumps(config or {}, separators=(",", ":")),
                        interaction.user.id,
                        now,
                        now,
                    ),
                )
                await self.bot.db.commit()
            except Exception:
                try:
                    await message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await interaction.followup.send(
                    "The report was generated, but I could not save its refresh "
                    "configuration, so the channel post was removed.",
                    ephemeral=True,
                )
                return
        await interaction.followup.send(
            (
                f"Posted and tracked the activity report in {channel.mention}."
                if report_type
                else f"Posted the activity report in {channel.mention}."
            ),
            ephemeral=True,
        )

    async def _send_activity_file(
        self,
        interaction: discord.Interaction,
        text: str,
        file: discord.File,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if channel is None:
            await interaction.followup.send(text, file=file, ephemeral=True)
            return
        if not self._valid_target_channel(interaction, channel):
            await interaction.followup.send(
                "The activity export must be posted in a text channel "
                "in this server.",
                ephemeral=True,
            )
            return
        try:
            await channel.send(content=text, file=file)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not post the activity export in that channel. "
                "Please check my channel permissions.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Posted the activity export in {channel.mention}.",
            ephemeral=True,
        )

    @staticmethod
    def _member_username(member: discord.Member) -> str:
        return (
            getattr(member, "name", None)
            or getattr(member, "global_name", None)
            or str(member.id)
        )

    @staticmethod
    def _get_channel(guild: discord.Guild, channel_id: int):
        get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
        if get_channel_or_thread:
            return get_channel_or_thread(channel_id)
        return guild.get_channel(channel_id)

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        if attachment.content_type:
            return attachment.content_type.startswith("image/")
        return attachment.filename.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        )

    @staticmethod
    def _utcnow() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            command_name = getattr(interaction.command, "name", None)
            if command_name in {"delete", "reset"}:
                message = f"Only administrators can use /stats {command_name}."
            else:
                message = PERMISSION_DENIED_MESSAGE
        else:
            message = "The stats command could not be completed."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stats(bot))
