"""Scheduled member reminders for Bro Eden."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.settings import get_csv_ids_setting, get_setting
from utils.ui import SUCCESS_COLOR, branded_embed, error_embed, truncate


logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"
REMINDER_CHECK_SECONDS = 45
MAX_REMINDER_MESSAGE_LENGTH = 4_000
MAX_REMINDERS_PER_MANAGE = 25
ALLOWED_MENTIONS = discord.AllowedMentions(users=True, roles=False, everyone=False)
CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
]
DATETIME_FORMATS = (
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I %p",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I %p",
    "%m/%d/%Y %H:%M",
)
HELPFUL_DATE_ERROR = (
    "I could not understand that date/time. Try `2026-07-01 7:30 PM` "
    "or `07/01/2026 7:30 PM`."
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return utc_now().isoformat()


def parse_id_set(value: Optional[str]) -> set[int]:
    result: set[int] = set()
    for item in re.findall(r"\d+", value or ""):
        try:
            parsed = int(item)
        except ValueError:
            continue
        if parsed > 0:
            result.add(parsed)
    return result


def configured_timezone_name() -> str:
    return (
        get_setting("REMINDER_TIMEZONE")
        or os.getenv("REMINDER_TIMEZONE")
        or os.getenv("TZ")
        or DEFAULT_TIMEZONE
    )


def reminder_timezone() -> ZoneInfo:
    name = configured_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid reminder timezone %s; using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def parse_local_datetime(value: str, tz: ZoneInfo) -> datetime:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise ValueError(HELPFUL_DATE_ERROR)
    for date_format in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        local_value = parsed.replace(tzinfo=tz)
        return local_value.astimezone(timezone.utc)
    raise ValueError(HELPFUL_DATE_ERROR)


def parse_utc_text(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def discord_timestamp(value: Any, style: str = "f") -> str:
    try:
        parsed = parse_utc_text(value)
    except (TypeError, ValueError):
        return str(value or "unknown")
    return f"<t:{int(parsed.timestamp())}:{style}>"


def local_datetime_text(value: Any, tz: ZoneInfo) -> str:
    try:
        parsed = parse_utc_text(value).astimezone(tz)
    except (TypeError, ValueError):
        return str(value or "unknown")
    return parsed.strftime("%B %-d, %Y at %-I:%M %p %Z")


def local_datetime_input(value: Any, tz: ZoneInfo) -> str:
    try:
        parsed = parse_utc_text(value).astimezone(tz)
    except (TypeError, ValueError):
        return ""
    return parsed.strftime("%Y-%m-%d %-I:%M %p")


def user_mention(user_id: Any) -> str:
    return f"<@{user_id}>"


def channel_mention(channel_id: Any) -> str:
    return f"<#{channel_id}>"


def channel_reference(channel: Any, fallback_id: Any = None) -> str:
    return (
        getattr(channel, "mention", None)
        or (channel_mention(fallback_id) if fallback_id else "unknown channel")
    )


def row_to_dict(cursor: Any, row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    columns = [entry[0] for entry in cursor.description or ()]
    return dict(zip(columns, row))


def is_sendable_channel(channel: Any) -> bool:
    return hasattr(channel, "send") and hasattr(channel, "permissions_for")


def send_permission_error(bot: commands.Bot, channel: Any) -> Optional[str]:
    guild = getattr(channel, "guild", None)
    member = getattr(guild, "me", None)
    bot_user = getattr(bot, "user", None)
    if member is None and guild is not None and bot_user is not None:
        member = guild.get_member(bot_user.id)
    if member is None or not hasattr(channel, "permissions_for"):
        return None
    permissions = channel.permissions_for(member)
    missing = []
    if not getattr(permissions, "view_channel", False):
        missing.append("View Channel")
    if getattr(channel, "type", None) in {
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
    }:
        if not getattr(permissions, "send_messages_in_threads", False):
            missing.append("Send Messages in Threads")
    elif not getattr(permissions, "send_messages", False):
        missing.append("Send Messages")
    if not getattr(permissions, "embed_links", False):
        missing.append("Embed Links")
    if missing:
        return "Missing bot permission(s): " + ", ".join(missing)
    return None


class ReminderEditModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "ReminderCog",
        reminder_id: int,
        row: dict[str, Any],
    ) -> None:
        super().__init__(title=f"Edit reminder #{reminder_id}", timeout=300)
        self.cog = cog
        self.reminder_id = reminder_id
        self.message = discord.ui.TextInput(
            label="Message",
            default=str(row["message"])[:MAX_REMINDER_MESSAGE_LENGTH],
            style=discord.TextStyle.paragraph,
            max_length=MAX_REMINDER_MESSAGE_LENGTH,
        )
        self.date_time = discord.ui.TextInput(
            label="Date/time",
            default=local_datetime_input(row["scheduled_at_utc"], reminder_timezone()),
            placeholder="2026-07-01 7:30 PM",
            max_length=80,
        )
        self.add_item(self.message)
        self.add_item(self.date_time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_modal_edit(
            interaction,
            self.reminder_id,
            message=str(self.message.value),
            date_time=str(self.date_time.value),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self.cog.handle_component_error(interaction, "reminder edit modal", error)


class ReminderSelect(discord.ui.Select):
    def __init__(self, cog: "ReminderCog", rows: list[dict[str, Any]]) -> None:
        options = []
        for row in rows[:MAX_REMINDERS_PER_MANAGE]:
            options.append(
                discord.SelectOption(
                    label=f"#{row['id']} for {row['target_user_id']}",
                    value=str(row["id"]),
                    description=truncate(
                        f"{discord_timestamp(row['scheduled_at_utc'])} • "
                        f"#{row['channel_id']} • {row['message']}",
                        100,
                    ),
                )
            )
        super().__init__(
            placeholder="Choose a reminder",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ReminderManageView):
            view.selected_reminder_id = int(self.values[0])
            row = await self.cog.get_manageable_reminder(
                interaction.guild_id,
                interaction.user.id,
                view.selected_reminder_id,
            )
            if row is None:
                await interaction.response.send_message(
                    "That reminder is no longer available.",
                    ephemeral=True,
                )
                return
            await interaction.response.edit_message(
                embed=self.cog.detail_embed(row),
                view=view,
            )


class ReminderChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog: "ReminderCog", reminder_id: int) -> None:
        super().__init__(
            placeholder="Choose a new reminder channel",
            channel_types=CHANNEL_TYPES,
            min_values=1,
            max_values=1,
        )
        self.cog = cog
        self.reminder_id = reminder_id

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = self.values[0]
        await self.cog.update_reminder_channel(interaction, self.reminder_id, channel)


class ReminderTargetSelect(discord.ui.UserSelect):
    def __init__(self, cog: "ReminderCog", reminder_id: int) -> None:
        super().__init__(
            placeholder="Choose a new reminder target",
            min_values=1,
            max_values=1,
        )
        self.cog = cog
        self.reminder_id = reminder_id

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self.values[0]
        await self.cog.update_reminder_target(interaction, self.reminder_id, target)


class ReminderFieldEditView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", reminder_id: int, field: str) -> None:
        super().__init__(timeout=300)
        if field == "channel":
            self.add_item(ReminderChannelSelect(cog, reminder_id))
        elif field == "target":
            self.add_item(ReminderTargetSelect(cog, reminder_id))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        child = self.children[0] if self.children else None
        cog = getattr(child, "cog", None)
        if cog is not None:
            await cog.handle_component_error(
                interaction,
                f"reminder selector ({type(item).__name__})",
                error,
            )


class ReminderManageView(discord.ui.View):
    def __init__(
        self,
        cog: "ReminderCog",
        owner_user_id: int,
        rows: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_user_id = owner_user_id
        self.selected_reminder_id = int(rows[0]["id"]) if rows else None
        self.add_item(ReminderSelect(cog, rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return await self.cog.ensure_staff_access(interaction)
        await interaction.response.send_message(
            "Only the person who opened this reminder panel can use it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Edit Message/Time", style=discord.ButtonStyle.primary)
    async def edit_message_time(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        reminder_id = self.selected_reminder_id
        if reminder_id is None:
            await interaction.response.send_message(
                "Choose a reminder first.",
                ephemeral=True,
            )
            return
        row = await self.cog.get_manageable_reminder(
            interaction.guild_id,
            interaction.user.id,
            reminder_id,
        )
        if row is None:
            await interaction.response.send_message(
                "That reminder is no longer available.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ReminderEditModal(self.cog, reminder_id, row))

    @discord.ui.button(label="Edit Channel", style=discord.ButtonStyle.secondary)
    async def edit_channel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.selected_reminder_id is None:
            await interaction.response.send_message("Choose a reminder first.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose the replacement channel.",
            view=ReminderFieldEditView(self.cog, self.selected_reminder_id, "channel"),
            ephemeral=True,
        )

    @discord.ui.button(label="Edit Target", style=discord.ButtonStyle.secondary)
    async def edit_target(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.selected_reminder_id is None:
            await interaction.response.send_message("Choose a reminder first.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose the replacement reminder target.",
            view=ReminderFieldEditView(self.cog, self.selected_reminder_id, "target"),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        reminder_id = self.selected_reminder_id
        if reminder_id is None:
            await interaction.response.send_message(
                "Choose a reminder first.",
                ephemeral=True,
            )
            return
        deleted = await self.cog.soft_delete_reminder(
            interaction.guild_id,
            interaction.user.id,
            reminder_id,
        )
        if not deleted:
            await interaction.response.send_message(
                "That pending reminder was not found.",
                ephemeral=True,
            )
            return
        logger.info("Reminder deleted: id=%s user_id=%s", reminder_id, interaction.user.id)
        await interaction.response.send_message(
            f"Reminder #{reminder_id} was deleted.",
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await self.cog.handle_component_error(
            interaction,
            f"reminder manage control ({type(item).__name__})",
            error,
        )


class ReminderCog(commands.Cog):
    reminder = app_commands.Group(
        name="reminder",
        description="Schedule and manage internal staff reminders",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._send_lock: Optional[asyncio.Lock] = None

    async def cog_load(self) -> None:
        await self.create_schema()
        if not self.reminder_scheduler.is_running():
            self.reminder_scheduler.start()

    async def cog_unload(self) -> None:
        if self.reminder_scheduler.is_running():
            self.reminder_scheduler.cancel()

    async def create_schema(self) -> None:
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                creator_user_id TEXT NOT NULL,
                target_user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message TEXT NOT NULL,
                scheduled_at_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'sent', 'deleted', 'failed')),
                failure_reason TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT,
                sent_at_utc TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_status_scheduled
                ON reminders (status, scheduled_at_utc);
            CREATE INDEX IF NOT EXISTS idx_reminders_guild
                ON reminders (guild_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_creator
                ON reminders (creator_user_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_target
                ON reminders (target_user_id);
            """
        )
        await self.bot.db.commit()

    async def fetch_one(
        self,
        sql: str,
        parameters: Iterable[Any] = (),
    ) -> Optional[dict[str, Any]]:
        cursor = await self.bot.db.execute(sql, tuple(parameters))
        row = await cursor.fetchone()
        result = row_to_dict(cursor, row)
        await cursor.close()
        return result

    async def fetch_all(
        self,
        sql: str,
        parameters: Iterable[Any] = (),
    ) -> list[dict[str, Any]]:
        cursor = await self.bot.db.execute(sql, tuple(parameters))
        rows = await cursor.fetchall()
        columns = [entry[0] for entry in cursor.description or ()]
        await cursor.close()
        return [dict(zip(columns, row)) for row in rows]

    def has_staff_access(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.id in get_csv_ids_setting("BOT_OWNER_USER_IDS"):
            return True
        if interaction.user.guild_permissions.administrator:
            return True
        allowed_ids = set(get_csv_ids_setting("REMINDER_ALLOWED_ROLE_IDS"))
        allowed_ids.update(parse_id_set(os.getenv("REMINDER_ALLOWED_ROLE_IDS", "")))
        allowed_ids.update(get_csv_ids_setting("STAFF_NOTES_ALLOWED_ROLE_IDS"))
        allowed_ids.update(get_csv_ids_setting("STAFF_AI_ALLOWED_ROLE_IDS"))
        allowed_ids.update(parse_id_set(get_setting("staff_role_ids", "")))
        allowed_ids.update(parse_id_set(get_setting("admin_role_ids", "")))
        return any(role.id in allowed_ids for role in interaction.user.roles)

    async def ensure_staff_access(self, interaction: discord.Interaction) -> bool:
        if self.has_staff_access(interaction):
            return True
        message = "Reminders are limited to internal staff."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    def can_target_user(
        self,
        interaction: discord.Interaction,
        target: discord.abc.User,
    ) -> bool:
        return self.has_staff_access(interaction)

    def reminder_embed(self, row: dict[str, Any]) -> discord.Embed:
        embed = branded_embed(
            "Reminder",
            description=truncate(row["message"], 4096),
            footer="Bro Eden Reminder",
        )
        embed.add_field(name="For", value=user_mention(row["target_user_id"]), inline=True)
        embed.add_field(
            name="Created by",
            value=user_mention(row["creator_user_id"]),
            inline=True,
        )
        embed.add_field(
            name="Scheduled for",
            value=local_datetime_text(row["scheduled_at_utc"], reminder_timezone()),
            inline=False,
        )
        embed.add_field(name="Reminder ID", value=f"#{row['id']}", inline=True)
        return embed

    def confirmation_embed(
        self,
        row: dict[str, Any],
        channel: Any,
    ) -> discord.Embed:
        embed = branded_embed(
            "Reminder Scheduled",
            description=truncate(row["message"], 4096),
            color=SUCCESS_COLOR,
            footer="Private confirmation",
        )
        embed.add_field(name="For", value=user_mention(row["target_user_id"]), inline=True)
        embed.add_field(
            name="Channel",
            value=channel_reference(channel, row["channel_id"]),
            inline=True,
        )
        embed.add_field(
            name="Scheduled for",
            value=(
                f"{local_datetime_text(row['scheduled_at_utc'], reminder_timezone())}\n"
                f"{discord_timestamp(row['scheduled_at_utc'])}"
            ),
            inline=False,
        )
        embed.add_field(name="Reminder ID", value=f"#{row['id']}", inline=True)
        return embed

    def detail_embed(self, row: dict[str, Any]) -> discord.Embed:
        embed = branded_embed(
            f"Reminder #{row['id']}",
            description=truncate(row["message"], 4096),
            footer="Private reminder management",
        )
        embed.add_field(name="For", value=user_mention(row["target_user_id"]), inline=True)
        embed.add_field(name="Channel", value=channel_mention(row["channel_id"]), inline=True)
        embed.add_field(
            name="Scheduled for",
            value=(
                f"{local_datetime_text(row['scheduled_at_utc'], reminder_timezone())}\n"
                f"{discord_timestamp(row['scheduled_at_utc'])}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Created by",
            value=user_mention(row["creator_user_id"]),
            inline=True,
        )
        return embed

    def list_embed(self, rows: list[dict[str, Any]]) -> discord.Embed:
        lines = []
        for row in rows[:MAX_REMINDERS_PER_MANAGE]:
            lines.append(
                f"**#{row['id']}** • {user_mention(row['target_user_id'])} • "
                f"{channel_mention(row['channel_id'])} • "
                f"{discord_timestamp(row['scheduled_at_utc'])}\n"
                f"{truncate(row['message'], 140)}"
            )
        return branded_embed(
            "Your Pending Reminders",
            description="\n\n".join(lines),
            footer="Select one to edit or delete",
        )

    async def insert_reminder(
        self,
        *,
        guild_id: int,
        creator_user_id: int,
        target_user_id: int,
        channel_id: int,
        message: str,
        scheduled_at_utc: datetime,
    ) -> dict[str, Any]:
        now = utc_now_text()
        cursor = await self.bot.db.execute(
            """
            INSERT INTO reminders (
                guild_id, creator_user_id, target_user_id, channel_id, message,
                scheduled_at_utc, status, created_at_utc, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                str(guild_id),
                str(creator_user_id),
                str(target_user_id),
                str(channel_id),
                message.strip(),
                scheduled_at_utc.astimezone(timezone.utc).isoformat(),
                now,
                now,
            ),
        )
        reminder_id = cursor.lastrowid
        await cursor.close()
        await self.bot.db.commit()
        row = await self.fetch_one(
            "SELECT * FROM reminders WHERE id = ?",
            (reminder_id,),
        )
        if row is None:
            raise RuntimeError("Reminder insert did not return a row")
        return row

    async def get_manageable_reminder(
        self,
        guild_id: Optional[int],
        user_id: int,
        reminder_id: int,
    ) -> Optional[dict[str, Any]]:
        if guild_id is None:
            return None
        return await self.fetch_one(
            """
            SELECT *
            FROM reminders
            WHERE id = ?
              AND guild_id = ?
              AND status = 'pending'
              AND (creator_user_id = ? OR target_user_id = ?)
            """,
            (reminder_id, str(guild_id), str(user_id), str(user_id)),
        )

    async def soft_delete_reminder(
        self,
        guild_id: Optional[int],
        user_id: int,
        reminder_id: int,
    ) -> bool:
        if guild_id is None:
            return False
        cursor = await self.bot.db.execute(
            """
            UPDATE reminders
            SET status = 'deleted',
                updated_at_utc = ?
            WHERE id = ?
              AND guild_id = ?
              AND status = 'pending'
              AND (creator_user_id = ? OR target_user_id = ?)
            """,
            (utc_now_text(), reminder_id, str(guild_id), str(user_id), str(user_id)),
        )
        changed = cursor.rowcount
        await cursor.close()
        await self.bot.db.commit()
        return bool(changed)

    async def update_pending_reminder(
        self,
        reminder_id: int,
        guild_id: Optional[int],
        user_id: int,
        **values: Any,
    ) -> Optional[dict[str, Any]]:
        if guild_id is None or not values:
            return None
        assignments = []
        parameters: list[Any] = []
        for key, value in values.items():
            assignments.append(f"{key} = ?")
            parameters.append(value)
        assignments.append("updated_at_utc = ?")
        parameters.append(utc_now_text())
        parameters.extend([reminder_id, str(guild_id), str(user_id), str(user_id)])
        cursor = await self.bot.db.execute(
            f"""
            UPDATE reminders
            SET {", ".join(assignments)}
            WHERE id = ?
              AND guild_id = ?
              AND status = 'pending'
              AND (creator_user_id = ? OR target_user_id = ?)
            """,
            tuple(parameters),
        )
        changed = cursor.rowcount
        await cursor.close()
        await self.bot.db.commit()
        if not changed:
            return None
        return await self.get_manageable_reminder(guild_id, user_id, reminder_id)

    async def update_reminder_channel(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
        channel: Any,
    ) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        if getattr(channel, "guild", None) and channel.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "Choose a channel in this server.",
                ephemeral=True,
            )
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await interaction.response.send_message(permission_error, ephemeral=True)
            return
        row = await self.update_pending_reminder(
            reminder_id,
            interaction.guild_id,
            interaction.user.id,
            channel_id=str(channel.id),
        )
        if row is None:
            await interaction.response.send_message(
                "That pending reminder was not found.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=self.detail_embed(row),
            ephemeral=True,
        )

    async def update_reminder_target(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
        target: discord.abc.User,
    ) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        if not self.can_target_user(interaction, target):
            await interaction.response.send_message(
                "Reminders are limited to internal staff.",
                ephemeral=True,
            )
            return
        row = await self.update_pending_reminder(
            reminder_id,
            interaction.guild_id,
            interaction.user.id,
            target_user_id=str(target.id),
        )
        if row is None:
            await interaction.response.send_message(
                "That pending reminder was not found.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=self.detail_embed(row),
            ephemeral=True,
        )

    async def apply_modal_edit(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
        *,
        message: str,
        date_time: str,
    ) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        try:
            scheduled_at = parse_local_datetime(date_time, reminder_timezone())
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if scheduled_at <= utc_now():
            await interaction.response.send_message(
                "Reminder date/time must be in the future.",
                ephemeral=True,
            )
            return
        cleaned_message = message.strip()
        if not cleaned_message:
            await interaction.response.send_message(
                "Reminder message cannot be blank.",
                ephemeral=True,
            )
            return
        row = await self.update_pending_reminder(
            reminder_id,
            interaction.guild_id,
            interaction.user.id,
            message=cleaned_message,
            scheduled_at_utc=scheduled_at.isoformat(),
        )
        if row is None:
            await interaction.response.send_message(
                "That pending reminder was not found.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=self.detail_embed(row), ephemeral=True)

    async def handle_component_error(
        self,
        interaction: discord.Interaction,
        context: str,
        error: Exception,
    ) -> None:
        logger.error(
            "Reminder component failure: context=%s error_type=%s",
            context,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "That reminder control could not be completed. Try reopening `/reminder manage`."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not deliver reminder component error")

    @reminder.command(name="add", description="Schedule a reminder")
    @app_commands.describe(
        who="Member to remind. Defaults to you.",
        message="Reminder message",
        date_time="Local date/time, like 2026-07-01 7:30 PM",
        channel="Channel where the reminder should be posted",
    )
    @app_commands.guild_only()
    async def add(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, MAX_REMINDER_MESSAGE_LENGTH],
        date_time: str,
        channel: discord.TextChannel,
        who: Optional[discord.Member] = None,
    ) -> None:
        target = who or interaction.user
        if not await self.ensure_staff_access(interaction):
            return
        cleaned_message = str(message).strip()
        if not cleaned_message:
            await interaction.response.send_message(
                "Reminder message cannot be blank.",
                ephemeral=True,
            )
            return
        if not self.can_target_user(interaction, target):
            await interaction.response.send_message(
                "Reminders are limited to internal staff.",
                ephemeral=True,
            )
            return
        if getattr(channel, "guild", None) and channel.guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "Choose a channel in this server.",
                ephemeral=True,
            )
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await interaction.response.send_message(permission_error, ephemeral=True)
            return
        try:
            scheduled_at = parse_local_datetime(date_time, reminder_timezone())
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if scheduled_at <= utc_now():
            await interaction.response.send_message(
                "Reminder date/time must be in the future.",
                ephemeral=True,
            )
            return
        try:
            row = await self.insert_reminder(
                guild_id=int(interaction.guild_id or 0),
                creator_user_id=interaction.user.id,
                target_user_id=target.id,
                channel_id=channel.id,
                message=cleaned_message,
                scheduled_at_utc=scheduled_at,
            )
        except Exception as exc:
            logger.error(
                "Reminder create failed: error_type=%s",
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.response.send_message(
                embed=error_embed(
                    "Reminder Not Saved",
                    "The reminder could not be saved. Please try again later.",
                ),
                ephemeral=True,
            )
            return
        logger.info(
            "Reminder created: id=%s guild_id=%s creator=%s target=%s channel=%s scheduled_at=%s",
            row["id"],
            interaction.guild_id,
            interaction.user.id,
            target.id,
            channel.id,
            row["scheduled_at_utc"],
        )
        await interaction.response.send_message(
            embed=self.confirmation_embed(row, channel),
            ephemeral=True,
        )

    @reminder.command(name="manage", description="Privately manage your pending reminders")
    @app_commands.guild_only()
    async def manage(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        rows = await self.fetch_all(
            """
            SELECT *
            FROM reminders
            WHERE guild_id = ?
              AND status = 'pending'
              AND (creator_user_id = ? OR target_user_id = ?)
            ORDER BY scheduled_at_utc ASC
            LIMIT ?
            """,
            (
                str(interaction.guild_id),
                str(interaction.user.id),
                str(interaction.user.id),
                MAX_REMINDERS_PER_MANAGE,
            ),
        )
        if not rows:
            await interaction.response.send_message(
                "No pending reminders found for you.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=self.list_embed(rows),
            view=ReminderManageView(self, interaction.user.id, rows),
            ephemeral=True,
        )

    @tasks.loop(seconds=REMINDER_CHECK_SECONDS)
    async def reminder_scheduler(self) -> None:
        await self.send_due_reminders()

    @reminder_scheduler.before_loop
    async def before_reminder_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def due_reminders(self) -> list[dict[str, Any]]:
        return await self.fetch_all(
            """
            SELECT *
            FROM reminders
            WHERE status = 'pending'
              AND scheduled_at_utc <= ?
            ORDER BY scheduled_at_utc ASC
            LIMIT 25
            """,
            (utc_now_text(),),
        )

    async def mark_sent(self, reminder_id: int) -> None:
        await self.bot.db.execute(
            """
            UPDATE reminders
            SET status = 'sent',
                sent_at_utc = ?,
                updated_at_utc = ?
            WHERE id = ? AND status = 'pending'
            """,
            (utc_now_text(), utc_now_text(), reminder_id),
        )
        await self.bot.db.commit()

    async def mark_failed(self, reminder_id: int, reason: str) -> None:
        await self.bot.db.execute(
            """
            UPDATE reminders
            SET status = 'failed',
                failure_reason = ?,
                updated_at_utc = ?
            WHERE id = ? AND status = 'pending'
            """,
            (truncate(reason, 500), utc_now_text(), reminder_id),
        )
        await self.bot.db.commit()

    async def send_due_reminders(self) -> None:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        if self._send_lock.locked():
            return
        async with self._send_lock:
            for row in await self.due_reminders():
                await self.send_one_reminder(row)

    async def send_one_reminder(self, row: dict[str, Any]) -> None:
        reminder_id = int(row["id"])
        channel = self.bot.get_channel(int(row["channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(row["channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                reason = f"Channel unavailable: {type(exc).__name__}"
                await self.mark_failed(reminder_id, reason)
                logger.warning("Reminder failed: id=%s reason=%s", reminder_id, reason)
                return
        if not is_sendable_channel(channel):
            reason = "Target channel cannot receive messages"
            await self.mark_failed(reminder_id, reason)
            logger.warning("Reminder failed: id=%s reason=%s", reminder_id, reason)
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await self.mark_failed(reminder_id, permission_error)
            logger.warning("Reminder failed: id=%s reason=%s", reminder_id, permission_error)
            return
        try:
            await channel.send(
                content=user_mention(row["target_user_id"]),
                embed=self.reminder_embed(row),
                allowed_mentions=ALLOWED_MENTIONS,
            )
        except discord.HTTPException as exc:
            reason = f"Send failed: {type(exc).__name__}"
            await self.mark_failed(reminder_id, reason)
            logger.warning("Reminder failed: id=%s reason=%s", reminder_id, reason)
            return
        await self.mark_sent(reminder_id)
        logger.info("Reminder sent: id=%s channel_id=%s", reminder_id, row["channel_id"])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReminderCog(bot))
