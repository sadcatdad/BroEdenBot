"""Scheduled member reminders for Bro Eden."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import COLOR
from utils.settings import get_csv_ids_setting, get_setting
from utils.ui import SUCCESS_COLOR, branded_embed, error_embed, truncate


logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_DATE_ONLY_TIME = "9:00 AM"
REMINDER_CHECK_SECONDS = 45
MAX_REMINDER_MESSAGE_LENGTH = 4_000
MAX_REMINDERS_PER_MANAGE = 25
SUBSCRIPTION_BATCH_SIZE = 25
SUBSCRIPTION_MAX_ATTEMPTS = 3
SUBSCRIPTION_RECOVERY_MINUTES = 10
TIMESTAMP_STYLES = ("F", "f", "D", "d", "T", "t", "R", "s", "S")
ALLOWED_MENTIONS = discord.AllowedMentions(users=True, roles=True, everyone=False)
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
DATE_ONLY_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
)
HELPFUL_DATE_ERROR = (
    "I could not understand that date/time. Try `in 2 hours`, "
    "`tomorrow 9am`, `Friday 7:30pm`, or `2026-07-01 7:30 PM`."
)
WEEKDAY_NUMBERS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
RELATIVE_UNITS = {
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
    "d": "days",
    "day": "days",
    "days": "days",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
}


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
    except (ValueError, ZoneInfoNotFoundError):
        logger.warning("Invalid reminder timezone %s; using %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def _reference_time(now: Optional[datetime], tz: ZoneInfo) -> datetime:
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=tz)
    return reference.astimezone(tz)


def _parse_clock(value: str) -> tuple[int, int]:
    text = " ".join(str(value or "").strip().casefold().split())
    if text == "noon":
        return 12, 0
    if text == "midnight":
        return 0, 0
    text = re.sub(r"(?i)(\d)(am|pm)\b", r"\1 \2", text)
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if match is None:
        raise ValueError(HELPFUL_DATE_ERROR)
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if minute > 59:
        raise ValueError(HELPFUL_DATE_ERROR)
    if meridiem:
        if hour < 1 or hour > 12:
            raise ValueError(HELPFUL_DATE_ERROR)
        hour %= 12
        if meridiem == "pm":
            hour += 12
    elif hour > 23:
        raise ValueError(HELPFUL_DATE_ERROR)
    return hour, minute


def _parse_relative_duration(value: str) -> Optional[timedelta]:
    match = re.fullmatch(r"(?:in\s+(.+)|(.+?)\s+from\s+now)", value, re.IGNORECASE)
    if match is None:
        return None
    body = (match.group(1) or match.group(2) or "").strip().casefold()
    token_re = re.compile(
        r"(\d+(?:\.\d+)?)\s*"
        r"(weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
        re.IGNORECASE,
    )
    tokens = list(token_re.finditer(body))
    leftover = token_re.sub(" ", body)
    leftover = re.sub(r"(?:\s|,|\band\b)+", "", leftover, flags=re.IGNORECASE)
    if not tokens or leftover:
        raise ValueError(HELPFUL_DATE_ERROR)
    totals = {"weeks": 0.0, "days": 0.0, "hours": 0.0, "minutes": 0.0}
    for token in tokens:
        amount = float(token.group(1))
        totals[RELATIVE_UNITS[token.group(2).casefold()]] += amount
    duration = timedelta(**totals)
    if duration <= timedelta(0):
        raise ValueError("Reminder time must be greater than zero.")
    return duration


def parse_local_datetime(
    value: str,
    tz: ZoneInfo,
    *,
    now: Optional[datetime] = None,
) -> datetime:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise ValueError(HELPFUL_DATE_ERROR)
    text = (
        text.replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    text = re.sub(r"(?i)(\d)(am|pm)\b", r"\1 \2", text)

    reference = _reference_time(now, tz)
    relative = _parse_relative_duration(text)
    if relative is not None:
        return (reference + relative).astimezone(timezone.utc)

    conversational = re.fullmatch(
        r"(today|tomorrow)(?:\s+(?:at\s+)?(.+))?",
        text,
        re.IGNORECASE,
    )
    if conversational is not None:
        day_offset = 1 if conversational.group(1).casefold() == "tomorrow" else 0
        hour, minute = _parse_clock(conversational.group(2) or DEFAULT_DATE_ONLY_TIME)
        target_date = reference.date() + timedelta(days=day_offset)
        local_value = datetime.combine(
            target_date,
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=tz,
        )
        return local_value.astimezone(timezone.utc)

    weekday = re.fullmatch(
        r"(?:(next)\s+)?"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"(?:\s+(?:at\s+)?(.+))?",
        text,
        re.IGNORECASE,
    )
    if weekday is not None:
        desired_weekday = WEEKDAY_NUMBERS[weekday.group(2).casefold()]
        hour, minute = _parse_clock(weekday.group(3) or DEFAULT_DATE_ONLY_TIME)
        days_ahead = (desired_weekday - reference.weekday()) % 7
        candidate_date = reference.date() + timedelta(days=days_ahead)
        local_value = datetime.combine(
            candidate_date,
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=tz,
        )
        if weekday.group(1) or local_value <= reference:
            local_value += timedelta(days=7)
        return local_value.astimezone(timezone.utc)

    for date_format in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        local_value = parsed.replace(tzinfo=tz)
        return local_value.astimezone(timezone.utc)
    for date_format in DATE_ONLY_FORMATS:
        try:
            parsed_date = datetime.strptime(text, date_format).date()
            parsed_time = datetime.strptime(DEFAULT_DATE_ONLY_TIME, "%I:%M %p").time()
        except ValueError:
            continue
        local_value = datetime.combine(parsed_date, parsed_time, tzinfo=tz)
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


def timestamp_codes_embed(value: datetime, timezone_name: str) -> discord.Embed:
    unix_timestamp = int(value.astimezone(timezone.utc).timestamp())
    lines = []
    for style in TIMESTAMP_STYLES:
        code = f"<t:{unix_timestamp}:{style}>"
        lines.append(f"`{code}`  {code}")
    return branded_embed(
        "TIME CODES",
        description=(
            f"Time was parsed using `{timezone_name}`.\n\n"
            "Copy and paste a code below to show the time in each viewer's "
            "local timezone.\n\n"
            + "\n".join(lines)
        ),
    )


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


def reminder_target_text(target_user_id: Any) -> str:
    value = str(target_user_id or "").strip()
    return user_mention(value) if value.isdigit() and int(value) > 0 else "Nobody"


def reminder_ping_content(row: dict[str, Any]) -> Optional[str]:
    """Return only explicit pings; the formatted message remains in the embed."""
    mentions: list[str] = []
    seen: set[tuple[str, str]] = set()
    target_id = str(row.get("target_user_id") or "").strip()
    if target_id.isdigit() and int(target_id) > 0:
        seen.add(("user", target_id))
        mentions.append(user_mention(target_id))
    message = str(row.get("message") or "")
    for match in re.finditer(r"<@(!?)(\d+)>|<@&(\d+)>", message):
        user_id = match.group(2)
        role_id = match.group(3)
        kind, snowflake = ("role", role_id) if role_id else ("user", user_id)
        if not snowflake or (kind, snowflake) in seen:
            continue
        seen.add((kind, snowflake))
        mentions.append(f"<@&{snowflake}>" if kind == "role" else user_mention(snowflake))
    return " ".join(mentions) or None


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


class ReminderCreateModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "ReminderCog",
        channel: Any,
        target: Optional[discord.Member],
    ) -> None:
        super().__init__(title="Create reminder", timeout=300)
        self.cog = cog
        self.channel = channel
        self.target = target
        self.message = discord.ui.TextInput(
            label="Message",
            placeholder="# Reminder heading\n- First item\n- Second item",
            style=discord.TextStyle.paragraph,
            max_length=MAX_REMINDER_MESSAGE_LENGTH,
        )
        self.date_time = discord.ui.TextInput(
            label="When",
            placeholder="in 2 hours or tomorrow at 9am",
            max_length=100,
        )
        self.add_item(self.message)
        self.add_item(self.date_time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_from_modal(
            interaction,
            channel=self.channel,
            target=self.target,
            message=str(self.message.value),
            date_time=str(self.date_time.value),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self.cog.handle_component_error(interaction, "reminder create modal", error)


class RemindSubscribeModal(discord.ui.Modal):
    def __init__(self, cog: "ReminderCog", channel: Any) -> None:
        super().__init__(title="Create subscribable reminder", timeout=300)
        self.cog = cog
        self.channel = channel
        self.message = discord.ui.TextInput(
            label="Message",
            placeholder="# Event reminder\n- First detail\n- Second detail",
            style=discord.TextStyle.paragraph,
            max_length=MAX_REMINDER_MESSAGE_LENGTH,
        )
        self.date_time = discord.ui.TextInput(
            label="When",
            placeholder="today at 11am or in 1 hour",
            max_length=100,
        )
        self.destination = discord.ui.ChannelSelect(
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
                discord.ChannelType.stage_voice,
            ],
            placeholder="Choose a text, voice, or Stage channel",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.add_item(self.message)
        self.add_item(self.date_time)
        self.add_item(discord.ui.Label(
            text="WHERE:",
            description="Members will be taken here from the reminder DM.",
            component=self.destination,
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_subscription_post(
            interaction,
            channel=self.channel,
            destination=self.destination.values[0],
            message=str(self.message.value),
            date_time=str(self.date_time.value),
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        await self.cog.handle_component_error(
            interaction,
            "subscribable reminder create modal",
            error,
        )


class ReminderEditModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "ReminderCog",
        reminder_id: int,
        row: dict[str, Any],
        user_timezone: ZoneInfo,
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
            default=local_datetime_input(row["scheduled_at_utc"], user_timezone),
            placeholder="in 2 hours or tomorrow at 9am",
            max_length=100,
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
            target_label = (
                f"member {row['target_user_id']}"
                if str(row.get("target_user_id") or "").isdigit()
                else "no automatic ping"
            )
            options.append(
                discord.SelectOption(
                    label=f"#{row['id']} • {target_label}",
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
        user_timezone = await self.cog.user_timezone(
            interaction.guild_id,
            interaction.user.id,
        )
        await interaction.response.send_modal(
            ReminderEditModal(self.cog, reminder_id, row, user_timezone)
        )

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
        await self.cog.defer_private(interaction, thinking=True)
        reminder_id = self.selected_reminder_id
        if reminder_id is None:
            await self.cog.send_private(
                interaction,
                "Choose a reminder first.",
            )
            return
        deleted = await self.cog.soft_delete_reminder(
            interaction.guild_id,
            interaction.user.id,
            reminder_id,
        )
        if not deleted:
            await self.cog.send_private(
                interaction,
                "That pending reminder was not found.",
            )
            return
        logger.info("Reminder deleted: id=%s user_id=%s", reminder_id, interaction.user.id)
        await self.cog.send_private(
            interaction,
            f"Reminder #{reminder_id} was deleted.",
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
    remind = app_commands.Group(
        name="remind",
        description="Create reminders members can subscribe to",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._send_lock: Optional[asyncio.Lock] = None
        self._subscription_lock: Optional[asyncio.Lock] = None

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
            CREATE TABLE IF NOT EXISTS reminder_subscription_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT,
                destination_channel_id TEXT,
                destination_channel_name TEXT,
                creator_user_id TEXT NOT NULL,
                message TEXT NOT NULL,
                scheduled_at_utc TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'completed', 'failed')),
                failure_reason TEXT,
                created_at_utc TEXT NOT NULL,
                completed_at_utc TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_subscription_message
                ON reminder_subscription_posts (message_id)
                WHERE message_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_reminder_subscription_due
                ON reminder_subscription_posts (status, scheduled_at_utc);
            CREATE TABLE IF NOT EXISTS reminder_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'subscribed'
                    CHECK (status IN (
                        'subscribed', 'processing', 'sent', 'cancelled', 'failed'
                    )),
                subscribed_at_utc TEXT NOT NULL,
                cancelled_at_utc TEXT,
                processing_at_utc TEXT,
                sent_at_utc TEXT,
                dm_confirmation_message_id TEXT,
                dm_reminder_message_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                FOREIGN KEY (post_id) REFERENCES reminder_subscription_posts(id),
                UNIQUE (post_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_subscribers_status
                ON reminder_subscribers (status, post_id);
            CREATE TABLE IF NOT EXISTS user_timezones (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                timezone_name TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        cursor = await self.bot.db.execute("PRAGMA table_info(reminder_subscription_posts)")
        subscription_columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "destination_channel_id" not in subscription_columns:
            await self.bot.db.execute(
                "ALTER TABLE reminder_subscription_posts "
                "ADD COLUMN destination_channel_id TEXT"
            )
        if "destination_channel_name" not in subscription_columns:
            await self.bot.db.execute(
                "ALTER TABLE reminder_subscription_posts "
                "ADD COLUMN destination_channel_name TEXT"
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

    def member_has_staff_access(self, guild: Any, member: Any) -> bool:
        if guild is None or not isinstance(member, discord.Member):
            return False
        if member.id in get_csv_ids_setting("BOT_OWNER_USER_IDS"):
            return True
        if member.guild_permissions.administrator:
            return True
        allowed_ids = set(get_csv_ids_setting("REMINDER_ALLOWED_ROLE_IDS"))
        allowed_ids.update(parse_id_set(os.getenv("REMINDER_ALLOWED_ROLE_IDS", "")))
        allowed_ids.update(get_csv_ids_setting("STAFF_NOTES_ALLOWED_ROLE_IDS"))
        allowed_ids.update(get_csv_ids_setting("STAFF_AI_ALLOWED_ROLE_IDS"))
        allowed_ids.update(parse_id_set(get_setting("staff_role_ids", "")))
        allowed_ids.update(parse_id_set(get_setting("admin_role_ids", "")))
        return any(role.id in allowed_ids for role in member.roles)

    async def user_timezone_name(self, guild_id: Any, user_id: Any) -> str:
        if guild_id is not None and user_id is not None:
            row = await self.fetch_one(
                """
                SELECT timezone_name
                FROM user_timezones
                WHERE guild_id = ? AND user_id = ?
                """,
                (str(guild_id), str(user_id)),
            )
            if row is not None:
                name = str(row["timezone_name"] or "").strip()
                try:
                    ZoneInfo(name)
                except (ValueError, ZoneInfoNotFoundError):
                    logger.warning(
                        "Stored user timezone is invalid guild_id=%s user_id=%s timezone=%r",
                        guild_id,
                        user_id,
                        name,
                    )
                else:
                    return name
        return configured_timezone_name()

    async def user_timezone(self, guild_id: Any, user_id: Any) -> ZoneInfo:
        name = await self.user_timezone_name(guild_id, user_id)
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            return reminder_timezone()

    async def save_user_timezone(
        self,
        guild_id: int,
        user_id: int,
        timezone_name: str,
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO user_timezones (
                guild_id, user_id, timezone_name, updated_at_utc
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                timezone_name = excluded.timezone_name,
                updated_at_utc = excluded.updated_at_utc
            """,
            (str(guild_id), str(user_id), timezone_name, utc_now_text()),
        )
        await self.bot.db.commit()

    async def ensure_staff_access(self, interaction: discord.Interaction) -> bool:
        if self.has_staff_access(interaction):
            return True
        message = "Reminders are limited to internal staff."
        await self.send_private(interaction, message)
        return False

    async def defer_private(
        self,
        interaction: discord.Interaction,
        *,
        thinking: bool = False,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=thinking)

    async def send_private(
        self,
        interaction: discord.Interaction,
        content: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(content, ephemeral=True, **kwargs)

    def can_target_user(
        self,
        interaction: discord.Interaction,
        target: discord.abc.User,
    ) -> bool:
        return self.has_staff_access(interaction)

    def reminder_embed(self, row: dict[str, Any]) -> discord.Embed:
        return discord.Embed(
            description=truncate(row["message"], 4096),
            color=discord.Color(COLOR),
        )

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
        embed.add_field(
            name="Ping:",
            value=reminder_target_text(row["target_user_id"]),
            inline=True,
        )
        embed.add_field(
            name="Channel:",
            value=channel_reference(channel, row["channel_id"]),
            inline=True,
        )
        embed.add_field(
            name="Scheduled For:",
            value=discord_timestamp(row["scheduled_at_utc"]),
            inline=False,
        )
        return embed

    def detail_embed(self, row: dict[str, Any]) -> discord.Embed:
        embed = branded_embed(
            f"Reminder #{row['id']}",
            description=truncate(row["message"], 4096),
            footer="Private reminder management",
        )
        embed.add_field(
            name="Automatic ping",
            value=reminder_target_text(row["target_user_id"]),
            inline=True,
        )
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
                f"**#{row['id']}** • {reminder_target_text(row['target_user_id'])} • "
                f"{channel_mention(row['channel_id'])} • "
                f"{discord_timestamp(row['scheduled_at_utc'])}\n"
                f"{truncate(row['message'], 140)}"
            )
        return branded_embed(
            "Your Pending Reminders",
            description="\n\n".join(lines),
            footer="Select one to edit or delete",
        )

    @staticmethod
    def subscription_view(post_id: int, *, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            emoji="🔔",
            style=discord.ButtonStyle.secondary,
            custom_id=f"remindsubscribe|join|{post_id}",
            disabled=disabled,
        ))
        return view

    @staticmethod
    def subscription_cancel_view(
        subscriber_id: int,
        *,
        disabled: bool = False,
    ) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Cancel Reminder",
            emoji="🔕",
            style=discord.ButtonStyle.secondary,
            custom_id=f"remindsubscribe|cancel|{subscriber_id}",
            disabled=disabled,
        ))
        return view

    @staticmethod
    def subscription_jump_url(row: dict[str, Any]) -> str:
        return (
            "https://discord.com/channels/"
            f"{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
        )

    @staticmethod
    def subscription_destination_url(row: dict[str, Any]) -> Optional[str]:
        destination_id = str(row.get("destination_channel_id") or "").strip()
        if not destination_id.isdigit():
            return None
        return f"https://discord.com/channels/{row['guild_id']}/{destination_id}"

    def subscription_destination_link(self, row: dict[str, Any]) -> str:
        destination_url = self.subscription_destination_url(row)
        if destination_url is None:
            return f"[Back to server reminder]({self.subscription_jump_url(row)})"
        destination_name = discord.utils.escape_markdown(
            str(row.get("destination_channel_name") or "destination channel")
        )
        return f"[Open #{destination_name}]({destination_url})"

    def subscription_post_embed(self, row: dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            description=truncate(row["message"], 4096),
            color=discord.Color(COLOR),
        )
        embed.add_field(
            name="WHEN:",
            value=(
                f"{discord_timestamp(row['scheduled_at_utc'], 'F')} "
                f"• {discord_timestamp(row['scheduled_at_utc'], 'R')}"
            ),
            inline=False,
        )
        destination_id = str(row.get("destination_channel_id") or "").strip()
        if destination_id.isdigit():
            embed.add_field(
                name="WHERE:",
                value=channel_mention(destination_id),
                inline=False,
            )
        embed.set_footer(text="🔔 Subscribe to DM Reminder")
        return embed

    def subscription_confirmation_embed(self, row: dict[str, Any]) -> discord.Embed:
        description = (
            f"You will be reminded {discord_timestamp(row['scheduled_at_utc'], 'F')} "
            f"({discord_timestamp(row['scheduled_at_utc'], 'R')}) about:\n\n"
            f"{truncate(row['message'], 3400)}\n\n"
            f"**WHERE:** {self.subscription_destination_link(row)}"
        )
        return discord.Embed(
            title="🔔 Reminder Confirmation",
            description=description,
            color=discord.Color(COLOR),
        )

    def subscription_delivery_embed(self, row: dict[str, Any]) -> discord.Embed:
        description = (
            f"{truncate(row['message'], 3700)}\n\n"
            f"**WHERE:** {self.subscription_destination_link(row)}"
        )
        return discord.Embed(
            title="🔔 Reminder",
            description=description,
            color=discord.Color(COLOR),
        )

    async def insert_reminder(
        self,
        *,
        guild_id: int,
        creator_user_id: int,
        target_user_id: Optional[int],
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
                str(target_user_id) if target_user_id is not None else "",
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
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_staff_access(interaction):
            return
        if getattr(channel, "guild", None) and channel.guild.id != interaction.guild_id:
            await self.send_private(
                interaction,
                "Choose a channel in this server.",
            )
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await self.send_private(interaction, permission_error)
            return
        row = await self.update_pending_reminder(
            reminder_id,
            interaction.guild_id,
            interaction.user.id,
            channel_id=str(channel.id),
        )
        if row is None:
            await self.send_private(
                interaction,
                "That pending reminder was not found.",
            )
            return
        await self.send_private(
            interaction,
            embed=self.detail_embed(row),
        )

    async def update_reminder_target(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
        target: discord.abc.User,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_staff_access(interaction):
            return
        if not self.can_target_user(interaction, target):
            await self.send_private(
                interaction,
                "Reminders are limited to internal staff.",
            )
            return
        row = await self.update_pending_reminder(
            reminder_id,
            interaction.guild_id,
            interaction.user.id,
            target_user_id=str(target.id),
        )
        if row is None:
            await self.send_private(
                interaction,
                "That pending reminder was not found.",
            )
            return
        await self.send_private(
            interaction,
            embed=self.detail_embed(row),
        )

    async def apply_modal_edit(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
        *,
        message: str,
        date_time: str,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_staff_access(interaction):
            return
        try:
            user_tz = await self.user_timezone(interaction.guild_id, interaction.user.id)
            scheduled_at = parse_local_datetime(date_time, user_tz)
        except ValueError as exc:
            await self.send_private(interaction, str(exc))
            return
        if scheduled_at <= utc_now():
            await self.send_private(
                interaction,
                "Reminder date/time must be in the future.",
            )
            return
        cleaned_message = message.strip()
        if not cleaned_message:
            await self.send_private(
                interaction,
                "Reminder message cannot be blank.",
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
            await self.send_private(
                interaction,
                "That pending reminder was not found.",
            )
            return
        await self.send_private(interaction, embed=self.detail_embed(row))

    async def create_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        channel: Any,
        target: Optional[discord.Member],
        message: str,
        date_time: str,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_staff_access(interaction):
            return
        cleaned_message = message.strip()
        if not cleaned_message:
            await self.send_private(interaction, "Reminder message cannot be blank.")
            return
        try:
            user_tz = await self.user_timezone(interaction.guild_id, interaction.user.id)
            scheduled_at = parse_local_datetime(date_time, user_tz)
        except ValueError as exc:
            await self.send_private(interaction, str(exc))
            return
        if scheduled_at <= utc_now():
            await self.send_private(
                interaction,
                "Reminder date/time must be in the future.",
            )
            return
        try:
            row = await self.insert_reminder(
                guild_id=int(interaction.guild_id or 0),
                creator_user_id=interaction.user.id,
                target_user_id=target.id if target is not None else None,
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
            await self.send_private(
                interaction,
                embed=error_embed(
                    "Reminder Not Saved",
                    "The reminder could not be saved. Please try again later.",
                ),
            )
            return
        logger.info(
            "Reminder created: id=%s guild_id=%s creator=%s target=%s channel=%s scheduled_at=%s",
            row["id"],
            interaction.guild_id,
            interaction.user.id,
            target.id if target is not None else "none",
            channel.id,
            row["scheduled_at_utc"],
        )
        await self.send_private(
            interaction,
            embed=self.confirmation_embed(row, channel),
        )

    async def create_subscription_post(
        self,
        interaction: discord.Interaction,
        *,
        channel: Any,
        destination: Any,
        message: str,
        date_time: str,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_staff_access(interaction):
            return
        cleaned_message = message.strip()
        if not cleaned_message:
            await self.send_private(interaction, "Reminder message cannot be blank.")
            return
        try:
            user_tz = await self.user_timezone(interaction.guild_id, interaction.user.id)
            scheduled_at = parse_local_datetime(date_time, user_tz)
        except ValueError as exc:
            await self.send_private(interaction, str(exc))
            return
        if scheduled_at <= utc_now():
            await self.send_private(
                interaction,
                "Reminder date/time must be in the future.",
            )
            return
        destination_id = getattr(destination, "id", None)
        destination_guild_id = getattr(destination, "guild_id", None)
        if destination_guild_id is None:
            destination_guild_id = getattr(getattr(destination, "guild", None), "id", None)
        if (
            destination_id is None
            or destination_guild_id is None
            or str(destination_guild_id) != str(interaction.guild_id)
        ):
            await self.send_private(
                interaction,
                "Choose a destination channel, voice channel, or Stage in this server.",
            )
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await self.send_private(interaction, permission_error)
            return
        now = utc_now_text()
        cursor = await self.bot.db.execute(
            """
            INSERT INTO reminder_subscription_posts (
                guild_id, channel_id, destination_channel_id,
                destination_channel_name, creator_user_id, message,
                scheduled_at_utc, status, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                str(interaction.guild_id),
                str(channel.id),
                str(destination_id),
                str(getattr(destination, "name", "destination channel")),
                str(interaction.user.id),
                cleaned_message,
                scheduled_at.isoformat(),
                now,
            ),
        )
        post_id = int(cursor.lastrowid)
        await cursor.close()
        await self.bot.db.commit()
        row = await self.fetch_one(
            "SELECT * FROM reminder_subscription_posts WHERE id = ?",
            (post_id,),
        )
        try:
            public_message = await channel.send(
                embed=self.subscription_post_embed(row),
                view=self.subscription_view(post_id),
            )
        except discord.HTTPException as exc:
            await self.bot.db.execute(
                """
                UPDATE reminder_subscription_posts
                SET status = 'failed', failure_reason = ?
                WHERE id = ?
                """,
                (f"Publish failed: {type(exc).__name__}", post_id),
            )
            await self.bot.db.commit()
            logger.warning(
                "Subscribable reminder publish failed post_id=%s error=%s",
                post_id,
                type(exc).__name__,
            )
            await self.send_private(
                interaction,
                "The subscribable reminder could not be posted. Please try again.",
            )
            return
        await self.bot.db.execute(
            "UPDATE reminder_subscription_posts SET message_id = ? WHERE id = ?",
            (str(public_message.id), post_id),
        )
        await self.bot.db.commit()
        logger.info(
            "Subscribable reminder created post_id=%s guild_id=%s channel_id=%s creator=%s",
            post_id,
            interaction.guild_id,
            channel.id,
            interaction.user.id,
        )
        await self.send_private(
            interaction,
            f"🔔 Subscribable reminder posted for "
            f"{discord_timestamp(scheduled_at.isoformat(), 'F')}.",
        )

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
        channel="Channel where the reminder should be posted",
        who="Optional member to ping automatically. Defaults to nobody.",
    )
    @app_commands.guild_only()
    async def add(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        who: Optional[discord.Member] = None,
    ) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        if who is not None and not self.can_target_user(interaction, who):
            await self.send_private(
                interaction,
                "Reminders are limited to internal staff.",
            )
            return
        if getattr(channel, "guild", None) and channel.guild.id != interaction.guild_id:
            await self.send_private(
                interaction,
                "Choose a channel in this server.",
            )
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await self.send_private(interaction, permission_error)
            return
        await interaction.response.send_modal(ReminderCreateModal(self, channel, who))

    @remind.command(
        name="subscribe",
        description="Post a reminder that members can subscribe to by DM",
    )
    @app_commands.guild_only()
    async def subscribe(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        channel = interaction.channel
        if channel is None or not is_sendable_channel(channel):
            await self.send_private(
                interaction,
                "Use this command in a server channel where the bot can post.",
            )
            return
        permission_error = send_permission_error(self.bot, channel)
        if permission_error:
            await self.send_private(interaction, permission_error)
            return
        await interaction.response.send_modal(RemindSubscribeModal(self, channel))

    @app_commands.command(
        name="timezone",
        description="View or set your personal timezone for staff time tools",
    )
    @app_commands.describe(
        timezone="Optional IANA timezone, such as America/New_York or Europe/London",
    )
    @app_commands.guild_only()
    async def timezone_command(
        self,
        interaction: discord.Interaction,
        timezone: Optional[str] = None,
    ) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        if timezone is None:
            saved = await self.fetch_one(
                "SELECT timezone_name FROM user_timezones WHERE guild_id = ? AND user_id = ?",
                (str(interaction.guild_id), str(interaction.user.id)),
            )
            user_tz = await self.user_timezone(
                interaction.guild_id,
                interaction.user.id,
            )
            timezone_name = user_tz.key
            source = "your saved preference" if saved is not None else "the server fallback"
            await self.send_private(
                interaction,
                f"Your time phrases currently use `{timezone_name}` from {source}. "
                "Run `/timezone` again with the timezone field to change it.",
            )
            return
        timezone_name = timezone.strip()
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError):
            await self.send_private(
                interaction,
                "I could not find that timezone. Start typing a city and select a "
                "suggestion, such as `America/New_York` or `Europe/London`.",
            )
            return
        await self.save_user_timezone(
            int(interaction.guild_id or 0),
            interaction.user.id,
            timezone_name,
        )
        await self.send_private(
            interaction,
            f"✅ Your timezone is now `{timezone_name}`. Future `/time`, `!time`, "
            "and reminder phrases will use it.",
        )

    @timezone_command.autocomplete("timezone")
    async def timezone_autocomplete(
        self,
        _interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        query = current.strip().replace(" ", "_").casefold()
        matches = [
            name
            for name in sorted(available_timezones())
            if not query or query in name.casefold()
        ]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    @app_commands.command(
        name="time",
        description="Create copyable Discord time codes from a natural-language time",
    )
    @app_commands.describe(when="Time such as tomorrow at 9am or Friday at 7:30pm")
    @app_commands.guild_only()
    async def time_command(
        self,
        interaction: discord.Interaction,
        when: str,
    ) -> None:
        if not await self.ensure_staff_access(interaction):
            return
        user_tz = await self.user_timezone(
            interaction.guild_id,
            interaction.user.id,
        )
        timezone_name = user_tz.key
        try:
            parsed = parse_local_datetime(when, user_tz)
        except ValueError as exc:
            await self.send_private(interaction, str(exc))
            return
        await self.send_private(
            interaction,
            embed=timestamp_codes_embed(parsed, timezone_name),
        )

    @commands.command(name="time", description="Create public Discord time codes")
    async def time_prefix(
        self,
        ctx: commands.Context,
        *,
        when: str = "",
    ) -> None:
        if not self.member_has_staff_access(ctx.guild, ctx.author):
            await ctx.send("Time tools are limited to configured staff.")
            return
        if not when.strip():
            await ctx.send("Try `!time tomorrow at 9am`.")
            return
        user_tz = await self.user_timezone(ctx.guild.id, ctx.author.id)
        timezone_name = user_tz.key
        try:
            parsed = parse_local_datetime(when, user_tz)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(embed=timestamp_codes_embed(parsed, timezone_name))

    @reminder.command(name="manage", description="Privately manage your pending reminders")
    @app_commands.guild_only()
    async def manage(self, interaction: discord.Interaction) -> None:
        await self.defer_private(interaction, thinking=True)
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
            await self.send_private(
                interaction,
                "No pending reminders found for you.",
            )
            return
        await self.send_private(
            interaction,
            embed=self.list_embed(rows),
            view=ReminderManageView(self, interaction.user.id, rows),
        )

    async def handle_subscription_join(
        self,
        interaction: discord.Interaction,
        post_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.fetch_one(
            "SELECT * FROM reminder_subscription_posts WHERE id = ?",
            (post_id,),
        )
        if (
            row is None
            or row["status"] != "open"
            or parse_utc_text(row["scheduled_at_utc"]) <= utc_now()
        ):
            await interaction.followup.send(
                "This reminder is no longer accepting subscriptions.",
                ephemeral=True,
            )
            return
        if interaction.guild_id is None or str(interaction.guild_id) != str(row["guild_id"]):
            await interaction.followup.send(
                "This reminder does not belong to this server.",
                ephemeral=True,
            )
            return
        existing = await self.fetch_one(
            """
            SELECT * FROM reminder_subscribers
            WHERE post_id = ? AND user_id = ?
            """,
            (post_id, str(interaction.user.id)),
        )
        if existing is not None and existing["status"] == "subscribed":
            await interaction.followup.send(
                "🔔 You are already subscribed to this reminder.",
                ephemeral=True,
            )
            return
        now = utc_now_text()
        await self.bot.db.execute(
            """
            INSERT INTO reminder_subscribers (
                post_id, user_id, status, subscribed_at_utc,
                cancelled_at_utc, processing_at_utc, sent_at_utc,
                dm_confirmation_message_id, dm_reminder_message_id,
                attempt_count, failure_reason
            ) VALUES (?, ?, 'subscribed', ?, NULL, NULL, NULL, NULL, NULL, 0, NULL)
            ON CONFLICT (post_id, user_id) DO UPDATE SET
                status = 'subscribed',
                subscribed_at_utc = excluded.subscribed_at_utc,
                cancelled_at_utc = NULL,
                processing_at_utc = NULL,
                sent_at_utc = NULL,
                dm_confirmation_message_id = NULL,
                dm_reminder_message_id = NULL,
                attempt_count = 0,
                failure_reason = NULL
            """,
            (post_id, str(interaction.user.id), now),
        )
        await self.bot.db.commit()
        subscriber = await self.fetch_one(
            """
            SELECT * FROM reminder_subscribers
            WHERE post_id = ? AND user_id = ?
            """,
            (post_id, str(interaction.user.id)),
        )
        try:
            dm_message = await interaction.user.send(
                embed=self.subscription_confirmation_embed(row),
                view=self.subscription_cancel_view(int(subscriber["id"])),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await self.bot.db.execute(
                """
                UPDATE reminder_subscribers
                SET status = 'failed', failure_reason = ?
                WHERE id = ?
                """,
                (f"Confirmation DM failed: {type(exc).__name__}", subscriber["id"]),
            )
            await self.bot.db.commit()
            await interaction.followup.send(
                "I could not DM you. Enable direct messages for this server and try again.",
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            """
            UPDATE reminder_subscribers
            SET dm_confirmation_message_id = ?
            WHERE id = ?
            """,
            (str(dm_message.id), subscriber["id"]),
        )
        await self.bot.db.commit()
        await interaction.followup.send(
            "✅ Check your DMs for the reminder confirmation.",
            ephemeral=True,
        )

    async def handle_subscription_cancel(
        self,
        interaction: discord.Interaction,
        subscriber_id: int,
    ) -> None:
        row = await self.fetch_one(
            """
            SELECT s.*, p.message, p.scheduled_at_utc
            FROM reminder_subscribers AS s
            JOIN reminder_subscription_posts AS p ON p.id = s.post_id
            WHERE s.id = ?
            """,
            (subscriber_id,),
        )
        if row is None or str(row["user_id"]) != str(interaction.user.id):
            await interaction.response.send_message(
                "This reminder subscription is not yours.",
                ephemeral=True,
            )
            return
        cursor = await self.bot.db.execute(
            """
            UPDATE reminder_subscribers
            SET status = 'cancelled', cancelled_at_utc = ?, processing_at_utc = NULL
            WHERE id = ? AND status = 'subscribed'
            """,
            (utc_now_text(), subscriber_id),
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        if not changed:
            await interaction.response.send_message(
                "This reminder was already cancelled or delivered.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🔕 Reminder Cancelled",
            description=(
                "You will not receive this reminder:\n\n"
                f"{truncate(row['message'], 3800)}"
            ),
            color=discord.Color(COLOR),
        )
        await interaction.response.edit_message(
            embed=embed,
            view=self.subscription_cancel_view(subscriber_id, disabled=True),
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        parts = custom_id.split("|")
        if len(parts) != 3 or parts[0] != "remindsubscribe" or not parts[2].isdigit():
            return
        if parts[1] == "join":
            await self.handle_subscription_join(interaction, int(parts[2]))
        elif parts[1] == "cancel":
            await self.handle_subscription_cancel(interaction, int(parts[2]))

    @tasks.loop(seconds=REMINDER_CHECK_SECONDS)
    async def reminder_scheduler(self) -> None:
        await self.send_due_reminders()
        await self.send_due_subscription_reminders()

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

    async def send_due_subscription_reminders(self) -> None:
        if self._subscription_lock is None:
            self._subscription_lock = asyncio.Lock()
        if self._subscription_lock.locked():
            return
        async with self._subscription_lock:
            now = utc_now()
            recovery_cutoff = now - timedelta(minutes=SUBSCRIPTION_RECOVERY_MINUTES)
            await self.bot.db.execute(
                """
                UPDATE reminder_subscribers
                SET status = 'subscribed', processing_at_utc = NULL
                WHERE status = 'processing'
                  AND processing_at_utc <= ?
                """,
                (recovery_cutoff.isoformat(),),
            )
            await self.bot.db.execute(
                """
                UPDATE reminder_subscription_posts
                SET status = 'completed', completed_at_utc = ?
                WHERE status = 'open' AND scheduled_at_utc <= ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            await self.bot.db.commit()
            rows = await self.fetch_all(
                """
                SELECT
                    s.id AS subscriber_id,
                    s.user_id,
                    s.attempt_count,
                    p.id AS post_id,
                    p.guild_id,
                    p.channel_id,
                    p.message_id,
                    p.destination_channel_id,
                    p.destination_channel_name,
                    p.message,
                    p.scheduled_at_utc
                FROM reminder_subscribers AS s
                JOIN reminder_subscription_posts AS p ON p.id = s.post_id
                WHERE s.status = 'subscribed'
                  AND p.scheduled_at_utc <= ?
                ORDER BY p.scheduled_at_utc ASC, s.id ASC
                LIMIT ?
                """,
                (now.isoformat(), SUBSCRIPTION_BATCH_SIZE),
            )
            for row in rows:
                await self.send_one_subscription_reminder(row)

    async def send_one_subscription_reminder(self, row: dict[str, Any]) -> None:
        subscriber_id = int(row["subscriber_id"])
        cursor = await self.bot.db.execute(
            """
            UPDATE reminder_subscribers
            SET status = 'processing', processing_at_utc = ?, attempt_count = attempt_count + 1
            WHERE id = ? AND status = 'subscribed'
            """,
            (utc_now_text(), subscriber_id),
        )
        claimed = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        if not claimed:
            return
        user = self.bot.get_user(int(row["user_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(row["user_id"]))
            except (discord.NotFound, discord.Forbidden) as exc:
                await self.bot.db.execute(
                    """
                    UPDATE reminder_subscribers
                    SET status = 'failed', processing_at_utc = NULL, failure_reason = ?
                    WHERE id = ?
                    """,
                    (f"User unavailable: {type(exc).__name__}", subscriber_id),
                )
                await self.bot.db.commit()
                return
            except discord.HTTPException as exc:
                await self._retry_or_fail_subscription(
                    subscriber_id,
                    int(row["attempt_count"]) + 1,
                    f"User fetch failed: {type(exc).__name__}",
                )
                return
        try:
            dm_message = await user.send(embed=self.subscription_delivery_embed(row))
        except discord.Forbidden as exc:
            await self.bot.db.execute(
                """
                UPDATE reminder_subscribers
                SET status = 'failed', processing_at_utc = NULL, failure_reason = ?
                WHERE id = ?
                """,
                (f"Reminder DM blocked: {type(exc).__name__}", subscriber_id),
            )
            await self.bot.db.commit()
            return
        except discord.HTTPException as exc:
            await self._retry_or_fail_subscription(
                subscriber_id,
                int(row["attempt_count"]) + 1,
                f"Reminder DM failed: {type(exc).__name__}",
            )
            return
        await self.bot.db.execute(
            """
            UPDATE reminder_subscribers
            SET status = 'sent', sent_at_utc = ?, processing_at_utc = NULL,
                dm_reminder_message_id = ?, failure_reason = NULL
            WHERE id = ? AND status = 'processing'
            """,
            (utc_now_text(), str(dm_message.id), subscriber_id),
        )
        await self.bot.db.commit()
        logger.info(
            "Subscription reminder sent subscriber_id=%s post_id=%s user_id=%s",
            subscriber_id,
            row["post_id"],
            row["user_id"],
        )

    async def _retry_or_fail_subscription(
        self,
        subscriber_id: int,
        attempt_count: int,
        reason: str,
    ) -> None:
        status = "failed" if attempt_count >= SUBSCRIPTION_MAX_ATTEMPTS else "subscribed"
        await self.bot.db.execute(
            """
            UPDATE reminder_subscribers
            SET status = ?, processing_at_utc = NULL, failure_reason = ?
            WHERE id = ?
            """,
            (status, truncate(reason, 500), subscriber_id),
        )
        await self.bot.db.commit()
        logger.warning(
            "Subscription reminder delivery issue subscriber_id=%s attempt=%s status=%s reason=%s",
            subscriber_id,
            attempt_count,
            status,
            reason,
        )

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
                content=reminder_ping_content(row),
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
