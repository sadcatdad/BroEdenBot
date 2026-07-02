"""Admin commands and live listeners for Discord knowledge source channels."""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional, Union

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from utils.live_knowledge import (
    KNOWLEDGE_SOURCE_TYPES,
    KNOWLEDGE_SYNC_MODES,
    KNOWLEDGE_VISIBILITIES,
    delete_knowledge_entry,
    delete_knowledge_source,
    get_matching_source,
    initialize_live_knowledge_schema,
    list_knowledge_sources,
    mark_source_synced,
    upsert_knowledge_entry_from_message,
    upsert_knowledge_source,
)
from utils.ui import INFO_COLOR, SUCCESS_COLOR, branded_embed


logger = logging.getLogger(__name__)
SYNC_LIMIT_DEFAULT = 200
SYNC_LIMIT_MAX = 1_000


SourceChannel = Union[discord.TextChannel, discord.ForumChannel, discord.Thread]


class KnowledgeSources(commands.Cog):
    knowledge = app_commands.Group(
        name="knowledge",
        description="Manage live Discord knowledge sources",
    )
    sources = app_commands.Group(
        name="sources",
        description="Configure source-of-truth Discord channels",
    )
    knowledge.add_command(sources)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        db = self._db()
        if db is not None:
            await initialize_live_knowledge_schema(db)

    def _db(self) -> Optional[aiosqlite.Connection]:
        return getattr(self.bot, "db", None)

    @staticmethod
    def _can_manage(interaction: discord.Interaction) -> bool:
        user = interaction.user
        permissions = getattr(user, "guild_permissions", None)
        return bool(
            permissions
            and (permissions.administrator or permissions.manage_guild)
        )

    async def _deny_if_needed(self, interaction: discord.Interaction) -> bool:
        if self._can_manage(interaction):
            return False
        await interaction.response.send_message(
            "Only server managers can configure knowledge sources.",
            ephemeral=True,
        )
        return True

    async def _source_for_channel(
        self,
        guild_id: int,
        channel_id: int,
    ) -> Optional[aiosqlite.Row]:
        db = self._db()
        if db is None:
            return None
        cursor = await db.execute(
            """
            SELECT *
            FROM knowledge_sources
            WHERE guild_id = ? AND channel_id = ?
            """,
            (guild_id, channel_id),
        )
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def _fetch_channel(self, channel_id: int) -> Optional[SourceChannel]:
        cached = self.bot.get_channel(channel_id)
        if isinstance(cached, (discord.TextChannel, discord.ForumChannel, discord.Thread)):
            return cached
        try:
            channel = await self.bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
        return channel if isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.Thread)) else None

    async def _iter_thread_messages(
        self,
        thread: discord.Thread,
        *,
        limit: int,
    ) -> AsyncIterator[discord.Message]:
        count = 0
        try:
            async for message in thread.history(limit=limit, oldest_first=True):
                yield message
                count += 1
                if count >= limit:
                    break
        except discord.HTTPException:
            logger.warning("Could not read forum thread history: thread_id=%s", thread.id)

    async def _iter_forum_threads(
        self,
        channel: discord.ForumChannel,
        *,
        limit: int,
    ) -> AsyncIterator[discord.Thread]:
        seen: set[int] = set()
        for thread in getattr(channel, "threads", []) or []:
            if thread.id in seen:
                continue
            seen.add(thread.id)
            yield thread
            if len(seen) >= limit:
                return
        try:
            async for thread in channel.archived_threads(limit=limit):
                if thread.id in seen:
                    continue
                seen.add(thread.id)
                yield thread
                if len(seen) >= limit:
                    return
        except (discord.HTTPException, AttributeError):
            logger.warning("Could not read archived forum threads: channel_id=%s", channel.id)

    async def _iter_source_messages(
        self,
        channel: SourceChannel,
        *,
        limit: int,
    ) -> AsyncIterator[discord.Message]:
        if isinstance(channel, discord.Thread):
            async for message in self._iter_thread_messages(channel, limit=limit):
                yield message
            return
        if isinstance(channel, discord.TextChannel):
            async for message in channel.history(limit=limit, oldest_first=True):
                yield message
            return
        if isinstance(channel, discord.ForumChannel):
            remaining = limit
            async for thread in self._iter_forum_threads(channel, limit=limit):
                if remaining <= 0:
                    break
                async for message in self._iter_thread_messages(thread, limit=remaining):
                    yield message
                    remaining -= 1
                    if remaining <= 0:
                        break

    async def _index_message(self, message: discord.Message, source: aiosqlite.Row) -> bool:
        if getattr(message.author, "id", None) == getattr(self.bot.user, "id", None):
            return False
        db = self._db()
        if db is None:
            return False
        return await upsert_knowledge_entry_from_message(
            db,
            message=message,
            source=source,
        )

    async def _sync_channel(
        self,
        channel: SourceChannel,
        source: aiosqlite.Row,
        *,
        limit: int,
    ) -> tuple[int, int, Optional[int]]:
        indexed = 0
        scanned = 0
        last_message_id = None
        async for message in self._iter_source_messages(channel, limit=limit):
            scanned += 1
            last_message_id = message.id
            if await self._index_message(message, source):
                indexed += 1
        db = self._db()
        if db is not None:
            await mark_source_synced(
                db,
                guild_id=source["guild_id"],
                channel_id=source["channel_id"],
                last_message_id=last_message_id,
            )
        return scanned, indexed, last_message_id

    @sources.command(name="add", description="Add or update a Discord knowledge source")
    @app_commands.describe(
        channel="Text channel, forum channel, or specific forum post/thread to index",
        source_type="Kind of knowledge this channel contains",
        visibility="public for member answers, staff_only for private staff tools",
        sync_mode="live updates continuously; manual only syncs when requested",
    )
    @app_commands.choices(
        source_type=[
            app_commands.Choice(name=value, value=value)
            for value in sorted(KNOWLEDGE_SOURCE_TYPES)
        ],
        visibility=[
            app_commands.Choice(name=value, value=value)
            for value in sorted(KNOWLEDGE_VISIBILITIES)
        ],
        sync_mode=[
            app_commands.Choice(name=value, value=value)
            for value in sorted(KNOWLEDGE_SYNC_MODES)
        ],
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_source(
        self,
        interaction: discord.Interaction,
        channel: SourceChannel,
        source_type: app_commands.Choice[str],
        visibility: app_commands.Choice[str],
        sync_mode: app_commands.Choice[str],
    ) -> None:
        if await self._deny_if_needed(interaction):
            return
        db = self._db()
        if db is None or interaction.guild_id is None:
            await interaction.response.send_message(
                "The shared database is not ready yet.",
                ephemeral=True,
            )
            return
        await upsert_knowledge_source(
            db,
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            channel_name=channel.name,
            source_type=source_type.value,
            visibility=visibility.value,
            sync_mode=sync_mode.value,
            enabled=True,
        )
        await db.commit()
        await interaction.response.send_message(
            (
                f"{channel.mention} is now a `{source_type.value}` knowledge source.\n"
                f"Visibility: `{visibility.value}`\n"
                f"Sync mode: `{sync_mode.value}`\n"
                "Run `/knowledge sources sync` to backfill existing messages. "
                "For forum info pages, select the individual forum post/thread."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sources.command(name="list", description="List configured knowledge sources")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def list_sources(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_needed(interaction):
            return
        db = self._db()
        if db is None or interaction.guild_id is None:
            await interaction.response.send_message(
                "The shared database is not ready yet.",
                ephemeral=True,
            )
            return
        rows = await list_knowledge_sources(db, guild_id=interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "No live knowledge sources are configured yet.",
                ephemeral=True,
            )
            return
        embed = branded_embed(
            "Knowledge Sources",
            color=INFO_COLOR,
            footer="Public answers can only use public sources",
        )
        for row in rows[:20]:
            state = "enabled" if row["enabled"] else "disabled"
            latest = row["latest_indexed_at"] or row["last_synced_at"] or "never"
            embed.add_field(
                name=f"#{row['channel_name']} ({state})"[:256],
                value=(
                    f"Type: `{row['source_type']}`\n"
                    f"Visibility: `{row['visibility']}`\n"
                    f"Sync: `{row['sync_mode']}`\n"
                    f"Entries: **{row['entry_count'] or 0}**\n"
                    f"Latest: `{latest}`"
                )[:1024],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sources.command(name="remove", description="Remove a Discord knowledge source")
    @app_commands.describe(channel="Configured source channel to remove")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_source(
        self,
        interaction: discord.Interaction,
        channel: SourceChannel,
    ) -> None:
        if await self._deny_if_needed(interaction):
            return
        db = self._db()
        if db is None or interaction.guild_id is None:
            await interaction.response.send_message(
                "The shared database is not ready yet.",
                ephemeral=True,
            )
            return
        deleted_entries = await delete_knowledge_source(
            db,
            guild_id=interaction.guild_id,
            channel_id=channel.id,
        )
        await db.commit()
        await interaction.response.send_message(
            f"Removed {channel.mention} and {deleted_entries} indexed entries.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @sources.command(name="sync", description="Backfill configured knowledge source messages")
    @app_commands.describe(
        channel="Optional source channel; leave empty to sync all enabled sources",
        limit="Maximum messages to scan per source",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def sync_sources(
        self,
        interaction: discord.Interaction,
        channel: Optional[SourceChannel] = None,
        limit: app_commands.Range[int, 1, SYNC_LIMIT_MAX] = SYNC_LIMIT_DEFAULT,
    ) -> None:
        if await self._deny_if_needed(interaction):
            return
        db = self._db()
        if db is None or interaction.guild_id is None:
            await interaction.response.send_message(
                "The shared database is not ready yet.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if channel is not None:
            source = await self._source_for_channel(interaction.guild_id, channel.id)
            rows = [source] if source is not None else []
        else:
            rows = [
                row
                for row in await list_knowledge_sources(db, guild_id=interaction.guild_id)
                if row["enabled"]
            ]
        if not rows:
            await interaction.followup.send(
                "No matching enabled knowledge sources were found.",
                ephemeral=True,
            )
            return

        lines = []
        total_indexed = 0
        for row in rows:
            source_channel = channel if channel is not None else await self._fetch_channel(row["channel_id"])
            if not isinstance(source_channel, (discord.TextChannel, discord.ForumChannel, discord.Thread)):
                lines.append(f"`{row['channel_name']}`: unavailable or unsupported")
                continue
            try:
                scanned, indexed, _ = await self._sync_channel(
                    source_channel,
                    row,
                    limit=limit,
                )
            except discord.Forbidden:
                lines.append(f"`{row['channel_name']}`: missing Read Message History")
                continue
            except discord.HTTPException:
                logger.exception("Knowledge source sync failed: channel_id=%s", row["channel_id"])
                lines.append(f"`{row['channel_name']}`: Discord read failed")
                continue
            total_indexed += indexed
            lines.append(f"`{row['channel_name']}`: scanned {scanned}, indexed {indexed}")

        await db.commit()
        embed = branded_embed(
            "Knowledge Sync Complete",
            description="\n".join(lines)[:4000],
            color=SUCCESS_COLOR,
            footer=f"{total_indexed} entries indexed or updated",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        db = self._db()
        if db is None or message.guild is None:
            return
        if getattr(message.author, "id", None) == getattr(self.bot.user, "id", None):
            return
        parent_id = getattr(getattr(message.channel, "parent", None), "id", None)
        source = await get_matching_source(
            db,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            parent_channel_id=parent_id,
            live_only=True,
        )
        if source is None:
            return
        try:
            changed = await self._index_message(message, source)
            if changed:
                await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Could not index live knowledge message_id=%s", message.id)

    @commands.Cog.listener()
    async def on_message_edit(
        self,
        before: discord.Message,
        after: discord.Message,
    ) -> None:
        await self.on_message(after)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        db = self._db()
        if db is None or message.guild is None:
            return
        try:
            await delete_knowledge_entry(
                db,
                guild_id=message.guild.id,
                source_message_id=message.id,
            )
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Could not delete live knowledge message_id=%s", message.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        db = self._db()
        if db is None or payload.guild_id is None:
            return
        try:
            await delete_knowledge_entry(
                db,
                guild_id=payload.guild_id,
                source_message_id=payload.message_id,
            )
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception(
                "Could not delete raw live knowledge message_id=%s",
                payload.message_id,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KnowledgeSources(bot))
