"""Unified personal and subscribable event reminders for Bro Eden."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import COLOR
from utils.reminder_service import (
    DEFAULT_EVENT_OFFSETS,
    ReminderService,
    env_bool,
    normalize_title,
    parse_offsets,
    parse_utc,
    recurrence_dates,
    sanitize_text,
    timing_label,
    timing_summary,
    utc_now,
    utc_text,
)
from utils.settings import get_csv_ids_setting, get_setting
from utils.ui import SUCCESS_COLOR, branded_embed, error_embed, truncate


logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_DATE_ONLY_TIME = "9:00 AM"
REMINDER_CHECK_SECONDS = 30
MAX_REMINDER_MESSAGE_LENGTH = 4_000
MAX_REMINDERS_PER_MANAGE = 25
TIMESTAMP_STYLES = ("F", "f", "D", "d", "T", "t", "R", "s", "S")
SAFE_MENTIONS = discord.AllowedMentions.none()
LEGACY_ENABLED = env_bool("ENABLE_LEGACY_REMINDER_COMMANDS", True)

DATETIME_FORMATS = (
    "%Y-%m-%d %I:%M %p",
    "%Y-%m-%d %I %p",
    "%Y-%m-%d %H:%M",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %I %p",
    "%m/%d/%Y %H:%M",
)
DATE_ONLY_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")
HELPFUL_DATE_ERROR = (
    "I could not understand that date/time. Try `in 2 hours`, "
    "`tomorrow 9am`, `Friday 7:30pm`, or `2026-07-01 7:30 PM`."
)
WEEKDAY_NUMBERS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
RELATIVE_UNITS = {
    "w": "weeks", "week": "weeks", "weeks": "weeks",
    "d": "days", "day": "days", "days": "days",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
}


def parse_id_set(value: Optional[str]) -> set[int]:
    return {int(item) for item in re.findall(r"\d+", value or "") if int(item) > 0}


def configured_timezone_name() -> str:
    return get_setting("REMINDER_TIMEZONE") or os.getenv("REMINDER_TIMEZONE") or os.getenv("TZ") or DEFAULT_TIMEZONE


def reminder_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(configured_timezone_name())
    except (ValueError, ZoneInfoNotFoundError):
        logger.warning("Invalid reminder timezone; using %s", DEFAULT_TIMEZONE)
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
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    meridiem = match.group(3)
    if minute > 59:
        raise ValueError(HELPFUL_DATE_ERROR)
    if meridiem:
        if not 1 <= hour <= 12:
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
        r"(\d+(?:\.\d+)?)\s*(weeks?|w|days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
        re.IGNORECASE,
    )
    tokens = list(token_re.finditer(body))
    leftover = token_re.sub(" ", body)
    leftover = re.sub(r"(?:\s|,|\band\b)+", "", leftover, flags=re.IGNORECASE)
    if not tokens or leftover:
        raise ValueError(HELPFUL_DATE_ERROR)
    totals = {"weeks": 0.0, "days": 0.0, "hours": 0.0, "minutes": 0.0}
    for token in tokens:
        totals[RELATIVE_UNITS[token.group(2).casefold()]] += float(token.group(1))
    duration = timedelta(**totals)
    if duration <= timedelta(0):
        raise ValueError("Reminder time must be greater than zero.")
    return duration


def parse_local_datetime(value: str, tz: ZoneInfo, *, now: Optional[datetime] = None) -> datetime:
    text = " ".join(str(value or "").strip().split())
    if not text:
        raise ValueError(HELPFUL_DATE_ERROR)
    for dash in "\u2010\u2011\u2012\u2013\u2014":
        text = text.replace(dash, "-")
    text = re.sub(r"(?i)(\d)(am|pm)\b", r"\1 \2", text)
    reference = _reference_time(now, tz)
    relative = _parse_relative_duration(text)
    if relative is not None:
        return (reference + relative).astimezone(timezone.utc)
    conversational = re.fullmatch(r"(today|tomorrow)(?:\s+(?:at\s+)?(.+))?", text, re.IGNORECASE)
    if conversational:
        day_offset = 1 if conversational.group(1).casefold() == "tomorrow" else 0
        hour, minute = _parse_clock(conversational.group(2) or DEFAULT_DATE_ONLY_TIME)
        local_value = datetime.combine(
            reference.date() + timedelta(days=day_offset),
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=tz,
        )
        return local_value.astimezone(timezone.utc)
    weekday = re.fullmatch(
        r"(?:(next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+(?:at\s+)?(.+))?",
        text,
        re.IGNORECASE,
    )
    if weekday:
        desired = WEEKDAY_NUMBERS[weekday.group(2).casefold()]
        hour, minute = _parse_clock(weekday.group(3) or DEFAULT_DATE_ONLY_TIME)
        days_ahead = (desired - reference.weekday()) % 7
        local_value = datetime.combine(
            reference.date() + timedelta(days=days_ahead),
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=tz,
        )
        if weekday.group(1) or local_value <= reference:
            local_value += timedelta(days=7)
        return local_value.astimezone(timezone.utc)
    for date_format in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, date_format).replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            pass
    for date_format in DATE_ONLY_FORMATS:
        try:
            parsed_date = datetime.strptime(text, date_format).date()
        except ValueError:
            continue
        hour, minute = _parse_clock(DEFAULT_DATE_ONLY_TIME)
        return datetime.combine(
            parsed_date,
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=tz,
        ).astimezone(timezone.utc)
    raise ValueError(HELPFUL_DATE_ERROR)


def parse_utc_text(value: Any) -> datetime:
    return parse_utc(value)


def discord_timestamp(value: Any, style: str = "f") -> str:
    try:
        parsed = parse_utc(value)
    except (TypeError, ValueError):
        return str(value or "unknown")
    return f"<t:{int(parsed.timestamp())}:{style}>"


def timestamp_codes_embed(value: datetime, timezone_name: str) -> discord.Embed:
    unix_timestamp = int(value.astimezone(timezone.utc).timestamp())
    lines = [f"`<t:{unix_timestamp}:{style}>`  <t:{unix_timestamp}:{style}>" for style in TIMESTAMP_STYLES]
    return branded_embed(
        "TIME CODES",
        description=(
            f"Time was parsed using `{timezone_name}`.\n\n"
            "Copy and paste a code below to show the time in each viewer's local timezone.\n\n"
            + "\n".join(lines)
        ),
    )


def parse_recurrence(
    value: str,
    count: Optional[int],
    tz: Optional[ZoneInfo] = None,
) -> tuple[str, int, Optional[int], Optional[datetime]]:
    text = str(value or "none").strip().casefold()
    if text in {"", "none", "once", "one time"}:
        return "none", 1, None, None
    until_at = None
    until_match = re.fullmatch(r"(.+?)\s+until\s+(.+)", text)
    if until_match:
        text = until_match.group(1).strip()
        until_at = parse_local_datetime(until_match.group(2), tz or reminder_timezone())
    count_match = re.fullmatch(r"(.+?)\s+(?:for\s+|x\s*)(\d+)\s*(?:occurrences?)?", text)
    if count_match:
        text = count_match.group(1).strip()
        count = int(count_match.group(2))
    recurrence_count = max(2, min(60, int(count or 60)))
    if text in {"daily", "weekly", "monthly"}:
        return text, 1, recurrence_count, until_at
    match = re.fullmatch(r"(?:every\s+)?(\d+)\s+days?", text)
    if match:
        return "interval", max(1, min(365, int(match.group(1)))), recurrence_count, until_at
    raise ValueError(
        "Recurrence must be `none`, `daily`, `weekly`, `monthly`, or `every N days`; "
        "optionally add `for 10` or `until 2035-08-01`."
    )


def channel_url(row: dict[str, Any]) -> Optional[str]:
    channel_id = row.get("destination_channel_id") or row.get("reminder_channel_id")
    if not str(channel_id or "").isdigit():
        return None
    return f"https://discord.com/channels/{row['guild_id']}/{channel_id}"


def channel_mention(channel_id: Any) -> str:
    return f"<#{channel_id}>" if str(channel_id or "").isdigit() else "Private DM"


def is_sendable_channel(channel: Any) -> bool:
    return hasattr(channel, "send") and hasattr(channel, "permissions_for")


def send_permission_error(bot: commands.Bot, channel: Any) -> Optional[str]:
    guild = getattr(channel, "guild", None)
    member = getattr(guild, "me", None)
    if member is None and guild is not None and getattr(bot, "user", None) is not None:
        member = guild.get_member(bot.user.id)
    if member is None or not hasattr(channel, "permissions_for"):
        return None
    permissions = channel.permissions_for(member)
    missing = []
    if not getattr(permissions, "view_channel", False):
        missing.append("View Channel")
    if getattr(channel, "type", None) in {discord.ChannelType.public_thread, discord.ChannelType.private_thread}:
        if not getattr(permissions, "send_messages_in_threads", False):
            missing.append("Send Messages in Threads")
    elif not getattr(permissions, "send_messages", False):
        missing.append("Send Messages")
    if not getattr(permissions, "embed_links", False):
        missing.append("Embed Links")
    return "Missing bot permission(s): " + ", ".join(missing) if missing else None


class PersonalReminderModal(discord.ui.Modal):
    def __init__(self, cog: "ReminderCog", destination: Any = None, target: Any = None) -> None:
        super().__init__(title="Create a personal reminder", timeout=300)
        self.cog, self.destination, self.target = cog, destination, target
        self.reminder_title = discord.ui.TextInput(label="Reminder", placeholder="Submit the event plan", max_length=100)
        self.details = discord.ui.TextInput(label="Details (optional)", required=False, style=discord.TextStyle.paragraph, max_length=MAX_REMINDER_MESSAGE_LENGTH)
        self.when = discord.ui.TextInput(label="When", placeholder="tomorrow at 9am", max_length=100)
        self.recurrence = discord.ui.TextInput(label="Recurrence (optional)", placeholder="weekly for 10 or monthly until 2035-12-01", required=False, max_length=100)
        self.repeat_count = discord.ui.TextInput(label="Occurrences (optional, max 60)", placeholder="Leave blank for the rolling limit", required=False, max_length=2)
        for item in (self.reminder_title, self.details, self.when, self.recurrence, self.repeat_count):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.preview_personal(
            interaction,
            title=str(self.reminder_title.value),
            details=str(self.details.value),
            when=str(self.when.value),
            recurrence=str(self.recurrence.value),
            count=str(self.repeat_count.value),
            destination=self.destination,
            target=self.target,
        )


class EventReminderModal(discord.ui.Modal):
    def __init__(self, cog: "ReminderCog", public_channel: Any, destination: Any, host: Any = None) -> None:
        super().__init__(title="Create an event reminder", timeout=300)
        self.cog, self.public_channel, self.destination, self.host = cog, public_channel, destination, host
        self.event_name = discord.ui.TextInput(label="Event name", placeholder="Movie Night: Bottoms", max_length=100)
        self.details = discord.ui.TextInput(label="Details (optional)", required=False, style=discord.TextStyle.paragraph, max_length=MAX_REMINDER_MESSAGE_LENGTH)
        self.when = discord.ui.TextInput(label="When", placeholder="next Friday at 8pm", max_length=100)
        self.timings = discord.ui.TextInput(label="Reminder timings", default="15m, start", placeholder="start, 15m, 1h, 1d", max_length=100)
        self.recurrence = discord.ui.TextInput(label="Recurrence (optional)", placeholder="weekly for 10 or monthly until 2035-12-01", required=False, max_length=100)
        for item in (self.event_name, self.details, self.when, self.timings, self.recurrence):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.preview_event(
            interaction,
            title=str(self.event_name.value),
            details=str(self.details.value),
            when=str(self.when.value),
            timings=str(self.timings.value),
            recurrence=str(self.recurrence.value),
            public_channel=self.public_channel,
            destination=self.destination,
            host=self.host,
        )


class CreationPreviewView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, draft: dict[str, Any]) -> None:
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.draft = cog, owner_id, draft

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person creating this reminder can confirm it.", ephemeral=True)
        return False

    @discord.ui.button(label="Create Reminder", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.confirm_creation(interaction, self.draft)
        self.stop()


class EventCreationPreviewView(CreationPreviewView):
    def __init__(self, cog: "ReminderCog", owner_id: int, draft: dict[str, Any]) -> None:
        super().__init__(cog, owner_id, draft)
        states = {
            "Auto-subscribe": draft["auto_subscribe_creator"],
            "Custom timing": draft["allow_custom_timing"],
            "Close at start": draft["close_subscriptions_at_start"],
            "Keep card": draft["keep_public_card"],
        }
        for item in self.children:
            if not isinstance(item, discord.ui.Button) or not item.label:
                continue
            prefix = str(item.label).split(":", 1)[0]
            if prefix in states:
                item.label = f"{prefix}: {'On' if states[prefix] else 'Off'}"

    @discord.ui.button(label="Auto-subscribe: On", style=discord.ButtonStyle.secondary)
    async def toggle_auto_subscribe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.draft["auto_subscribe_creator"] = not self.draft["auto_subscribe_creator"]
        button.label = f"Auto-subscribe: {'On' if self.draft['auto_subscribe_creator'] else 'Off'}"
        await interaction.response.edit_message(embed=self.cog.preview_embed(self.draft), view=self)

    @discord.ui.button(label="Custom timing: On", style=discord.ButtonStyle.secondary)
    async def toggle_custom_timing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.draft["allow_custom_timing"] = not self.draft["allow_custom_timing"]
        button.label = f"Custom timing: {'On' if self.draft['allow_custom_timing'] else 'Off'}"
        await interaction.response.edit_message(embed=self.cog.preview_embed(self.draft), view=self)

    @discord.ui.button(label="Close at start: On", style=discord.ButtonStyle.secondary)
    async def toggle_close_at_start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.draft["close_subscriptions_at_start"] = not self.draft["close_subscriptions_at_start"]
        button.label = f"Close at start: {'On' if self.draft['close_subscriptions_at_start'] else 'Off'}"
        await interaction.response.edit_message(embed=self.cog.preview_embed(self.draft), view=self)

    @discord.ui.button(label="Keep card: On", style=discord.ButtonStyle.secondary)
    async def toggle_keep_card(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.draft["keep_public_card"] = not self.draft["keep_public_card"]
        button.label = f"Keep card: {'On' if self.draft['keep_public_card'] else 'Off'}"
        await interaction.response.edit_message(embed=self.cog.preview_embed(self.draft), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Reminder creation cancelled.", embed=None, view=None)
        self.stop()


class LegacyStartView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, kind: str, **values: Any) -> None:
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.kind, self.values = cog, owner_id, kind, values

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def proceed(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.kind == "personal":
            await interaction.response.send_modal(PersonalReminderModal(self.cog, self.values.get("destination"), self.values.get("target")))
        else:
            await interaction.response.send_modal(EventReminderModal(self.cog, self.values["public_channel"], self.values["destination"]))


class ReminderCreateModal(PersonalReminderModal):
    """Compatibility alias for older integrations and tests."""


class RemindSubscribeModal(EventReminderModal):
    """Compatibility alias for older integrations and tests."""


class TimingSelect(discord.ui.Select):
    def __init__(self, selected: Sequence[int]) -> None:
        values = set(selected)
        options = [
            discord.SelectOption(label="When the event begins", value="0", default=0 in values),
            discord.SelectOption(label="15 minutes before", value="15", default=15 in values),
            discord.SelectOption(label="1 hour before", value="60", default=60 in values),
            discord.SelectOption(label="1 day before", value="1440", default=1440 in values),
        ]
        super().__init__(placeholder="Choose reminder timings", min_values=1, max_values=4, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, SubscriptionControlsView):
            await view.cog.change_subscription_timing(interaction, view.subscription_id, [int(value) for value in self.values])


class CustomTimingModal(discord.ui.Modal):
    def __init__(self, cog: "ReminderCog", subscription_id: int) -> None:
        super().__init__(title="Custom reminder timing", timeout=300)
        self.cog, self.subscription_id = cog, subscription_id
        self.timings = discord.ui.TextInput(
            label="Timings",
            placeholder="30m, 2h, start",
            max_length=100,
        )
        self.add_item(self.timings)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            offsets = parse_offsets(str(self.timings.value))
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.cog.change_subscription_timing(interaction, self.subscription_id, offsets)


class SubscriptionControlsView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, subscription_id: int, row: dict[str, Any], *, include_select: bool = False) -> None:
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.subscription_id, self.row = cog, owner_id, subscription_id, row
        if include_select and row.get("allow_custom_timing"):
            selected = parse_offsets(row.get("custom_offsets_json") or row.get("default_offsets_json"))
            self.add_item(TimingSelect(selected))
        destination = channel_url(row)
        if destination:
            self.add_item(discord.ui.Button(label="Open Event Channel", style=discord.ButtonStyle.link, url=destination))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("That subscription belongs to another member.", ephemeral=True)
        return False

    @discord.ui.button(label="Change Timing", emoji="⏱️", style=discord.ButtonStyle.primary)
    async def timing(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self.cog.subscription_detail(self.subscription_id, interaction.user.id)
        if row is None:
            await interaction.response.send_message("That active subscription was not found.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Choose one or more private DM timings.",
            view=SubscriptionControlsView(self.cog, self.owner_id, self.subscription_id, row, include_select=True),
            ephemeral=True,
        )

    @discord.ui.button(label="Use Event Defaults", style=discord.ButtonStyle.secondary)
    async def defaults(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.change_subscription_timing(interaction, self.subscription_id, None)

    @discord.ui.button(label="Custom Timing", style=discord.ButtonStyle.secondary)
    async def custom_timing(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self.cog.subscription_detail(self.subscription_id, interaction.user.id)
        if row is None or not row.get("allow_custom_timing"):
            await interaction.response.send_message("Custom timing is unavailable for this event.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomTimingModal(self.cog, self.subscription_id))

    @discord.ui.button(label="Unsubscribe", emoji="🔕", style=discord.ButtonStyle.danger)
    async def unsubscribe(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.unsubscribe_interaction(interaction, self.subscription_id)


class ReminderSelect(discord.ui.Select):
    def __init__(self, rows: list[dict[str, Any]], kind: str) -> None:
        self.kind = kind
        options = []
        for row in rows[:25]:
            item_id = row["id"]
            label = row.get("title") or "Untitled reminder"
            description = f"{discord_timestamp(row.get('scheduled_at_utc'), 'f')} • {row.get('status') or row.get('event_status')}"
            options.append(discord.SelectOption(label=truncate(label, 100), value=str(item_id), description=truncate(description, 100)))
        super().__init__(placeholder="Choose a reminder" if kind == "manage" else "Choose an event subscription", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, ReminderManageView):
            self.view.selected_id = int(self.values[0])
            row = await self.view.cog.service.get_reminder(self.view.selected_id)
            await interaction.response.edit_message(embed=self.view.cog.reminder_detail_embed(row), view=self.view)
        elif isinstance(self.view, SubscriptionListView):
            self.view.selected_id = int(self.values[0])
            row = await self.view.cog.subscription_detail(self.view.selected_id, interaction.user.id)
            await interaction.response.edit_message(embed=self.view.cog.subscription_embed(row), view=self.view)


class SubscriptionListView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, rows: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.selected_id = cog, owner_id, int(rows[0]["id"])
        self.add_item(ReminderSelect(rows, "subscriptions"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Manage Selected", style=discord.ButtonStyle.primary)
    async def manage_selected(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self.cog.subscription_detail(self.selected_id, interaction.user.id)
        if row is None:
            await interaction.response.send_message("That active subscription was not found.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self.cog.subscription_embed(row), view=SubscriptionControlsView(self.cog, self.owner_id, self.selected_id, row), ephemeral=True)


class ReminderEditModal(discord.ui.Modal):
    def __init__(self, cog: "ReminderCog", reminder_id: int, row: dict[str, Any]) -> None:
        super().__init__(title="Edit reminder", timeout=300)
        self.cog, self.reminder_id = cog, reminder_id
        self.reminder_title = discord.ui.TextInput(label="Title", default=row["title"], max_length=100)
        self.details = discord.ui.TextInput(label="Details", default=row["description"][:4000], required=False, style=discord.TextStyle.paragraph, max_length=4000)
        self.when = discord.ui.TextInput(label="When", default=discord_timestamp(row["scheduled_at_utc"], "f"), placeholder="Leave Discord timestamp unchanged or enter a new time", max_length=100)
        self.timings = discord.ui.TextInput(label="Default timings", default=", ".join("start" if value == 0 else f"{value}m" for value in parse_offsets(row["default_offsets_json"])), required=row["reminder_type"] == "event", max_length=100)
        for item in (self.reminder_title, self.details, self.when, self.timings):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.apply_edit(
            interaction,
            self.reminder_id,
            title=str(self.reminder_title.value),
            details=str(self.details.value),
            when=str(self.when.value),
            timings=str(self.timings.value),
        )


class CancelConfirmView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, reminder_id: int) -> None:
        super().__init__(timeout=120)
        self.cog, self.owner_id, self.reminder_id = cog, owner_id, reminder_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Cancel Reminder", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self.cog.cancel_interaction(interaction, self.reminder_id)


class DeleteConfirmView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, reminder_id: int, staff: bool) -> None:
        super().__init__(timeout=120)
        self.cog, self.owner_id, self.reminder_id, self.staff = cog, owner_id, reminder_id, staff

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Delete From Normal Views", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            changed = await self.cog.service.archive_reminder(
                self.reminder_id,
                interaction.user.id,
                staff=self.staff,
            )
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            "Reminder removed from normal views. Delivery and audit history were retained."
            if changed else "That reminder was already deleted.",
            ephemeral=True,
        )


class ReminderManageView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, rows: list[dict[str, Any]], staff: bool) -> None:
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.staff, self.selected_id = cog, owner_id, staff, int(rows[0]["id"])
        self.add_item(ReminderSelect(rows, "manage"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the person who opened this panel can use it.", ephemeral=True)
        return False

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self.cog.service.get_reminder(self.selected_id)
        if row is None:
            await interaction.response.send_message("That reminder was not found.", ephemeral=True)
            return
        await interaction.response.send_modal(ReminderEditModal(self.cog, self.selected_id, row))

    @discord.ui.button(label="Duplicate", style=discord.ButtonStyle.secondary)
    async def duplicate(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            copy = await self.cog.service.duplicate_reminder(self.selected_id, interaction.user.id, staff=self.staff)
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if copy["reminder_type"] == "event":
            if not await self.cog.publish_event_card(copy):
                await self.cog.service.cancel_reminder(
                    int(copy["id"]),
                    interaction.user.id,
                    reason="Duplicate event card could not be posted",
                    staff=True,
                )
                await interaction.followup.send(
                    "The copy was saved but cancelled because its public event card could not be posted.",
                    ephemeral=True,
                )
                return
        await interaction.followup.send(f"Created a copy: **{copy['title']}** for {discord_timestamp(copy['scheduled_at_utc'], 'f')}.", ephemeral=True)

    @discord.ui.button(label="Subscribers", style=discord.ButtonStyle.secondary)
    async def subscribers(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        rows = await self.cog.service.fetch_all(
            "SELECT user_id, status, custom_offsets_json FROM reminder_subscriptions WHERE reminder_id = ? ORDER BY created_at_utc",
            (self.selected_id,),
        )
        text = "\n".join(f"<@{row['user_id']}> • {row['status']}" for row in rows[:50]) or "No subscriptions."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=SAFE_MENTIONS)

    @discord.ui.button(label="Occurrences", style=discord.ButtonStyle.secondary)
    async def occurrences(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        rows = await self.cog.service.fetch_all(
            "SELECT * FROM reminder_occurrences WHERE reminder_id = ? ORDER BY occurrence_index LIMIT 60",
            (self.selected_id,),
        )
        if not rows:
            await interaction.response.send_message("No occurrence records were found.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=occurrence_embed(rows[0]),
            view=OccurrenceManageView(self.cog, self.owner_id, rows, self.staff),
            ephemeral=True,
        )

    @discord.ui.button(label="Destination", style=discord.ButtonStyle.secondary)
    async def destination(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Choose a replacement destination channel.",
            view=DestinationEditView(self.cog, self.owner_id, self.selected_id, self.staff),
            ephemeral=True,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Deletion removes a completed or cancelled reminder from normal views but preserves its audit and delivery history.",
            view=DeleteConfirmView(self.cog, self.owner_id, self.selected_id, self.staff),
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message("This preserves history and cancels all future deliveries. Confirm?", view=CancelConfirmView(self.cog, self.owner_id, self.selected_id), ephemeral=True)


class EventDestinationSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Or choose a text, voice, or Stage destination",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.voice,
                discord.ChannelType.stage_voice,
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, EventStartView):
            await interaction.response.send_modal(
                EventReminderModal(
                    self.view.cog,
                    self.view.public_channel,
                    self.values[0],
                    self.view.host,
                )
            )


class EventStartView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, public_channel: Any, host: Any = None) -> None:
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.public_channel, self.host = cog, owner_id, public_channel, host
        self.add_item(EventDestinationSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Only the event creator can use this setup panel.", ephemeral=True)
        return False

    @discord.ui.button(label="Use Current Channel", style=discord.ButtonStyle.primary)
    async def current_channel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(EventReminderModal(self.cog, self.public_channel, self.public_channel, self.host))


def occurrence_embed(row: dict[str, Any]) -> discord.Embed:
    embed = branded_embed(f"Occurrence #{int(row['occurrence_index']) + 1}", footer="Private occurrence management")
    embed.add_field(name="When", value=f"{discord_timestamp(row['scheduled_at_utc'], 'F')}\n{discord_timestamp(row['scheduled_at_utc'], 'R')}", inline=False)
    embed.add_field(name="Status", value=str(row["status"]).title(), inline=True)
    return embed


class OccurrenceSelect(discord.ui.Select):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        options = [
            discord.SelectOption(
                label=f"Occurrence #{int(row['occurrence_index']) + 1}",
                value=str(row["id"]),
                description=truncate(f"{discord_timestamp(row['scheduled_at_utc'], 'f')} • {row['status']}", 100),
            )
            for row in rows[:25]
        ]
        super().__init__(placeholder="Choose an occurrence", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, OccurrenceManageView):
            self.view.selected_id = int(self.values[0])
            row = await self.view.cog.service.fetch_one("SELECT * FROM reminder_occurrences WHERE id = ?", (self.view.selected_id,))
            await interaction.response.edit_message(embed=occurrence_embed(row), view=self.view)


class OccurrenceEditModal(discord.ui.Modal):
    def __init__(self, cog: "ReminderCog", occurrence_id: int, row: dict[str, Any], staff: bool) -> None:
        super().__init__(title="Reschedule occurrence", timeout=300)
        self.cog, self.occurrence_id, self.staff = cog, occurrence_id, staff
        self.when = discord.ui.TextInput(label="New date/time", placeholder="next Friday at 8pm", max_length=100)
        self.scope = discord.ui.TextInput(label="Apply to", default="one", placeholder="one, future, or all", max_length=10)
        self.add_item(self.when)
        self.add_item(self.scope)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.service.fetch_one(
            "SELECT r.interpretation_timezone, o.reminder_id FROM reminder_occurrences o JOIN reminder_items r ON r.id = o.reminder_id WHERE o.id = ?",
            (self.occurrence_id,),
        )
        if row is None:
            await interaction.followup.send("That occurrence was not found.", ephemeral=True)
            return
        try:
            parsed = parse_local_datetime(str(self.when.value), ZoneInfo(row["interpretation_timezone"]))
            occurrence = await self.cog.service.reschedule_occurrence(
                self.occurrence_id,
                interaction.user.id,
                parsed,
                scope=str(self.scope.value).strip().casefold(),
                staff=self.staff,
            )
        except (ValueError, PermissionError, ZoneInfoNotFoundError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.cog.refresh_public_card(int(row["reminder_id"]))
        await interaction.followup.send("✅ Occurrence schedule updated.", embed=occurrence_embed(occurrence), ephemeral=True)


class OccurrenceManageView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, rows: list[dict[str, Any]], staff: bool) -> None:
        super().__init__(timeout=300)
        self.cog, self.owner_id, self.staff, self.selected_id = cog, owner_id, staff, int(rows[0]["id"])
        self.add_item(OccurrenceSelect(rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Reschedule", style=discord.ButtonStyle.primary)
    async def reschedule(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        row = await self.cog.service.fetch_one("SELECT * FROM reminder_occurrences WHERE id = ?", (self.selected_id,))
        if row is None or row["status"] != "upcoming":
            await interaction.response.send_message("That upcoming occurrence was not found.", ephemeral=True)
            return
        await interaction.response.send_modal(OccurrenceEditModal(self.cog, self.selected_id, row, self.staff))

    @discord.ui.button(label="Cancel Occurrence", style=discord.ButtonStyle.danger)
    async def cancel_occurrence(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.service.fetch_one("SELECT reminder_id FROM reminder_occurrences WHERE id = ?", (self.selected_id,))
        try:
            changed = await self.cog.service.cancel_occurrence(self.selected_id, interaction.user.id, staff=self.staff)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if not changed:
            await interaction.followup.send("That upcoming occurrence was not found.", ephemeral=True)
            return
        if row:
            await self.cog.refresh_public_card(int(row["reminder_id"]))
        await interaction.followup.send("Occurrence cancelled; the rest of the series remains active.", ephemeral=True)


class DestinationChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose the destination",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice, discord.ChannelType.stage_voice],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, DestinationEditView):
            return
        channel = self.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            updated, changes = await self.view.cog.service.update_reminder(
                self.view.reminder_id,
                interaction.user.id,
                staff=self.view.staff,
                destination_channel_id=int(channel.id),
                destination_channel_name=str(getattr(channel, "name", "")),
            )
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.view.cog.refresh_public_card(self.view.reminder_id)
        if "destination" in changes and updated["reminder_type"] == "event":
            await self.view.cog.notify_event_update(updated, changes)
        await interaction.followup.send("✅ Reminder destination updated.", embed=self.view.cog.reminder_detail_embed(updated), ephemeral=True)


class DestinationEditView(discord.ui.View):
    def __init__(self, cog: "ReminderCog", owner_id: int, reminder_id: int, staff: bool) -> None:
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.reminder_id, self.staff = cog, owner_id, reminder_id, staff
        self.add_item(DestinationChannelSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Deliver by DM", style=discord.ButtonStyle.secondary)
    async def deliver_by_dm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog.service.get_reminder(self.reminder_id)
        if row is None or row["reminder_type"] != "personal":
            await interaction.followup.send("DM delivery is only available for personal reminders.", ephemeral=True)
            return
        try:
            updated, _changes = await self.cog.service.update_reminder(
                self.reminder_id,
                interaction.user.id,
                staff=self.staff,
                clear_destination=True,
            )
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("✅ Personal reminder delivery changed to DM.", embed=self.cog.reminder_detail_embed(updated), ephemeral=True)


class ReminderCog(commands.Cog):
    remind = app_commands.Group(name="remind", description="Create and manage reminders")
    legacy_reminder = app_commands.Group(name="reminder", description="Legacy reminder commands")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = ReminderService(bot.db)
        self._scheduler_lock: Optional[asyncio.Lock] = None
        self._refresh_tasks: dict[int, asyncio.Task[Any]] = {}

    async def cog_load(self) -> None:
        report = await self.create_schema()
        await self.restore_persistent_views()
        if not self.reminder_scheduler.is_running():
            self.reminder_scheduler.start()
        logger.info("Reminder system ready migration=%s", report.as_dict())

    async def cog_unload(self) -> None:
        if self.reminder_scheduler.is_running():
            self.reminder_scheduler.cancel()
        for pending in self._refresh_tasks.values():
            pending.cancel()
        self._refresh_tasks.clear()

    async def create_schema(self):
        # Old tables are retained solely as compatibility/migration sources.
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
                status TEXT NOT NULL DEFAULT 'pending',
                failure_reason TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT,
                sent_at_utc TEXT
            );
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
                status TEXT NOT NULL DEFAULT 'open',
                failure_reason TEXT,
                created_at_utc TEXT NOT NULL,
                completed_at_utc TEXT
            );
            CREATE TABLE IF NOT EXISTS reminder_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'subscribed',
                subscribed_at_utc TEXT NOT NULL,
                cancelled_at_utc TEXT,
                processing_at_utc TEXT,
                sent_at_utc TEXT,
                dm_confirmation_message_id TEXT,
                dm_reminder_message_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                failure_reason TEXT,
                UNIQUE (post_id, user_id)
            );
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
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "destination_channel_id" not in columns:
            await self.bot.db.execute(
                "ALTER TABLE reminder_subscription_posts ADD COLUMN destination_channel_id TEXT"
            )
        if "destination_channel_name" not in columns:
            await self.bot.db.execute(
                "ALTER TABLE reminder_subscription_posts ADD COLUMN destination_channel_name TEXT"
            )
        await self.bot.db.commit()
        return await self.service.initialize()

    async def fetch_one(self, sql: str, parameters: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        return await self.service.fetch_one(sql, parameters)

    async def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await self.service.fetch_all(sql, parameters)

    def has_staff_access(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not hasattr(interaction.user, "roles"):
            return False
        if self.is_owner_or_administrator(interaction):
            return True
        allowed_ids = set(get_csv_ids_setting("REMINDER_ALLOWED_ROLE_IDS"))
        allowed_ids.update(parse_id_set(os.getenv("REMINDER_ALLOWED_ROLE_IDS", "")))
        allowed_ids.update(get_csv_ids_setting("STAFF_NOTES_ALLOWED_ROLE_IDS"))
        allowed_ids.update(get_csv_ids_setting("STAFF_AI_ALLOWED_ROLE_IDS"))
        allowed_ids.update(parse_id_set(get_setting("staff_role_ids", "")))
        allowed_ids.update(parse_id_set(get_setting("admin_role_ids", "")))
        return any(role.id in allowed_ids for role in interaction.user.roles)

    @staticmethod
    def member_has_any_role(member: Any, role_ids: set[int]) -> bool:
        return any(getattr(role, "id", None) in role_ids for role in getattr(member, "roles", ()))

    def is_owner_or_administrator(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not hasattr(interaction.user, "roles"):
            return False
        if interaction.user.id in get_csv_ids_setting("BOT_OWNER_USER_IDS"):
            return True
        return bool(getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False))

    def has_manage_all_access(self, interaction: discord.Interaction) -> bool:
        if self.is_owner_or_administrator(interaction):
            return True
        allowed_ids = set(get_csv_ids_setting("REMINDER_MANAGE_ALL_ROLE_IDS"))
        if allowed_ids:
            return self.member_has_any_role(interaction.user, allowed_ids)
        return self.has_staff_access(interaction)

    def has_remind_command_access(self, interaction: discord.Interaction, command: str) -> bool:
        if self.is_owner_or_administrator(interaction):
            return True
        setting_keys = {
            "personal": "REMINDER_PERSONAL_ALLOWED_ROLE_IDS",
            "event": "REMINDER_EVENT_ALLOWED_ROLE_IDS",
            "manage": "REMINDER_MANAGE_ALLOWED_ROLE_IDS",
            "subscriptions": "REMINDER_SUBSCRIPTIONS_ALLOWED_ROLE_IDS",
        }
        setting_key = setting_keys.get(command)
        if setting_key is None:
            return True
        allowed_ids = set(get_csv_ids_setting(setting_key))
        if allowed_ids:
            if command == "manage" and self.has_manage_all_access(interaction):
                return True
            return self.member_has_any_role(interaction.user, allowed_ids)
        if command == "event":
            return self.has_staff_access(interaction)
        return True

    async def ensure_remind_command_access(
        self,
        interaction: discord.Interaction,
        command: str,
    ) -> bool:
        if self.has_remind_command_access(interaction, command):
            return True
        await self.send_private(
            interaction,
            f"You need one of the configured roles to use `/remind {command}`.",
        )
        return False

    def member_has_staff_access(self, guild: Any, member: Any) -> bool:
        interaction = type("AccessInteraction", (), {"guild": guild, "user": member})()
        return self.has_staff_access(interaction)

    async def ensure_staff_access(self, interaction: discord.Interaction) -> bool:
        if self.has_staff_access(interaction):
            return True
        await self.send_private(interaction, "Event and staff-targeted reminders are limited to configured staff.")
        return False

    def can_target_user(self, interaction: discord.Interaction, target: discord.abc.User) -> bool:
        return target.id == interaction.user.id or self.has_staff_access(interaction)

    async def defer_private(self, interaction: discord.Interaction, *, thinking: bool = False) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=thinking)

    async def send_private(self, interaction: discord.Interaction, content: Optional[str] = None, **kwargs: Any) -> None:
        kwargs.setdefault("allowed_mentions", SAFE_MENTIONS)
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True, **kwargs)
        else:
            await interaction.response.send_message(content, ephemeral=True, **kwargs)

    async def user_timezone_name(self, guild_id: Any, user_id: Any) -> str:
        row = await self.service.fetch_one(
            "SELECT timezone_name FROM user_timezones WHERE guild_id = ? AND user_id = ?",
            (str(guild_id), str(user_id)),
        )
        if row:
            try:
                ZoneInfo(row["timezone_name"])
                return str(row["timezone_name"])
            except (ValueError, ZoneInfoNotFoundError):
                logger.warning("Invalid stored reminder timezone guild=%s user=%s", guild_id, user_id)
        return configured_timezone_name()

    async def user_timezone(self, guild_id: Any, user_id: Any) -> ZoneInfo:
        try:
            return ZoneInfo(await self.user_timezone_name(guild_id, user_id))
        except (ValueError, ZoneInfoNotFoundError):
            return reminder_timezone()

    async def save_user_timezone(self, guild_id: int, user_id: int, timezone_name: str) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO user_timezones (guild_id, user_id, timezone_name, updated_at_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                timezone_name = excluded.timezone_name,
                updated_at_utc = excluded.updated_at_utc
            """,
            (str(guild_id), str(user_id), timezone_name, utc_text()),
        )
        await self.bot.db.commit()

    def event_embed(self, row: dict[str, Any]) -> discord.Embed:
        status_prefix = "❌ " if row["status"] == "cancelled" else "✅ " if row["status"] == "completed" else "🔔 "
        embed = branded_embed(
            status_prefix + row["title"],
            description=truncate(row.get("description") or "", 4096) or None,
            color=discord.Color(COLOR),
            footer=(
                "This event was cancelled."
                if row["status"] == "cancelled"
                else "This event is complete."
                if row["status"] == "completed"
                else "Select Remind Me to receive private event reminders."
            ),
        )
        embed.add_field(name="When", value=f"{discord_timestamp(row['scheduled_at_utc'], 'F')}\n{discord_timestamp(row['scheduled_at_utc'], 'R')}", inline=False)
        embed.add_field(name="Where", value=channel_mention(row.get("destination_channel_id")), inline=True)
        embed.add_field(name="Hosted by", value=f"<@{row.get('host_user_id') or row['creator_user_id']}>", inline=True)
        embed.add_field(name="Reminders", value=timing_summary(parse_offsets(row["default_offsets_json"])), inline=False)
        embed.add_field(name="Subscribers", value=str(row.get("subscriber_count", 0)), inline=True)
        if row["recurrence_type"] != "none":
            embed.add_field(name="Recurrence", value=row["recurrence_type"].title(), inline=True)
        return embed

    def preview_embed(self, draft: dict[str, Any]) -> discord.Embed:
        embed = branded_embed(
            "Confirm Event" if draft["reminder_type"] == "event" else "Confirm Personal Reminder",
            description=draft["description"] or "No additional details.",
            footer="Nothing is scheduled until you select Create Reminder.",
        )
        embed.add_field(name="Title", value=draft["title"], inline=False)
        embed.add_field(name="When", value=f"{discord_timestamp(draft['scheduled_at_utc'], 'F')}\n{discord_timestamp(draft['scheduled_at_utc'], 'R')}\nInterpreted in `{draft['interpretation_timezone']}`", inline=False)
        embed.add_field(name="Where", value=channel_mention(getattr(draft.get("destination"), "id", None)) if draft.get("destination") else "Private DM", inline=True)
        embed.add_field(name="Reminders", value=timing_summary(draft["default_offsets"]), inline=True)
        recurrence_value = draft["recurrence_type"].title()
        if draft.get("recurrence_end_at_utc"):
            recurrence_value += f" until {discord_timestamp(draft['recurrence_end_at_utc'], 'D')}"
        elif draft.get("recurrence_end_count"):
            recurrence_value += f" · {draft['recurrence_end_count']} occurrences"
        embed.add_field(name="Recurrence", value=recurrence_value, inline=True)
        if draft["reminder_type"] == "event":
            embed.add_field(name="Hosted by", value=f"<@{draft['host_user_id']}>", inline=True)
            embed.add_field(
                name="Subscription settings",
                value=(
                    f"Creator auto-subscribe: **{'On' if draft['auto_subscribe_creator'] else 'Off'}**\n"
                    f"Custom timing: **{'On' if draft['allow_custom_timing'] else 'Off'}**\n"
                    f"Close at start: **{'On' if draft['close_subscriptions_at_start'] else 'Off'}**\n"
                    f"Keep public card: **{'On' if draft['keep_public_card'] else 'Off'}**"
                ),
                inline=False,
            )
        return embed

    def personal_confirmation_embed(self, row: dict[str, Any]) -> discord.Embed:
        embed = branded_embed("🔔 Reminder Set", description=row.get("description") or None, color=SUCCESS_COLOR, footer="Private reminder confirmation")
        embed.add_field(name="Reminder", value=row["title"], inline=False)
        embed.add_field(name="When", value=f"{discord_timestamp(row['scheduled_at_utc'], 'F')}\n{discord_timestamp(row['scheduled_at_utc'], 'R')}", inline=False)
        embed.add_field(name="Delivery", value=channel_mention(row.get("destination_channel_id")) if row.get("destination_channel_id") else "Private DM", inline=True)
        if row["recurrence_type"] != "none":
            embed.add_field(name="Recurrence", value=row["recurrence_type"].title(), inline=True)
        return embed

    def subscription_embed(self, row: Optional[dict[str, Any]]) -> discord.Embed:
        if row is None:
            return error_embed("Subscription unavailable", "That event subscription was not found.")
        offsets = parse_offsets(row.get("custom_offsets_json") or row.get("default_offsets_json"))
        embed = branded_embed("🔔 Reminder Set", description=row.get("description") or None, color=SUCCESS_COLOR)
        embed.add_field(name="Event", value=row["title"], inline=False)
        embed.add_field(name="When", value=f"{discord_timestamp(row['scheduled_at_utc'], 'F')}\n{discord_timestamp(row['scheduled_at_utc'], 'R')}", inline=False)
        embed.add_field(name="Where", value=channel_mention(row.get("destination_channel_id")), inline=True)
        embed.add_field(name="Reminders", value=timing_summary(offsets), inline=True)
        embed.add_field(name="Hosted by", value=f"<@{row.get('host_user_id') or row['creator_user_id']}>", inline=True)
        return embed

    def delivery_embed(self, row: dict[str, Any]) -> discord.Embed:
        offset = int(row["offset_minutes"])
        title = "🎉 Starting Now" if offset == 0 else f"🔔 Starting in {timing_label(offset).replace(' before', '').title()}"
        embed = branded_embed(title, description=row.get("description") or None)
        embed.add_field(name="Event", value=row["title"], inline=False)
        embed.add_field(name="When", value=f"{discord_timestamp(row['occurrence_at_utc'], 'F')}\n{discord_timestamp(row['occurrence_at_utc'], 'R')}", inline=False)
        embed.add_field(name="Where", value=channel_mention(row.get("reminder_channel_id")), inline=True)
        embed.add_field(name="Hosted by", value=f"<@{row.get('host_user_id') or row['creator_user_id']}>", inline=True)
        return embed

    def reminder_detail_embed(self, row: Optional[dict[str, Any]]) -> discord.Embed:
        if row is None:
            return error_embed("Reminder unavailable", "That reminder was not found.")
        embed = branded_embed(row["title"], description=row.get("description") or None, footer="Private reminder management")
        embed.add_field(name="Type", value=row["reminder_type"].title(), inline=True)
        embed.add_field(name="Status", value=row["status"].title(), inline=True)
        embed.add_field(name="When", value=f"{discord_timestamp(row['scheduled_at_utc'], 'F')}\n{discord_timestamp(row['scheduled_at_utc'], 'R')}", inline=False)
        embed.add_field(name="Destination", value=channel_mention(row.get("destination_channel_id")), inline=True)
        embed.add_field(name="Subscribers", value=str(row.get("subscriber_count", 0)), inline=True)
        embed.add_field(name="Recurrence", value=row["recurrence_type"].title(), inline=True)
        return embed

    @staticmethod
    def event_view(row: dict[str, Any], *, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Remind Me",
            emoji="🔔",
            style=discord.ButtonStyle.primary,
            custom_id=f"broeden:remind:event:join:{row['id']}",
            disabled=disabled,
        ))
        destination = channel_url(row)
        if destination:
            view.add_item(discord.ui.Button(label="Open Channel", style=discord.ButtonStyle.link, url=destination))
        return view

    @staticmethod
    def dm_subscription_view(row: dict[str, Any], subscription_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        destination = channel_url(row)
        if destination:
            view.add_item(discord.ui.Button(label="Open Event Channel", style=discord.ButtonStyle.link, url=destination))
        view.add_item(discord.ui.Button(label="Change Timing", style=discord.ButtonStyle.secondary, custom_id=f"broeden:remind:sub:timing:{subscription_id}"))
        view.add_item(discord.ui.Button(label="Cancel Reminder", style=discord.ButtonStyle.danger, custom_id=f"broeden:remind:sub:cancel:{subscription_id}"))
        return view

    async def restore_persistent_views(self) -> None:
        if not hasattr(self.bot, "add_view"):
            return
        rows = await self.service.fetch_all(
            "SELECT * FROM reminder_items WHERE reminder_type = 'event' AND status = 'upcoming' AND public_message_id IS NOT NULL"
        )
        restored = 0
        for row in rows:
            try:
                self.bot.add_view(self.event_view(row), message_id=int(row["public_message_id"]))
                restored += 1
            except (TypeError, ValueError):
                logger.warning("Persistent event view skipped reminder_id=%s", row["id"])
        logger.info("Persistent reminder views restored count=%s", restored)

    async def publish_event_card(self, row: dict[str, Any]) -> bool:
        channel_id = row.get("public_channel_id") or row.get("destination_channel_id")
        if not str(channel_id or "").isdigit():
            return False
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False
        if channel is None or not hasattr(channel, "send"):
            return False
        try:
            message = await channel.send(
                embed=self.event_embed(row),
                view=self.event_view(row),
                allowed_mentions=SAFE_MENTIONS,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Duplicate event publish failed reminder_id=%s", row["id"])
            return False
        await self.service.set_public_message(int(row["id"]), int(channel.id), int(message.id))
        return True

    async def preview_personal(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        details: str,
        when: str,
        recurrence: str,
        count: str,
        destination: Any,
        target: Any,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_remind_command_access(interaction, "personal"):
            return
        target = target or interaction.user
        if not self.can_target_user(interaction, target):
            await self.send_private(interaction, "You may only create personal reminders for yourself.")
            return
        if destination is not None and not self.has_staff_access(interaction):
            await self.send_private(interaction, "Only configured staff may deliver a personal reminder into a server channel.")
            return
        try:
            user_tz = await self.user_timezone(interaction.guild_id, interaction.user.id)
            scheduled = parse_local_datetime(when, user_tz)
            recurrence_type, interval, recurrence_count, recurrence_end_at = parse_recurrence(
                recurrence,
                int(count) if count.strip().isdigit() else None,
                user_tz,
            )
            if scheduled <= utc_now():
                raise ValueError("Reminder date/time must be in the future.")
            clean_title = normalize_title(title)
        except ValueError as exc:
            logger.info("Reminder validation failed kind=personal user=%s reason=%s", interaction.user.id, type(exc).__name__)
            await self.send_private(interaction, str(exc))
            return
        draft = {
            "reminder_type": "personal",
            "guild_id": int(interaction.guild_id or 0),
            "creator_user_id": interaction.user.id,
            "target_user_id": target.id,
            "title": clean_title,
            "description": sanitize_text(details),
            "scheduled_at_utc": scheduled,
            "interpretation_timezone": user_tz.key,
            "destination": destination,
            "default_offsets": (0,),
            "recurrence_type": recurrence_type,
            "recurrence_interval": interval,
            "recurrence_end_count": recurrence_count,
            "recurrence_end_at_utc": recurrence_end_at,
        }
        await self.send_private(
            interaction,
            embed=self.preview_embed(draft),
            view=CreationPreviewView(self, interaction.user.id, draft),
        )

    async def preview_event(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        details: str,
        when: str,
        timings: str,
        recurrence: str,
        public_channel: Any,
        destination: Any,
        host: Any = None,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_remind_command_access(interaction, "event"):
            return
        destination_guild_id = getattr(destination, "guild_id", None) or getattr(getattr(destination, "guild", None), "id", None)
        if str(destination_guild_id) != str(interaction.guild_id):
            await self.send_private(interaction, "Choose a destination in this server.")
            return
        permission_error = send_permission_error(self.bot, public_channel)
        if permission_error:
            await self.send_private(interaction, permission_error)
            return
        try:
            user_tz = await self.user_timezone(interaction.guild_id, interaction.user.id)
            scheduled = parse_local_datetime(when, user_tz)
            offsets = parse_offsets(timings, DEFAULT_EVENT_OFFSETS)
            recurrence_type, interval, recurrence_count, recurrence_end_at = parse_recurrence(
                recurrence,
                None,
                user_tz,
            )
            if scheduled <= utc_now():
                raise ValueError("Event date/time must be in the future.")
            # Validate the recurrence eagerly and reject impossible rules.
            recurrence_dates(scheduled, recurrence_type, interval=interval, count=recurrence_count)
            clean_title = normalize_title(title)
        except ValueError as exc:
            logger.info("Reminder validation failed kind=event user=%s reason=%s", interaction.user.id, type(exc).__name__)
            await self.send_private(interaction, str(exc))
            return
        draft = {
            "reminder_type": "event",
            "guild_id": int(interaction.guild_id or 0),
            "creator_user_id": interaction.user.id,
            "host_user_id": getattr(host, "id", None) or interaction.user.id,
            "title": clean_title,
            "description": sanitize_text(details),
            "scheduled_at_utc": scheduled,
            "interpretation_timezone": user_tz.key,
            "destination": destination,
            "public_channel": public_channel,
            "default_offsets": offsets,
            "allow_custom_timing": True,
            "close_subscriptions_at_start": True,
            "keep_public_card": True,
            "auto_subscribe_creator": env_bool("REMINDER_EVENT_AUTO_SUBSCRIBE_CREATOR", True),
            "recurrence_type": recurrence_type,
            "recurrence_interval": interval,
            "recurrence_end_count": recurrence_count,
            "recurrence_end_at_utc": recurrence_end_at,
        }
        await self.send_private(
            interaction,
            embed=self.preview_embed(draft),
            view=EventCreationPreviewView(self, interaction.user.id, draft),
        )

    async def confirm_creation(self, interaction: discord.Interaction, draft: dict[str, Any]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        command = "event" if draft.get("reminder_type") == "event" else "personal"
        if not await self.ensure_remind_command_access(interaction, command):
            return
        destination = draft.get("destination")
        try:
            row = await self.service.create_reminder(
                reminder_type=draft["reminder_type"],
                guild_id=draft["guild_id"],
                creator_user_id=draft["creator_user_id"],
                host_user_id=draft.get("host_user_id"),
                target_user_id=draft.get("target_user_id"),
                title=draft["title"],
                description=draft["description"],
                scheduled_at_utc=draft["scheduled_at_utc"],
                interpretation_timezone=draft["interpretation_timezone"],
                destination_channel_id=getattr(destination, "id", None),
                destination_channel_name=str(getattr(destination, "name", "")),
                public_channel_id=getattr(draft.get("public_channel"), "id", None),
                default_offsets=draft["default_offsets"],
                allow_custom_timing=draft.get("allow_custom_timing", True),
                close_subscriptions_at_start=draft.get("close_subscriptions_at_start", True),
                keep_public_card=draft.get("keep_public_card", True),
                auto_subscribe_creator=draft.get("auto_subscribe_creator", False),
                recurrence_type=draft["recurrence_type"],
                recurrence_interval=draft["recurrence_interval"],
                recurrence_end_count=draft.get("recurrence_end_count"),
                recurrence_end_at_utc=draft.get("recurrence_end_at_utc"),
            )
        except Exception as exc:
            if isinstance(exc, ValueError):
                await interaction.followup.send(str(exc), ephemeral=True)
            else:
                logger.exception("Reminder creation failed kind=%s", draft.get("reminder_type"))
                await interaction.followup.send(embed=error_embed("Reminder Not Saved", "The reminder could not be saved. Try again later."), ephemeral=True)
            return
        if row["reminder_type"] == "event":
            public_channel = draft["public_channel"]
            try:
                message = await public_channel.send(
                    embed=self.event_embed(row),
                    view=self.event_view(row),
                    allowed_mentions=SAFE_MENTIONS,
                )
            except discord.HTTPException as exc:
                logger.warning("Event publish failed reminder_id=%s error=%s", row["id"], type(exc).__name__)
                await self.service.cancel_reminder(int(row["id"]), interaction.user.id, reason="Public event card could not be posted", staff=True)
                await interaction.followup.send("The event could not be posted. Check the bot's channel permissions and try again.", ephemeral=True)
                return
            await self.service.set_public_message(int(row["id"]), int(public_channel.id), int(message.id))
            row = await self.service.get_reminder(int(row["id"])) or row
            creator_dm_warning = ""
            if row.get("auto_subscribe_creator"):
                subscription = await self.subscription_detail_for_event(int(row["id"]), interaction.user.id)
                if subscription:
                    dm_ok = await self.send_subscription_confirmation(interaction.user, subscription, send_dm=True)
                    if not dm_ok:
                        await self.bot.db.execute(
                            "UPDATE reminder_subscriptions SET status = 'delivery_unavailable', failure_reason = 'dm_privacy', updated_at_utc = ? WHERE id = ?",
                            (utc_text(), subscription["id"]),
                        )
                        await self.bot.db.execute(
                            "UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ? WHERE subscription_id = ? AND status IN ('pending', 'retry')",
                            (utc_text(), subscription["id"]),
                        )
                        await self.bot.db.commit()
                        creator_dm_warning = "\n⚠️ I could not DM your auto-subscription. Enable server DMs and select **Remind Me** to retry."
            await interaction.followup.send(
                f"✅ **{row['title']}** was posted for {discord_timestamp(row['scheduled_at_utc'], 'F')}.{creator_dm_warning}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=self.personal_confirmation_embed(row), ephemeral=True)
        logger.info("Reminder created reminder_id=%s type=%s guild=%s creator=%s", row["id"], row["reminder_type"], row["guild_id"], row["creator_user_id"])

    @remind.command(name="personal", description="Create a private reminder for yourself")
    @app_commands.describe(
        destination="Optional staff-only server channel delivery",
        who="Optional staff-only member target; defaults to you",
    )
    @app_commands.guild_only()
    async def personal(
        self,
        interaction: discord.Interaction,
        destination: Optional[discord.TextChannel] = None,
        who: Optional[discord.Member] = None,
    ) -> None:
        if not await self.ensure_remind_command_access(interaction, "personal"):
            return
        target = who or interaction.user
        if not self.can_target_user(interaction, target):
            await self.send_private(interaction, "You may only create personal reminders for yourself.")
            return
        if destination is not None:
            if not await self.ensure_staff_access(interaction):
                return
            if destination.guild.id != interaction.guild_id:
                await self.send_private(interaction, "Choose a channel in this server.")
                return
            if error := send_permission_error(self.bot, destination):
                await self.send_private(interaction, error)
                return
        await interaction.response.send_modal(PersonalReminderModal(self, destination, target))

    @remind.command(name="event", description="Create a public event members can subscribe to")
    @app_commands.guild_only()
    @app_commands.describe(host="Optional event host; defaults to you")
    async def event(self, interaction: discord.Interaction, host: Optional[discord.Member] = None) -> None:
        if not await self.ensure_remind_command_access(interaction, "event"):
            return
        channel = interaction.channel
        if channel is None or not is_sendable_channel(channel):
            await self.send_private(interaction, "Use this command in a server channel where the bot can post.")
            return
        if error := send_permission_error(self.bot, channel):
            await self.send_private(interaction, error)
            return
        await interaction.response.send_message(
            "Use the current channel as the event destination, or choose a text, voice, or Stage channel.",
            view=EventStartView(self, interaction.user.id, channel, host or interaction.user),
            ephemeral=True,
        )

    @remind.command(name="manage", description="Privately manage reminders you created")
    @app_commands.describe(
        status_filter="Show upcoming, completed, cancelled, or all reminders",
        reminder_type="Show personal reminders, events, or both",
        recurrence="Show one-time, recurring, or both",
    )
    @app_commands.choices(status_filter=[
        app_commands.Choice(name="Upcoming", value="upcoming"),
        app_commands.Choice(name="Completed", value="completed"),
        app_commands.Choice(name="Cancelled", value="cancelled"),
        app_commands.Choice(name="All", value="all"),
    ])
    @app_commands.choices(reminder_type=[
        app_commands.Choice(name="All types", value="all"),
        app_commands.Choice(name="Personal", value="personal"),
        app_commands.Choice(name="Events", value="event"),
    ])
    @app_commands.choices(recurrence=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="One-time", value="one_time"),
        app_commands.Choice(name="Recurring", value="recurring"),
    ])
    @app_commands.guild_only()
    async def manage(
        self,
        interaction: discord.Interaction,
        status_filter: Optional[app_commands.Choice[str]] = None,
        reminder_type: Optional[app_commands.Choice[str]] = None,
        recurrence: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_remind_command_access(interaction, "manage"):
            return
        staff = self.has_manage_all_access(interaction)
        rows = await self.service.list_reminders(
            int(interaction.guild_id or 0),
            interaction.user.id,
            staff=staff,
            status=(status_filter.value if status_filter else "upcoming"),
            reminder_type=(reminder_type.value if reminder_type else "all"),
            recurrence=(recurrence.value if recurrence else "all"),
        )
        if not rows:
            await self.send_private(interaction, "No matching reminders were found.")
            return
        await self.send_private(
            interaction,
            embed=self.reminder_detail_embed(rows[0]),
            view=ReminderManageView(self, interaction.user.id, rows, staff),
        )

    @remind.command(name="subscriptions", description="View and manage your active event subscriptions")
    @app_commands.guild_only()
    async def subscriptions(self, interaction: discord.Interaction) -> None:
        await self.defer_private(interaction, thinking=True)
        if not await self.ensure_remind_command_access(interaction, "subscriptions"):
            return
        rows = await self.service.list_subscriptions(int(interaction.guild_id or 0), interaction.user.id)
        if not rows:
            await self.send_private(interaction, "You have no active event subscriptions.")
            return
        await self.send_private(
            interaction,
            embed=self.subscription_embed(rows[0]),
            view=SubscriptionListView(self, interaction.user.id, rows),
        )

    @remind.command(name="help", description="Explain personal reminders, events, and subscriptions")
    @app_commands.guild_only()
    async def remind_help(self, interaction: discord.Interaction) -> None:
        embed = branded_embed(
            "Reminder Help",
            description=(
                "`/remind personal` creates a private one-time or recurring reminder.\n\n"
                "`/remind event` lets members with a configured event role post an event with a **Remind Me** button. "
                "Members receive private DMs at the selected times.\n\n"
                "`/remind manage` edits, duplicates, or cancels reminders you created. Configured manager roles can manage guild reminders.\n\n"
                "`/remind subscriptions` changes timing or unsubscribes from events.\n\n"
                "Natural-language dates use your `/timezone` setting and are always previewed before creation."
            ),
        )
        await self.send_private(interaction, embed=embed)

    @app_commands.command(name="timezone", description="View or set your timezone for reminder and time tools")
    @app_commands.describe(timezone="Optional IANA timezone, such as America/New_York")
    @app_commands.guild_only()
    async def timezone_command(self, interaction: discord.Interaction, timezone: Optional[str] = None) -> None:
        if timezone is None:
            name = await self.user_timezone_name(interaction.guild_id, interaction.user.id)
            await self.send_private(interaction, f"Your reminder timezone is `{name}`. Run `/timezone` with a timezone to change it.")
            return
        name = timezone.strip()
        try:
            ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            await self.send_private(interaction, "I could not find that timezone. Try `America/New_York` or `Europe/London`.")
            return
        await self.save_user_timezone(int(interaction.guild_id or 0), interaction.user.id, name)
        await self.send_private(interaction, f"✅ Your timezone is now `{name}`.")

    @timezone_command.autocomplete("timezone")
    async def timezone_autocomplete(self, _interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        query = current.strip().replace(" ", "_").casefold()
        matches = [name for name in sorted(available_timezones()) if not query or query in name.casefold()]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    @app_commands.command(name="time", description="Create copyable Discord time codes")
    @app_commands.describe(when="Time such as tomorrow at 9am or Friday at 7:30pm")
    @app_commands.guild_only()
    async def time_command(self, interaction: discord.Interaction, when: str) -> None:
        tz = await self.user_timezone(interaction.guild_id, interaction.user.id)
        try:
            parsed = parse_local_datetime(when, tz)
        except ValueError as exc:
            await self.send_private(interaction, str(exc))
            return
        await self.send_private(interaction, embed=timestamp_codes_embed(parsed, tz.key))

    @commands.command(name="time", description="Create public Discord time codes")
    async def time_prefix(self, ctx: commands.Context, *, when: str = "") -> None:
        if not self.member_has_staff_access(ctx.guild, ctx.author):
            await ctx.send("Time tools are limited to configured staff.")
            return
        if not when.strip():
            await ctx.send("Try `!time tomorrow at 9am`.")
            return
        tz = await self.user_timezone(ctx.guild.id, ctx.author.id)
        try:
            parsed = parse_local_datetime(when, tz)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(embed=timestamp_codes_embed(parsed, tz.key))

    async def subscription_detail(self, subscription_id: int, user_id: int) -> Optional[dict[str, Any]]:
        return await self.service.fetch_one(
            """
            SELECT s.*, r.title, r.description, r.scheduled_at_utc,
                   r.destination_channel_id, r.destination_channel_name,
                   r.default_offsets_json, r.allow_custom_timing,
                   r.status AS event_status, r.guild_id, r.creator_user_id,
                   r.host_user_id
            FROM reminder_subscriptions s
            JOIN reminder_items r ON r.id = s.reminder_id
            WHERE s.id = ? AND s.user_id = ?
            """,
            (subscription_id, str(user_id)),
        )

    async def subscription_detail_for_event(self, reminder_id: int, user_id: int) -> Optional[dict[str, Any]]:
        row = await self.service.fetch_one(
            "SELECT id FROM reminder_subscriptions WHERE reminder_id = ? AND user_id = ?",
            (reminder_id, str(user_id)),
        )
        return await self.subscription_detail(int(row["id"]), user_id) if row else None

    async def send_subscription_confirmation(self, user: Any, row: dict[str, Any], *, send_dm: bool) -> bool:
        if not send_dm:
            return True
        try:
            await user.send(
                embed=self.subscription_embed(row),
                view=self.dm_subscription_view(row, int(row["id"])),
                allowed_mentions=SAFE_MENTIONS,
            )
            return True
        except discord.Forbidden:
            return False
        except discord.HTTPException:
            logger.warning("Subscription confirmation temporary DM failure subscription_id=%s", row["id"])
            return False

    async def handle_event_join(self, interaction: discord.Interaction, reminder_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.ensure_remind_command_access(interaction, "subscriptions"):
            return
        event = await self.service.get_reminder(reminder_id)
        if event is None:
            await interaction.followup.send("This event reminder no longer exists.", ephemeral=True)
            return
        if interaction.guild_id is None or str(event["guild_id"]) != str(interaction.guild_id):
            await interaction.followup.send("This event reminder does not belong to this server.", ephemeral=True)
            return
        try:
            subscription, created = await self.service.subscribe(reminder_id, interaction.user.id)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        row = await self.subscription_detail(int(subscription["id"]), interaction.user.id)
        if row is None:
            await interaction.followup.send("The subscription could not be loaded.", ephemeral=True)
            return
        dm_ok = True
        if created:
            dm_ok = await self.send_subscription_confirmation(interaction.user, row, send_dm=True)
        if not dm_ok:
            await self.bot.db.execute(
                """
                UPDATE reminder_subscriptions
                SET status = 'delivery_unavailable', failure_reason = 'dm_privacy', updated_at_utc = ?
                WHERE id = ?
                """,
                (utc_text(), subscription["id"]),
            )
            await self.bot.db.execute(
                "UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ? WHERE subscription_id = ? AND status IN ('pending', 'retry')",
                (utc_text(), subscription["id"]),
            )
            await self.service.audit(
                "delivery_failed",
                reminder_id=reminder_id,
                subscription_id=int(subscription["id"]),
                guild_id=interaction.guild_id,
                actor_user_id=interaction.user.id,
                metadata={"category": "dm_privacy", "phase": "confirmation"},
                commit=False,
            )
            await self.bot.db.commit()
            await interaction.followup.send(
                "⚠️ I could not DM you, so reminder delivery is unavailable. Enable direct messages from server members, then select **Remind Me** again to retry.",
                ephemeral=True,
            )
        else:
            prefix = "✅ You're subscribed." if created else "🔔 You're already subscribed."
            await interaction.followup.send(
                prefix,
                embed=self.subscription_embed(row),
                view=SubscriptionControlsView(self, interaction.user.id, int(subscription["id"]), row),
                ephemeral=True,
            )
        self.schedule_public_refresh(reminder_id)

    async def change_subscription_timing(self, interaction: discord.Interaction, subscription_id: int, offsets: Optional[Sequence[int]]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.update_subscription_offsets(subscription_id, interaction.user.id, offsets)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        row = await self.subscription_detail(subscription_id, interaction.user.id)
        await interaction.followup.send(
            "✅ Reminder timing updated." if offsets is not None else "✅ Event defaults restored.",
            embed=self.subscription_embed(row),
            ephemeral=True,
        )

    async def unsubscribe_interaction(self, interaction: discord.Interaction, subscription_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.subscription_detail(subscription_id, interaction.user.id)
        changed = await self.service.unsubscribe(subscription_id, interaction.user.id)
        if not changed:
            await interaction.followup.send("That subscription was already cancelled or completed.", ephemeral=True)
            return
        await interaction.followup.send("🔕 You are unsubscribed. No future reminders will be sent for this event.", ephemeral=True)
        if row:
            self.schedule_public_refresh(int(row["reminder_id"]))

    def schedule_public_refresh(self, reminder_id: int) -> None:
        existing = self._refresh_tasks.get(reminder_id)
        if existing and not existing.done():
            return
        self._refresh_tasks[reminder_id] = asyncio.create_task(self._delayed_public_refresh(reminder_id))

    async def _delayed_public_refresh(self, reminder_id: int) -> None:
        try:
            await asyncio.sleep(3)
            await self.refresh_public_card(reminder_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Public reminder refresh failed reminder_id=%s", reminder_id)
        finally:
            self._refresh_tasks.pop(reminder_id, None)

    async def refresh_public_card(self, reminder_id: int) -> bool:
        row = await self.service.get_reminder(reminder_id)
        if row is None or not row.get("public_channel_id") or not row.get("public_message_id"):
            return False
        channel = self.bot.get_channel(int(row["public_channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(row["public_channel_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if channel is None or not hasattr(channel, "fetch_message"):
            logger.warning("Public reminder channel missing reminder_id=%s", reminder_id)
            return False
        try:
            message = await channel.fetch_message(int(row["public_message_id"]))
            if row["status"] == "completed" and not row["keep_public_card"]:
                await message.delete()
                return True
            await message.edit(
                embed=self.event_embed(row),
                view=self.event_view(row, disabled=row["status"] != "upcoming"),
                allowed_mentions=SAFE_MENTIONS,
            )
            return True
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Public reminder message update failed reminder_id=%s error=%s", reminder_id, type(exc).__name__)
            return False

    async def apply_edit(self, interaction: discord.Interaction, reminder_id: int, *, title: str, details: str, when: str, timings: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.service.get_reminder(reminder_id)
        if row is None:
            await interaction.followup.send("That reminder was not found.", ephemeral=True)
            return
        new_time = None
        if not when.strip().startswith("<t:"):
            try:
                tz = ZoneInfo(row["interpretation_timezone"])
            except (ValueError, ZoneInfoNotFoundError):
                tz = reminder_timezone()
            try:
                new_time = parse_local_datetime(when, tz)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
        try:
            updated, changes = await self.service.update_reminder(
                reminder_id,
                interaction.user.id,
                staff=self.has_staff_access(interaction),
                title=title,
                description=details,
                scheduled_at_utc=new_time,
                default_offsets=parse_offsets(timings) if row["reminder_type"] == "event" else None,
            )
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if row["reminder_type"] == "event":
            await self.refresh_public_card(reminder_id)
            meaningful = {"title", "scheduled_at_utc", "destination"} & set(changes)
            if meaningful:
                await self.notify_event_update(updated, changes)
        await interaction.followup.send("✅ Reminder updated.", embed=self.reminder_detail_embed(updated), ephemeral=True)

    async def notify_event_update(self, row: dict[str, Any], changes: dict[str, tuple[Any, Any]]) -> None:
        subscribers = await self.service.fetch_all(
            "SELECT user_id, id FROM reminder_subscriptions WHERE reminder_id = ? AND status = 'active'",
            (row["id"],),
        )
        lines = []
        if "title" in changes:
            lines.append(f"**Title:** {changes['title'][0]} → {changes['title'][1]}")
        if "scheduled_at_utc" in changes:
            lines.append(f"**When:** {discord_timestamp(changes['scheduled_at_utc'][0], 'f')} → {discord_timestamp(changes['scheduled_at_utc'][1], 'f')}")
        if "destination" in changes:
            lines.append(f"**Where:** {channel_mention(changes['destination'][0])} → {channel_mention(changes['destination'][1])}")
        embed = branded_embed("📝 Event Updated", description="\n".join(lines))
        embed.add_field(name="Event", value=row["title"], inline=False)
        for subscriber in subscribers:
            user = self.bot.get_user(int(subscriber["user_id"]))
            if user is None:
                try:
                    user = await self.bot.fetch_user(int(subscriber["user_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            try:
                await user.send(embed=embed, view=self.dm_subscription_view(row, int(subscriber["id"])), allowed_mentions=SAFE_MENTIONS)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Event update DM failed reminder_id=%s user_id=%s", row["id"], subscriber["user_id"])

    async def cancel_interaction(self, interaction: discord.Interaction, reminder_id: int) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.service.get_reminder(reminder_id)
        if row is None:
            await interaction.followup.send("That upcoming reminder was not found.", ephemeral=True)
            return
        subscriber_rows = await self.service.fetch_all(
            "SELECT id, user_id FROM reminder_subscriptions WHERE reminder_id = ? AND status = 'active'",
            (reminder_id,),
        ) if row["reminder_type"] == "event" else []
        try:
            cancelled = await self.service.cancel_reminder(
                reminder_id,
                interaction.user.id,
                staff=self.has_staff_access(interaction),
            )
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if cancelled is None:
            await interaction.followup.send("That upcoming reminder was not found.", ephemeral=True)
            return
        if row["reminder_type"] == "event":
            await self.refresh_public_card(reminder_id)
            embed = branded_embed("❌ Event Cancelled", description=f"**{row['title']}** has been cancelled by <@{interaction.user.id}>.\n\nNo further reminders will be sent.")
            embed.add_field(name="Original time", value=discord_timestamp(row["scheduled_at_utc"], "F"), inline=False)
            for subscriber in subscriber_rows:
                user = self.bot.get_user(int(subscriber["user_id"]))
                if user is None:
                    try:
                        user = await self.bot.fetch_user(int(subscriber["user_id"]))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                try:
                    await user.send(embed=embed, allowed_mentions=SAFE_MENTIONS)
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("Cancellation DM failed reminder_id=%s user_id=%s", reminder_id, subscriber["user_id"])
        await interaction.followup.send("❌ Reminder cancelled. Its history and delivery records were preserved.", ephemeral=True)

    @legacy_reminder.command(name="add", description="Legacy alias for /remind personal")
    @app_commands.describe(channel="Channel where the staff reminder should post", who="Optional member to ping")
    @app_commands.guild_only()
    async def legacy_add(self, interaction: discord.Interaction, channel: discord.TextChannel, who: Optional[discord.Member] = None) -> None:
        if not LEGACY_ENABLED:
            await self.send_private(interaction, "This command has been removed. Use `/remind personal`.")
            return
        if not await self.ensure_remind_command_access(interaction, "personal"):
            return
        if not await self.ensure_staff_access(interaction):
            return
        await interaction.response.send_message(
            "This command has moved to `/remind personal`.",
            view=LegacyStartView(self, interaction.user.id, "personal", destination=channel, target=who or interaction.user),
            ephemeral=True,
        )

    @legacy_reminder.command(name="manage", description="Legacy alias for /remind manage")
    @app_commands.guild_only()
    async def legacy_manage(self, interaction: discord.Interaction) -> None:
        if not LEGACY_ENABLED:
            await self.send_private(interaction, "This command has been removed. Use `/remind manage`.")
            return
        if not await self.ensure_remind_command_access(interaction, "manage"):
            return
        await self.send_private(
            interaction,
            "This command has moved to `/remind manage`. Run the new command to open the unified panel.",
        )

    @remind.command(name="subscribe", description="Legacy alias for /remind event")
    @app_commands.guild_only()
    async def legacy_subscribe(self, interaction: discord.Interaction) -> None:
        if not LEGACY_ENABLED:
            await self.send_private(interaction, "This command has been removed. Use `/remind event`.")
            return
        if not await self.ensure_remind_command_access(interaction, "event"):
            return
        channel = interaction.channel
        if channel is None or not is_sendable_channel(channel):
            await self.send_private(interaction, "Use this command in a server channel where the bot can post.")
            return
        await interaction.response.send_message(
            "This command has moved to `/remind event`.",
            view=LegacyStartView(self, interaction.user.id, "event", public_channel=channel, destination=channel),
            ephemeral=True,
        )

    # Compatibility service adapters. They preserve integrations while keeping
    # all writes in the canonical service.
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
        title, _, details = message.strip().partition("\n")
        return await self.service.create_reminder(
            reminder_type="personal",
            guild_id=guild_id,
            creator_user_id=creator_user_id,
            target_user_id=target_user_id,
            title=re.sub(r"^#{1,6}\s*", "", title)[:100],
            description=details,
            scheduled_at_utc=scheduled_at_utc,
            interpretation_timezone=configured_timezone_name(),
            destination_channel_id=channel_id,
            default_offsets=(0,),
        )

    async def soft_delete_reminder(self, guild_id: Optional[int], user_id: int, reminder_id: int) -> bool:
        row = await self.service.get_reminder(reminder_id)
        if row is None or str(row["guild_id"]) != str(guild_id):
            return False
        try:
            cancelled = await self.service.cancel_reminder(reminder_id, user_id)
        except PermissionError:
            return False
        if cancelled is None:
            return False
        await self.bot.db.execute(
            "UPDATE reminder_items SET status = 'deleted', updated_at_utc = ? WHERE id = ?",
            (utc_text(), reminder_id),
        )
        await self.bot.db.commit()
        return True

    async def create_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        channel: Any,
        target: Optional[discord.Member],
        message: str,
        date_time: str,
    ) -> None:
        title, _, details = message.strip().partition("\n")
        await self.preview_personal(
            interaction,
            title=re.sub(r"^#{1,6}\s*", "", title),
            details=details,
            when=date_time,
            recurrence="none",
            count="",
            destination=channel,
            target=target or interaction.user,
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
        title, _, details = message.strip().partition("\n")
        await self.preview_event(
            interaction,
            title=re.sub(r"^#{1,6}\s*", "", title),
            details=details,
            when=date_time,
            timings="start",
            recurrence="none",
            public_channel=channel,
            destination=destination,
        )

    @staticmethod
    def subscription_view(post_id: int, *, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Remind Me", emoji="🔔", style=discord.ButtonStyle.primary, custom_id=f"remindsubscribe|join|{post_id}", disabled=disabled))
        return view

    @staticmethod
    def subscription_cancel_view(subscriber_id: int, *, disabled: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Cancel Reminder", emoji="🔕", style=discord.ButtonStyle.secondary, custom_id=f"remindsubscribe|cancel|{subscriber_id}", disabled=disabled))
        return view

    async def handle_subscription_join(self, interaction: discord.Interaction, post_id: int) -> None:
        row = await self.service.fetch_one(
            "SELECT id FROM reminder_items WHERE legacy_source = 'reminder_subscription_posts' AND legacy_id = ?",
            (str(post_id),),
        )
        await self.handle_event_join(interaction, int(row["id"]) if row else post_id)

    async def handle_subscription_cancel(self, interaction: discord.Interaction, subscriber_id: int) -> None:
        row = await self.service.fetch_one(
            "SELECT id FROM reminder_subscriptions WHERE legacy_subscriber_id = ?",
            (str(subscriber_id),),
        )
        await self.unsubscribe_interaction(interaction, int(row["id"]) if row else subscriber_id)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        legacy = custom_id.split("|")
        if len(legacy) == 3 and legacy[0] == "remindsubscribe" and legacy[2].isdigit():
            if legacy[1] == "join":
                await self.handle_subscription_join(interaction, int(legacy[2]))
            elif legacy[1] == "cancel":
                await self.handle_subscription_cancel(interaction, int(legacy[2]))
            return
        parts = custom_id.split(":")
        if len(parts) != 5 or parts[:2] != ["broeden", "remind"] or not parts[4].isdigit():
            return
        record_id = int(parts[4])
        if parts[2] == "event" and parts[3] == "join":
            await self.handle_event_join(interaction, record_id)
        elif parts[2] == "sub" and parts[3] == "cancel":
            await self.unsubscribe_interaction(interaction, record_id)
        elif parts[2] == "sub" and parts[3] == "timing":
            row = await self.subscription_detail(record_id, interaction.user.id)
            if row is None:
                await interaction.response.send_message("That active subscription was not found.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "Choose one or more private DM timings.",
                    view=SubscriptionControlsView(self, interaction.user.id, record_id, row, include_select=True),
                    ephemeral=True,
                )

    @tasks.loop(seconds=REMINDER_CHECK_SECONDS)
    async def reminder_scheduler(self) -> None:
        await self.process_dashboard_actions()
        await self.send_due_deliveries()

    @reminder_scheduler.before_loop
    async def before_reminder_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def send_due_deliveries(self) -> None:
        if self._scheduler_lock is None:
            self._scheduler_lock = asyncio.Lock()
        if self._scheduler_lock.locked():
            return
        async with self._scheduler_lock:
            due_events = await self.service.fetch_all(
                """
                SELECT DISTINCT r.id FROM reminder_items r
                JOIN reminder_occurrences o ON o.reminder_id = r.id
                WHERE r.reminder_type = 'event' AND r.status = 'upcoming'
                  AND o.status = 'upcoming' AND o.scheduled_at_utc <= ?
                """,
                (utc_text(),),
            )
            for row in await self.service.claim_due_deliveries(limit=25):
                await self.send_one_delivery(row)
            await self.service.complete_finished_occurrences()
            for event in due_events:
                self.schedule_public_refresh(int(event["id"]))

    async def process_dashboard_actions(self) -> None:
        actions = await self.service.fetch_all(
            "SELECT * FROM reminder_dashboard_actions WHERE status = 'pending' ORDER BY requested_at_utc, id LIMIT 10"
        )
        for action in actions:
            cursor = await self.bot.db.execute(
                "UPDATE reminder_dashboard_actions SET status = 'processing' WHERE id = ? AND status = 'pending'",
                (action["id"],),
            )
            claimed = bool(cursor.rowcount)
            await cursor.close()
            await self.bot.db.commit()
            if not claimed:
                continue
            try:
                payload = __import__("json").loads(action["payload_json"] or "{}")
                reminder = await self.service.get_reminder(int(action["reminder_id"]))
                if reminder is None:
                    raise ValueError("Reminder no longer exists.")
                if action["action"] == "cancel":
                    result = await self.service.cancel_reminder(
                        int(reminder["id"]),
                        int(reminder["creator_user_id"]),
                        reason=str(payload.get("reason", "Cancelled from dashboard")),
                        staff=True,
                    )
                    if result is None:
                        raise ValueError("Reminder is no longer upcoming.")
                    await self.refresh_public_card(int(reminder["id"]))
                elif action["action"] == "duplicate":
                    duplicate = await self.service.duplicate_reminder(
                        int(reminder["id"]),
                        int(reminder["creator_user_id"]),
                        staff=True,
                    )
                    if duplicate["reminder_type"] == "event" and not await self.publish_event_card(duplicate):
                        await self.service.cancel_reminder(
                            int(duplicate["id"]),
                            int(reminder["creator_user_id"]),
                            reason="Dashboard duplicate event card could not be posted",
                            staff=True,
                        )
                        raise RuntimeError("Duplicate event card could not be posted.")
                elif action["action"] == "edit":
                    scheduled = parse_utc(payload["scheduled_at_utc"]) if payload.get("scheduled_at_utc") else None
                    destination_id = (
                        int(payload["destination_channel_id"])
                        if str(payload.get("destination_channel_id", "")).isdigit()
                        else None
                    )
                    if destination_id is not None:
                        destination = self.bot.get_channel(destination_id)
                        if destination is None:
                            try:
                                destination = await self.bot.fetch_channel(destination_id)
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                destination = None
                        destination_guild = getattr(destination, "guild_id", None) or getattr(getattr(destination, "guild", None), "id", None)
                        if destination is None or str(destination_guild) != str(reminder["guild_id"]):
                            raise ValueError("Destination channel is unavailable or outside the reminder guild.")
                        if reminder["reminder_type"] == "personal":
                            permission_error = send_permission_error(self.bot, destination)
                            if not is_sendable_channel(destination) or permission_error:
                                raise ValueError(permission_error or "Personal reminder destination cannot receive messages.")
                    updated, changes = await self.service.update_reminder(
                        int(reminder["id"]),
                        int(reminder["creator_user_id"]),
                        staff=True,
                        title=payload.get("title"),
                        description=payload.get("description"),
                        scheduled_at_utc=scheduled,
                        destination_channel_id=destination_id,
                        destination_channel_name=payload.get("destination_channel_name"),
                        clear_destination=(
                            "destination_channel_id" in payload
                            and not str(payload.get("destination_channel_id", "")).strip()
                            and bool(reminder.get("destination_channel_id"))
                        ),
                        default_offsets=parse_offsets(payload["timings"])
                        if payload.get("timings") else None,
                    )
                    await self.refresh_public_card(int(reminder["id"]))
                    if {"title", "scheduled_at_utc", "destination"} & set(changes):
                        await self.notify_event_update(updated, changes)
                elif action["action"] == "retry":
                    delivery_id = int(payload.get("delivery_id", 0))
                    if not await self.service.retry_failed_delivery(
                        int(reminder["id"]),
                        delivery_id,
                        action["requested_by"],
                    ):
                        raise ValueError("Delivery is not eligible for retry.")
                elif action["action"] == "archive":
                    changed = await self.service.archive_reminder(
                        int(reminder["id"]),
                        int(reminder["creator_user_id"]),
                        staff=True,
                    )
                    if not changed:
                        raise ValueError("Reminder is already archived.")
                await self.service.audit(
                    f"dashboard_{action['action']}",
                    reminder_id=int(reminder["id"]),
                    guild_id=reminder["guild_id"],
                    actor_user_id=action["requested_by"],
                    metadata={"dashboard_action_id": action["id"]},
                    commit=False,
                )
            except Exception as exc:
                logger.exception("Reminder dashboard action failed action_id=%s", action["id"])
                await self.bot.db.execute(
                    "UPDATE reminder_dashboard_actions SET status = 'failed', payload_json = '{}', failure_reason = ?, processed_at_utc = ? WHERE id = ?",
                    (truncate(str(exc), 500), utc_text(), action["id"]),
                )
            else:
                await self.bot.db.execute(
                    "UPDATE reminder_dashboard_actions SET status = 'completed', payload_json = '{}', processed_at_utc = ?, failure_reason = NULL WHERE id = ?",
                    (utc_text(), action["id"]),
                )
            await self.bot.db.commit()

    async def send_due_reminders(self) -> None:
        await self.send_due_deliveries()

    async def send_due_subscription_reminders(self) -> None:
        await self.send_due_deliveries()

    async def send_one_delivery(self, row: dict[str, Any]) -> None:
        delivery_id = int(row["id"])
        # Cancellation may race with a claim; re-check canonical state immediately before I/O.
        current = await self.service.fetch_one(
            """
            SELECT d.status, r.status AS reminder_status, o.status AS occurrence_status,
                   s.status AS subscription_status
            FROM reminder_deliveries d
            JOIN reminder_occurrences o ON o.id = d.occurrence_id
            JOIN reminder_items r ON r.id = o.reminder_id
            LEFT JOIN reminder_subscriptions s ON s.id = d.subscription_id
            WHERE d.id = ?
            """,
            (delivery_id,),
        )
        if current is None or current["status"] != "claimed" or current["reminder_status"] != "upcoming" or current["occurrence_status"] != "upcoming" or (row.get("subscription_id") and current["subscription_status"] != "active"):
            await self.bot.db.execute("UPDATE reminder_deliveries SET status = 'cancelled', updated_at_utc = ? WHERE id = ? AND status = 'claimed'", (utc_text(), delivery_id))
            await self.bot.db.commit()
            return
        try:
            if row["delivery_mode"] == "channel":
                channel_id = row.get("destination_channel_id") or row.get("reminder_channel_id")
                channel = self.bot.get_channel(int(channel_id)) if channel_id else None
                if channel is None and channel_id:
                    channel = await self.bot.fetch_channel(int(channel_id))
                if channel is None or not hasattr(channel, "send"):
                    await self.service.mark_delivery_failed(delivery_id, "deleted_channel", "Destination channel is unavailable", permanent=True)
                    return
                embed = branded_embed("🔔 Reminder", description=row.get("description") or None)
                embed.add_field(name="Reminder", value=row["title"], inline=False)
                await channel.send(
                    content=f"<@{row['recipient_user_id']}>",
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        roles=False,
                        everyone=False,
                        replied_user=False,
                    ),
                )
            else:
                user = self.bot.get_user(int(row["recipient_user_id"]))
                if user is None:
                    user = await self.bot.fetch_user(int(row["recipient_user_id"]))
                if user is None:
                    await self.service.mark_delivery_failed(delivery_id, "missing_user", "Recipient is unavailable", permanent=True)
                    return
                if row["reminder_type"] == "event":
                    embed = self.delivery_embed(row)
                    view = self.dm_subscription_view(row, int(row["subscription_id"])) if row.get("subscription_id") else None
                else:
                    embed = branded_embed("🔔 Reminder", description=row.get("description") or None)
                    embed.add_field(name="Reminder", value=row["title"], inline=False)
                    view = None
                await user.send(embed=embed, view=view, allowed_mentions=SAFE_MENTIONS)
        except (discord.Forbidden, discord.NotFound) as exc:
            category = "dm_privacy" if row["delivery_mode"] == "dm" else "deleted_channel"
            logger.warning("Reminder delivery permanent failure delivery_id=%s category=%s", delivery_id, category)
            await self.service.mark_delivery_failed(delivery_id, category, type(exc).__name__, permanent=True)
            return
        except discord.HTTPException as exc:
            logger.warning("Reminder delivery temporary failure delivery_id=%s status=%s", delivery_id, getattr(exc, "status", "unknown"))
            await self.service.mark_delivery_failed(delivery_id, "discord_temporary", type(exc).__name__, permanent=False)
            return
        except Exception as exc:
            logger.exception("Unexpected reminder delivery failure delivery_id=%s", delivery_id)
            await self.service.mark_delivery_failed(delivery_id, "unexpected", type(exc).__name__, permanent=False)
            return
        await self.service.mark_delivery_sent(delivery_id)
        if row["reminder_type"] == "event":
            self.schedule_public_refresh(int(row["reminder_id"]))
        logger.info("Reminder delivery sent delivery_id=%s reminder_id=%s trigger=%s", delivery_id, row["reminder_id"], row["trigger_key"])


async def setup(bot: commands.Bot) -> None:
    if not LEGACY_ENABLED:
        ReminderCog.remind.remove_command("subscribe")
    await bot.add_cog(ReminderCog(bot))
    if not LEGACY_ENABLED:
        bot.tree.remove_command("reminder", type=discord.AppCommandType.chat_input)
