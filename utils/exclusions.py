"""Shared user and role exclusion helpers for stats surfaces."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional


def parse_csv_ids(value: Optional[str]) -> set[int]:
    ids: set[int] = set()
    for item in str(value or "").replace("\n", ",").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            parsed = int(text)
        except ValueError:
            continue
        if parsed > 0:
            ids.add(parsed)
    return ids


def env_csv_ids(name: str) -> set[int]:
    return parse_csv_ids(os.getenv(name, ""))


def member_has_any_role(member: Any, role_ids: Iterable[int]) -> bool:
    wanted = set(role_ids)
    if not wanted or member is None:
        return False
    roles = getattr(member, "roles", None)
    if roles is None:
        return False
    return any(getattr(role, "id", None) in wanted for role in roles)


def member_is_excluded(
    member: Any,
    *,
    user_ids: Iterable[int],
    role_ids: Iterable[int],
) -> bool:
    member_id = getattr(member, "id", None)
    if member_id in set(user_ids):
        return True
    return member_has_any_role(member, role_ids)


def load_excluded_user_cache(path: Optional[Path | str]) -> set[int]:
    if not path:
        return set()
    cache_path = Path(path)
    if not cache_path.exists():
        raise FileNotFoundError(f"Excluded-user cache not found: {cache_path}")
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    return user_ids_from_cache_data(data)


def user_ids_from_cache_data(data: Any) -> set[int]:
    if isinstance(data, dict):
        candidates = (
            data.get("users")
            or data.get("members")
            or data.get("excluded_users")
            or data.get("user_ids")
            or []
        )
    else:
        candidates = data

    ids: set[int] = set()
    for item in candidates or []:
        value = item.get("id") if isinstance(item, dict) else item
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            ids.add(parsed)
    return ids


async def fetch_role_member_ids(
    guild_id: int,
    role_ids: Iterable[int],
    *,
    token_env: str = "DISCORD_TOKEN",
) -> set[int]:
    import asyncio
    import contextlib

    import discord

    wanted = set(role_ids)
    token = os.getenv(token_env, "").strip()
    if not token or not guild_id or not wanted:
        return set()

    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True
    client = discord.Client(intents=intents)
    ready = asyncio.Event()

    @client.event
    async def on_ready() -> None:
        ready.set()

    await client.login(token)
    task = asyncio.create_task(client.connect(reconnect=False))
    try:
        await asyncio.wait_for(ready.wait(), timeout=30)
        guild = client.get_guild(guild_id)
        if guild is None:
            guild = await client.fetch_guild(guild_id)
        matched: set[int] = set()
        async for member in guild.fetch_members(limit=None):
            if any(role.id in wanted for role in member.roles):
                matched.add(member.id)
        return matched
    finally:
        await client.close()
        with contextlib.suppress(Exception):
            await task
