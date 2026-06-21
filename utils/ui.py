"""Shared Discord UI helpers for a consistent Bro Eden visual style."""

from __future__ import annotations

from typing import Optional

import discord

from config import COLOR


SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
ERROR_COLOR = 0xED4245
INFO_COLOR = 0x5865F2
MUTED_COLOR = 0x747F8D
FOOTER_TEXT = "Bro Eden • Community tools"


def branded_embed(
    title: str,
    description: Optional[str] = None,
    *,
    color: int = COLOR,
    timestamp: bool = False,
    footer: str = FOOTER_TEXT,
) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256],
        description=description,
        color=color,
        timestamp=discord.utils.utcnow() if timestamp else None,
    )
    if footer:
        embed.set_footer(text=footer[:2048])
    return embed


def success_embed(title: str, description: str) -> discord.Embed:
    return branded_embed(f"✅ {title}", description, color=SUCCESS_COLOR)


def warning_embed(title: str, description: str) -> discord.Embed:
    return branded_embed(f"⚠️ {title}", description, color=WARNING_COLOR)


def error_embed(title: str, description: str) -> discord.Embed:
    return branded_embed(f"⛔ {title}", description, color=ERROR_COLOR)


def progress_bar(
    current: int,
    total: int,
    *,
    width: int = 10,
    filled: str = "▰",
    empty: str = "▱",
) -> str:
    ratio = min(max(current / max(total, 1), 0), 1)
    filled_count = round(ratio * width)
    return filled * filled_count + empty * (width - filled_count)


def truncate(value: object, limit: int, fallback: str = "Not provided") -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        text = fallback
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
