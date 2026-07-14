"""Shared, mention-safe publishing for configured Discord audit events."""

from __future__ import annotations

import logging

import discord

from utils.settings import get_setting
from utils.ui import branded_embed


logger = logging.getLogger(__name__)


async def publish_audit(
    bot,
    guild: discord.Guild,
    title: str,
    description: str,
) -> bool:
    raw = str(get_setting("AUDIT_LOG_THREAD_ID", "") or "").strip()
    if not raw.isdigit():
        return False
    channel_id = int(raw)
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.exception("Audit thread unavailable channel_id=%s", channel_id)
            return False
    if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
        logger.warning("Audit thread belongs to another guild channel_id=%s", channel_id)
        return False
    if not hasattr(channel, "send"):
        logger.warning("Audit destination is not sendable channel_id=%s", channel_id)
        return False
    try:
        if isinstance(channel, discord.Thread) and channel.archived:
            await channel.edit(
                archived=False,
                reason="Reopen configured Bro Eden audit thread",
            )
        await channel.send(
            embed=branded_embed(title, description),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not publish audit event channel_id=%s", channel_id)
        return False
