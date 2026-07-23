"""Dashboard presentation and validation helpers for Discord events."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image, ImageOps, UnidentifiedImageError

from utils.events import *  # Re-export the small dashboard-safe event API.
from utils.settings import get_setting


MAX_EVENT_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_EVENT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def event_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(str(get_setting("SERVER_TIMEZONE", "America/Chicago") or "America/Chicago"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Chicago")


def normalize_event_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    if not data:
        raise ValueError("The selected artwork file is empty.")
    if len(data) > MAX_EVENT_IMAGE_BYTES:
        raise ValueError("Event artwork must be 8 MiB or smaller.")
    if str(content_type).casefold() not in ALLOWED_EVENT_IMAGE_TYPES:
        raise ValueError("Event artwork must be a JPEG, PNG, or WebP image.")
    try:
        with Image.open(BytesIO(data)) as source:
            source.verify()
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, (1600, 900), method=Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, "WEBP", quality=88, method=6)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError("Event artwork could not be read as a safe image.") from exc
    return output.getvalue(), "image/webp"


def parse_event_form(form: Any) -> dict[str, Any]:
    entity_type = str(form.get("entity_type") or "").strip().casefold()
    if entity_type not in EVENT_TYPES:
        raise ValueError("Choose Stage, Voice, or External event type.")
    name = str(form.get("name") or "").strip()
    if not 2 <= len(name) <= 100:
        raise ValueError("Event name must be between 2 and 100 characters.")
    description = str(form.get("description") or "").strip()
    if len(description) > 900:
        raise ValueError("Event description must be 900 characters or shorter.")
    start = _parse_local_datetime(str(form.get("start_time") or ""), "start")
    end_text = str(form.get("end_time") or "").strip()
    end = _parse_local_datetime(end_text, "end") if end_text else None
    if start <= datetime.now(timezone.utc):
        raise ValueError("Event start time must be in the future.")
    if end is not None and end <= start:
        raise ValueError("Event end time must be after its start time.")
    channel_id = str(form.get("channel_id") or "").strip() or None
    location = str(form.get("location") or "").strip()
    if entity_type == "external":
        if not location or end is None:
            raise ValueError("External events require a location and end time.")
        channel_id = None
    elif not channel_id or not channel_id.isdigit():
        raise ValueError("Choose an eligible Discord channel.")
    return {
        "name": name,
        "description": description,
        "entity_type": entity_type,
        "channel_id": channel_id,
        "location": location[:100],
        "scheduled_at_utc": start.isoformat(),
        "end_at_utc": end.isoformat() if end else None,
    }


def _parse_local_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Choose a valid event {label} time.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=event_timezone())
    return parsed.astimezone(timezone.utc)


def eligible_event_channels(channels: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result = {"stage": [], "voice": []}
    for channel in channels:
        channel_type = str(channel.get("type") or "").casefold()
        if channel.get("archived") or channel.get("is_thread"):
            continue
        if channel_type in {"stage", "stage_voice", "13"}:
            result["stage"].append(channel)
        elif channel_type in {"voice", "2"}:
            result["voice"].append(channel)
    for values in result.values():
        values.sort(key=lambda item: (str(item.get("parent_name") or ""), int(item.get("position") or 0)))
    return result


def decorate_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    local_zone = event_timezone()
    for event in events:
        start = datetime.fromisoformat(str(event["scheduled_at_utc"]).replace("Z", "+00:00")).astimezone(local_zone)
        event["start_local"] = start
        event["month_key"] = start.strftime("%Y-%m")
        event["month_label"] = start.strftime("%B %Y")
        event["date_label"] = f"{start.strftime('%A, %B')} {start.day}"
        event["time_label"] = start.strftime("%I:%M %p %Z").lstrip("0")
        event["effective_offsets"] = event.get("custom_offsets") or event.get("default_offsets") or [15, 0]
    return events


def calendar_month(events: list[dict[str, Any]], year: int, month: int) -> dict[str, Any]:
    by_day: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        start = event.get("start_local")
        if start and start.year == year and start.month == month:
            by_day.setdefault(start.day, []).append(event)
    weeks = []
    for week in calendar.Calendar(firstweekday=6).monthdayscalendar(year, month):
        weeks.append([{"day": day, "events": by_day.get(day, [])} for day in week])
    return {"year": year, "month": month, "label": datetime(year, month, 1).strftime("%B %Y"), "weeks": weeks}
