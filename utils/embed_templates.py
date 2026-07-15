"""Saved Discord message/embed payloads shared by the dashboard and bot."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import discord

from utils.settings import get_setting, settings_database_path
from utils.sqlite import configure_sync_connection


MAX_EMBED_TOTAL_CHARS = 6000
SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
BUTTON_STYLES = {"primary", "secondary", "success", "danger"}
BUTTON_ACTIONS = {"add_role", "remove_role", "url"}


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return configure_sync_connection(sqlite3.connect(path, timeout=30))


def initialize_embed_templates_schema() -> None:
    with closing(_connect()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS embed_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT 'dashboard'
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_embed_templates_updated "
            "ON embed_templates (updated_at DESC)"
        )
        connection.commit()


def _clean_text(value: Any, maximum: int, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum:,} characters.")
    return text


def _clean_url(value: Any, label: str) -> str:
    text = _clean_text(value, 2048, label)
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be a complete http:// or https:// URL.")
    return text


def _clean_color(value: Any) -> str:
    text = str(value or "#25b8b8").strip().lower()
    if not text.startswith("#"):
        text = f"#{text}"
    if not re.fullmatch(r"#[0-9a-f]{6}", text):
        raise ValueError("Embed color must be a six-digit hex color.")
    return text


def validate_embed_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Embed data must be a JSON object.")
    content = _clean_text(payload.get("content"), 2000, "Regular message")
    raw_embed = payload.get("embed") or {}
    if not isinstance(raw_embed, dict):
        raise ValueError("Embed data is invalid.")
    fields: list[dict[str, Any]] = []
    raw_fields = raw_embed.get("fields") or []
    if not isinstance(raw_fields, list) or len(raw_fields) > 25:
        raise ValueError("An embed can contain at most 25 fields.")
    for index, field in enumerate(raw_fields, start=1):
        if not isinstance(field, dict):
            raise ValueError(f"Field {index} is invalid.")
        name = _clean_text(field.get("name"), 256, f"Field {index} name")
        value = _clean_text(field.get("value"), 1024, f"Field {index} value")
        if not name or not value:
            raise ValueError(f"Field {index} needs both a name and value.")
        fields.append({"name": name, "value": value, "inline": bool(field.get("inline"))})

    embed = {
        "author_name": _clean_text(raw_embed.get("author_name"), 256, "Author name"),
        "author_url": _clean_url(raw_embed.get("author_url"), "Author URL"),
        "author_icon_url": _clean_url(raw_embed.get("author_icon_url"), "Author icon URL"),
        "title": _clean_text(raw_embed.get("title"), 256, "Title"),
        "url": _clean_url(raw_embed.get("url"), "Title URL"),
        "description": _clean_text(raw_embed.get("description"), 4096, "Description"),
        "color": _clean_color(raw_embed.get("color")),
        "thumbnail_url": _clean_url(raw_embed.get("thumbnail_url"), "Thumbnail URL"),
        "image_url": _clean_url(raw_embed.get("image_url"), "Image URL"),
        "footer_text": _clean_text(raw_embed.get("footer_text"), 2048, "Footer"),
        "footer_icon_url": _clean_url(raw_embed.get("footer_icon_url"), "Footer icon URL"),
        "fields": fields,
    }
    embed_chars = sum(
        len(embed[key])
        for key in ("author_name", "title", "description", "footer_text")
    ) + sum(len(field["name"]) + len(field["value"]) for field in fields)
    if embed_chars > MAX_EMBED_TOTAL_CHARS:
        raise ValueError("Embed text cannot exceed 6,000 total characters.")

    buttons: list[dict[str, str]] = []
    raw_buttons = payload.get("buttons") or []
    if not isinstance(raw_buttons, list) or len(raw_buttons) > 5:
        raise ValueError("A message can contain at most 5 buttons.")
    for index, button in enumerate(raw_buttons, start=1):
        if not isinstance(button, dict):
            raise ValueError(f"Button {index} is invalid.")
        action = str(button.get("action") or "").strip()
        if action not in BUTTON_ACTIONS:
            raise ValueError(f"Button {index} needs a valid action.")
        label = _clean_text(button.get("label"), 80, f"Button {index} label")
        if not label:
            raise ValueError(f"Button {index} needs a label.")
        emoji = _clean_text(button.get("emoji"), 100, f"Button {index} emoji")
        style = str(button.get("style") or "secondary").strip().casefold()
        role_id = str(button.get("role_id") or "").strip()
        url = ""
        if action == "url":
            url = _clean_url(button.get("url"), f"Button {index} URL")
            if not url:
                raise ValueError(f"Button {index} needs a URL.")
            style = "link"
            role_id = ""
        else:
            if not SNOWFLAKE_RE.fullmatch(role_id):
                raise ValueError(f"Button {index} needs a Discord role.")
            if style not in BUTTON_STYLES:
                raise ValueError(f"Button {index} has an invalid color.")
        buttons.append({
            "label": label,
            "emoji": emoji,
            "style": style,
            "action": action,
            "role_id": role_id,
            "url": url,
        })

    has_embed_content = bool(
        embed["author_name"] or embed["title"] or embed["description"]
        or embed["image_url"] or embed["thumbnail_url"] or embed["footer_text"]
        or fields
    )
    if not content and not has_embed_content:
        raise ValueError("Add a regular message or at least one embed element.")
    return {"content": content, "embed": embed, "buttons": buttons}


def default_embed_payload() -> dict[str, Any]:
    return {
        "content": "",
        "embed": {
            "author_name": "",
            "author_url": "",
            "author_icon_url": "",
            "title": "",
            "url": "",
            "description": "",
            "color": "#25b8b8",
            "thumbnail_url": "",
            "image_url": "",
            "footer_text": "",
            "footer_icon_url": "",
            "fields": [],
        },
        "buttons": [],
    }


def _feature_names(template_id: int) -> list[str]:
    features = []
    if str(get_setting("BUMP_REMINDER_EMBED_ID", "") or "") == str(template_id):
        features.append("Bump reminders")
    return features


def list_embed_templates(query: str = "", sort: str = "updated", order: str = "desc") -> list[dict[str, Any]]:
    initialize_embed_templates_schema()
    sort_column = {"name": "name", "updated": "updated_at", "features": "name"}.get(sort, "updated_at")
    direction = "ASC" if order.casefold() == "asc" else "DESC"
    params: list[Any] = []
    where = ""
    if query.strip():
        where = "WHERE name LIKE ? ESCAPE '\\'"
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    with closing(_connect()) as connection:
        rows = connection.execute(
            f"SELECT id, name, created_at, updated_at, updated_by FROM embed_templates "
            f"{where} ORDER BY {sort_column} {direction}, id DESC",
            params,
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["features"] = _feature_names(int(item["id"]))
        results.append(item)
    if sort == "features":
        results.sort(
            key=lambda item: (", ".join(item["features"]).casefold(), item["name"].casefold()),
            reverse=direction == "DESC",
        )
    return results


def get_embed_template(template_id: int) -> Optional[dict[str, Any]]:
    initialize_embed_templates_schema()
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT id, name, payload_json, created_at, updated_at, updated_by "
            "FROM embed_templates WHERE id = ?",
            (int(template_id),),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    try:
        item["payload"] = validate_embed_payload(json.loads(str(item.pop("payload_json"))))
    except (json.JSONDecodeError, ValueError):
        item["payload"] = default_embed_payload()
    item["features"] = _feature_names(int(item["id"]))
    return item


def save_embed_template(
    *,
    name: str,
    payload_json: str,
    updated_by: str,
    template_id: Optional[int] = None,
) -> int:
    initialize_embed_templates_schema()
    clean_name = _clean_text(name, 100, "Name")
    if not clean_name:
        raise ValueError("Name is required.")
    try:
        payload = validate_embed_payload(json.loads(payload_json))
    except json.JSONDecodeError as exc:
        raise ValueError("Embed data could not be read. Refresh and try again.") from exc
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            if template_id is None:
                cursor = connection.execute(
                    "INSERT INTO embed_templates (name, payload_json, created_at, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (clean_name, json.dumps(payload, ensure_ascii=False), now, now, updated_by),
                )
                saved_id = int(cursor.lastrowid)
            else:
                cursor = connection.execute(
                    "UPDATE embed_templates SET name = ?, payload_json = ?, updated_at = ?, updated_by = ? "
                    "WHERE id = ?",
                    (clean_name, json.dumps(payload, ensure_ascii=False), now, updated_by, int(template_id)),
                )
                if cursor.rowcount < 1:
                    raise ValueError("Embed was not found.")
                saved_id = int(template_id)
            connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("An embed with that name already exists.") from exc
    return saved_id


def delete_embed_template(template_id: int) -> str:
    initialize_embed_templates_schema()
    features = _feature_names(int(template_id))
    if features:
        raise ValueError(
            "This embed is used by " + ", ".join(features) + ". Choose a different embed in Feature Settings first."
        )
    with closing(_connect()) as connection:
        row = connection.execute("SELECT name FROM embed_templates WHERE id = ?", (int(template_id),)).fetchone()
        if row is None:
            raise ValueError("Embed was not found.")
        connection.execute("DELETE FROM embed_templates WHERE id = ?", (int(template_id),))
        connection.commit()
    return str(row["name"])


def discord_embed_from_payload(payload: dict[str, Any]) -> Optional[discord.Embed]:
    data = validate_embed_payload(payload)["embed"]
    if not any(
        data[key]
        for key in ("author_name", "title", "description", "thumbnail_url", "image_url", "footer_text")
    ) and not data["fields"]:
        return None
    embed = discord.Embed(
        title=data["title"] or None,
        url=data["url"] or None,
        description=data["description"] or None,
        color=int(data["color"].lstrip("#"), 16),
    )
    if data["author_name"]:
        embed.set_author(
            name=data["author_name"],
            url=data["author_url"] or None,
            icon_url=data["author_icon_url"] or None,
        )
    if data["thumbnail_url"]:
        embed.set_thumbnail(url=data["thumbnail_url"])
    if data["image_url"]:
        embed.set_image(url=data["image_url"])
    if data["footer_text"]:
        embed.set_footer(text=data["footer_text"], icon_url=data["footer_icon_url"] or None)
    for field in data["fields"]:
        embed.add_field(name=field["name"], value=field["value"], inline=field["inline"])
    return embed


def _button_emoji(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return discord.PartialEmoji(name="emoji", id=int(text))
    if re.fullmatch(r"<a?:[^:>]+:\d{17,20}>", text):
        return discord.PartialEmoji.from_str(text)
    return text


def discord_view_from_payload(
    payload: dict[str, Any],
    *,
    subscribe_role_id: int = 0,
) -> Optional[discord.ui.View]:
    buttons = list(validate_embed_payload(payload)["buttons"])
    if subscribe_role_id:
        buttons = buttons[:4]
        buttons.append({
            "label": "Subscribe to Bump Reminders",
            "emoji": "🔔",
            "style": "primary",
            "action": "add_role",
            "role_id": str(subscribe_role_id),
            "url": "",
        })
    if not buttons:
        return None
    styles = {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }
    view = discord.ui.View(timeout=None)
    for button in buttons:
        emoji = _button_emoji(button["emoji"])
        if button["action"] == "url":
            view.add_item(discord.ui.Button(label=button["label"], emoji=emoji, url=button["url"]))
            continue
        verb = "add" if button["action"] == "add_role" else "remove"
        view.add_item(discord.ui.Button(
            label=button["label"],
            emoji=emoji,
            style=styles[button["style"]],
            custom_id=f"embedrole|{verb}|{button['role_id']}",
        ))
    return view
