#!/usr/bin/env python3
"""Export current members of excluded statistic roles to a JSON cache."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.exclusions import parse_csv_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Discord members who have any excluded stats role."
    )
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--role-ids", required=True, help="Comma-separated role IDs")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/excluded_activity_users.json"),
    )
    parser.add_argument(
        "--token-env",
        default="DISCORD_TOKEN",
        help="Environment variable containing the Discord bot token.",
    )
    return parser.parse_args()


async def export_members(args: argparse.Namespace) -> int:
    role_ids = parse_csv_ids(args.role_ids)
    if not role_ids:
        print("--role-ids must contain at least one Discord role ID.")
        return 2
    token = os.getenv(args.token_env, "").strip()
    if not token:
        print(f"{args.token_env} is not configured.")
        return 2

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
        guild = client.get_guild(args.guild_id)
        if guild is None:
            guild = await client.fetch_guild(args.guild_id)

        users = []
        async for member in guild.fetch_members(limit=None):
            matched_roles = sorted(role.id for role in member.roles if role.id in role_ids)
            if not matched_roles:
                continue
            users.append(
                {
                    "id": str(member.id),
                    "username": member.name,
                    "display_name": member.display_name,
                    "role_ids": [str(role_id) for role_id in matched_roles],
                    "bot": bool(member.bot),
                }
            )

        payload = {
            "guild_id": str(args.guild_id),
            "role_ids": [str(role_id) for role_id in sorted(role_ids)],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "users": sorted(users, key=lambda item: int(item["id"])),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {len(users):,} excluded members to {args.output}.")
        return 0
    finally:
        await client.close()
        with contextlib.suppress(Exception):
            await task


def main() -> int:
    return asyncio.run(export_members(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
