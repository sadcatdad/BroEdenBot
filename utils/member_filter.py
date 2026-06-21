from typing import Optional

import discord


MEMBER_CACHE_WARNING = (
    "Current-member filtering may be limited by member cache/intents."
)


def current_member(
    guild: discord.Guild,
    user_id: int,
) -> Optional[discord.Member]:
    """Return a cached current guild member without making API requests."""
    try:
        return guild.get_member(int(user_id))
    except (AttributeError, TypeError, ValueError):
        return None


def is_current_member(guild: discord.Guild, user_id: int) -> bool:
    return current_member(guild, user_id) is not None


def member_filter_warning(
    bot: discord.Client,
    guild: discord.Guild,
) -> Optional[str]:
    intents = getattr(bot, "intents", None)
    if intents is not None and not getattr(intents, "members", False):
        return MEMBER_CACHE_WARNING
    if getattr(guild, "chunked", True) is False:
        return MEMBER_CACHE_WARNING
    return None
