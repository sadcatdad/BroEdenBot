"""Minimal command metadata; never persist arguments, responses, or inferred motives."""

from __future__ import annotations

import re

import discord


COMMAND_USAGE_SQL = """
CREATE TABLE IF NOT EXISTS message_context_command_usage (
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    parent_channel_id TEXT,
    user_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    interaction_id TEXT NOT NULL,
    command_name TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    nsfw_access INTEGER,
    message_id TEXT,
    PRIMARY KEY (guild_id, application_id, interaction_id)
)
"""
COMMAND_USAGE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_context_command_user_time
ON message_context_command_usage (guild_id, user_id, timestamp)
"""


def command_name(data: object) -> str:
    if not isinstance(data, dict):
        return "unknown"
    names = [str(data.get("name") or "")]
    options = data.get("options", [])
    while isinstance(options, list):
        sub = next((option for option in options if isinstance(option, dict)
                    and option.get("type") in (1, 2)), None)
        if sub is None:
            break
        names.append(str(sub.get("name") or ""))
        options = sub.get("options", [])
    name = " ".join(names).strip().casefold()
    return name if re.fullmatch(r"[\w -]{1,100}", name) else "unknown"


def public_command_response(message: object, trusted_rank_bots: set[int]):
    """Modern metadata lacks the command name. Keep that uncertainty explicit."""
    if not getattr(getattr(message, "author", None), "bot", False):
        return None
    if getattr(getattr(message, "flags", None), "ephemeral", False):
        return None
    legacy = getattr(message, "interaction", None)
    metadata = getattr(message, "interaction_metadata", None) or legacy
    if metadata is None or getattr(metadata, "type", None) != discord.InteractionType.application_command:
        return None
    user = getattr(metadata, "user", None)
    if user is None or getattr(user, "bot", False) or not getattr(metadata, "id", None):
        return None
    name = command_name({"name": getattr(legacy, "name", "")}) if legacy else "unknown"
    confidence = "exact" if name != "unknown" else "unknown"
    if name == "unknown" and message.author.id in trusted_rank_bots:
        # A trusted bot's textual rank/level card can be identified, but does
        # not reveal slash arguments or whose level was requested.
        texts = [str(getattr(message, "content", "") or "")]
        for embed in getattr(message, "embeds", []):
            texts.extend([str(embed.title or ""), str(embed.description or "")])
            for field in embed.fields:
                texts.extend([field.name, field.value])
        text = " ".join(texts)
        if re.search(r"\brank(?:ing)?\b", text, re.I) and re.search(r"\blevel\b", text, re.I):
            name, confidence = "rank", "response_inferred"
    return user, str(metadata.id), name, confidence
