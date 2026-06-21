from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import discord
import requests
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from utils.ui import (
    INFO_COLOR,
    branded_embed,
    error_embed,
    success_embed,
    warning_embed,
)


logger = logging.getLogger(__name__)


def can_manage_queue(member: object) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (permissions.administrator or permissions.manage_channels)
    )


def is_queue_channel(channel: object) -> bool:
    return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))


class Queue(commands.Cog):
    queue = app_commands.Group(name="queue", description="Queue commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.history: dict[int, discord.Message] = {}
        self._channel_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                user_id INTEGER
            )
            """
        )
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS queue_lock (id INTEGER PRIMARY KEY)"
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_dashboard (
                channel_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            )
            """
        )
        await self.bot.db.execute(
            """
            DELETE FROM queue
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM queue
                GROUP BY channel_id, user_id
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_channel_user
            ON queue(channel_id, user_id)
            """
        )
        await self.bot.db.commit()

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        return self._channel_locks.setdefault(channel_id, asyncio.Lock())

    async def _is_locked(self, channel_id: int) -> bool:
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM queue_lock WHERE id = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def _queue_user_ids(self, channel_id: int) -> list[int]:
        cursor = await self.bot.db.execute(
            """
            SELECT user_id
            FROM queue
            WHERE channel_id = ?
            ORDER BY id ASC
            """,
            (channel_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [int(row[0]) for row in rows]

    async def _stored_dashboard_id(self, channel_id: int) -> Optional[int]:
        cursor = await self.bot.db.execute(
            "SELECT message_id FROM queue_dashboard WHERE channel_id = ?",
            (channel_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row else None

    async def _store_dashboard(self, channel_id: int, message_id: int) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO queue_dashboard (channel_id, message_id)
            VALUES (?, ?)
            ON CONFLICT(channel_id)
            DO UPDATE SET message_id = excluded.message_id
            """,
            (channel_id, message_id),
        )
        await self.bot.db.commit()

    @queue.command(name="dashboard", description="Post a polished queue dashboard")
    async def queue_dashboard(self, interaction: discord.Interaction) -> None:
        if not is_queue_channel(interaction.channel):
            await interaction.response.send_message(
                embed=error_embed(
                    "Use a voice-channel chat",
                    "Queue dashboards belong in the text chat attached to the "
                    "matching voice or stage channel.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = await self.send_queue(interaction.channel)
        await interaction.followup.send(
            embed=success_embed(
                "Queue dashboard posted",
                f"[Open the dashboard]({message.jump_url}).",
            ),
            ephemeral=True,
        )

    async def create_queue_dashboard(
        self,
        channel: discord.abc.GuildChannel,
    ) -> tuple[discord.Embed, Optional[discord.File], discord.ui.View]:
        locked = await self._is_locked(channel.id)
        user_ids = await self._queue_user_ids(channel.id)
        members: list[discord.Member] = []
        stale_ids: list[int] = []
        for user_id in user_ids:
            member = channel.guild.get_member(user_id)
            if member is None:
                stale_ids.append(user_id)
            else:
                members.append(member)
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            await self.bot.db.execute(
                f"""
                DELETE FROM queue
                WHERE channel_id = ? AND user_id IN ({placeholders})
                """,
                (channel.id, *stale_ids),
            )
            await self.bot.db.commit()

        state = "🔒 Locked" if locked else "🟢 Open"
        description = [
            f"**Status:** {state}",
            f"**Waiting:** {len(members):,}",
            "",
        ]
        if members:
            for index, member in enumerate(members, start=1):
                marker = "✨ **UP NEXT**" if index == 1 else f"`#{index}`"
                description.append(f"{marker}  {member.mention}")
        else:
            description.append(
                "The queue is clear. Join the matching voice channel, then tap "
                "**Join Queue**."
            )

        embed = branded_embed(
            "🎟️ Bro Eden Queue",
            "\n".join(description),
            color=INFO_COLOR,
            footer="Join • Leave • Move Back • Staff Pull",
        )
        file = None
        if members:
            file = await asyncio.to_thread(self.create_banner, members[0])
            if file:
                embed.set_image(url="attachment://queue-next.png")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Join Queue",
                emoji="➕",
                style=discord.ButtonStyle.success,
                custom_id="queue|join",
                disabled=locked,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Leave",
                emoji="↩️",
                style=discord.ButtonStyle.danger,
                custom_id="queue|leave",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Move Back",
                emoji="⏬",
                style=discord.ButtonStyle.secondary,
                custom_id="queue|delay",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Pull Next",
                emoji="🎤",
                style=discord.ButtonStyle.primary,
                custom_id="queue|pull",
            )
        )
        return embed, file, view

    async def update_queue(self, interaction: discord.Interaction) -> None:
        embed, file, view = await self.create_queue_dashboard(interaction.channel)
        attachments = [file] if file else []
        await interaction.edit_original_response(
            embed=embed,
            attachments=attachments,
            view=view,
        )
        if interaction.message is not None:
            self.history[interaction.channel.id] = interaction.message
            await self._store_dashboard(
                interaction.channel.id,
                interaction.message.id,
            )

    async def send_queue(
        self,
        channel: discord.abc.Messageable,
        content: Optional[str] = None,
    ) -> discord.Message:
        previous = self.history.get(channel.id)
        if previous is None:
            stored_id = await self._stored_dashboard_id(channel.id)
            if stored_id:
                try:
                    previous = await channel.fetch_message(stored_id)
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    previous = None
        if previous is not None:
            try:
                await previous.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        embed, file, view = await self.create_queue_dashboard(channel)
        kwargs = {"content": content, "embed": embed, "view": view}
        if file:
            kwargs["file"] = file
        message = await channel.send(**kwargs)
        self.history[channel.id] = message
        await self._store_dashboard(channel.id, message.id)
        return message

    def create_banner(self, user: discord.Member) -> Optional[discord.File]:
        try:
            background = Image.open("assets/up_next.png").convert("RGBA")
            with requests.get(
                user.display_avatar.url,
                timeout=10,
            ) as response:
                response.raise_for_status()
                avatar = Image.open(io.BytesIO(response.content)).convert("RGBA")
            avatar = avatar.resize((167, 167), Image.Resampling.LANCZOS)
        except (
            OSError,
            UnidentifiedImageError,
            requests.RequestException,
        ):
            logger.warning("Queue banner avatar unavailable for user_id=%s", user.id)
            return None

        mask = Image.new("L", avatar.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, 166, 166), fill=255)
        avatar.putalpha(mask)
        background.paste(avatar, (667, 45), avatar)
        draw = ImageDraw.Draw(background)
        font = ImageFont.truetype("assets/calibri.ttf", 40)
        display_name = user.display_name[:28]
        draw.text(
            (120, 150),
            f"@{display_name}",
            font=font,
            fill=(255, 255, 255),
        )
        image_binary = io.BytesIO()
        background.save(image_binary, "PNG", optimize=True)
        image_binary.seek(0)
        return discord.File(fp=image_binary, filename="queue-next.png")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        if not custom_id.startswith("queue|"):
            return
        if interaction.guild is None or interaction.channel is None:
            return
        parts = custom_id.split("|", 1)
        if len(parts) != 2 or parts[1] not in {"join", "leave", "delay", "pull"}:
            await interaction.response.send_message(
                "That queue button is no longer valid.",
                ephemeral=True,
            )
            return

        action = parts[1]
        await interaction.response.defer(ephemeral=True)
        async with self._lock_for(interaction.channel.id):
            if action == "pull":
                if not can_manage_queue(interaction.user):
                    await interaction.followup.send(
                        embed=error_embed(
                            "Staff control",
                            "You need **Manage Channels** to pull the next member.",
                        ),
                        ephemeral=True,
                    )
                    return
                pulled = await self.pull_from_queue(
                    interaction.user,
                    interaction.channel,
                )
                if not pulled:
                    await interaction.followup.send(
                        embed=warning_embed(
                            "Queue empty",
                            "There is nobody waiting right now.",
                        ),
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        embed=success_embed(
                            "Next member called",
                            "The queue has advanced and the dashboard was refreshed.",
                        ),
                        ephemeral=True,
                    )
                return

            user_ids = await self._queue_user_ids(interaction.channel.id)
            in_queue = interaction.user.id in user_ids
            if action == "join":
                if await self._is_locked(interaction.channel.id):
                    response = warning_embed(
                        "Queue locked",
                        "Staff have temporarily paused new entries.",
                    )
                elif in_queue:
                    response = warning_embed(
                        "Already queued",
                        "You are already in this queue.",
                    )
                elif not (
                    interaction.user.voice
                    and interaction.user.voice.channel
                    and interaction.user.voice.channel.id
                    == interaction.channel.id
                ):
                    response = error_embed(
                        "Join the voice channel first",
                        "You must be connected to this queue’s voice channel.",
                    )
                else:
                    await self.bot.db.execute(
                        """
                        INSERT OR IGNORE INTO queue (channel_id, user_id)
                        VALUES (?, ?)
                        """,
                        (interaction.channel.id, interaction.user.id),
                    )
                    await self.bot.db.commit()
                    response = success_embed(
                        "You’re in",
                        "You have been added to the queue.",
                    )
            elif action == "leave":
                if not in_queue:
                    response = warning_embed(
                        "Not queued",
                        "You are not currently in this queue.",
                    )
                else:
                    await self.bot.db.execute(
                        "DELETE FROM queue WHERE channel_id = ? AND user_id = ?",
                        (interaction.channel.id, interaction.user.id),
                    )
                    await self.bot.db.commit()
                    response = success_embed(
                        "Left queue",
                        "You have been removed from the queue.",
                    )
            else:
                if not in_queue:
                    response = warning_embed(
                        "Not queued",
                        "Join the queue before moving back.",
                    )
                else:
                    await self.move_user(
                        interaction.user.id,
                        "drop",
                        interaction.channel.id,
                    )
                    response = success_embed(
                        "Moved back",
                        "You moved one position later in the queue.",
                    )

            await interaction.followup.send(embed=response, ephemeral=True)
            await self.update_queue(interaction)

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        if not is_queue_channel(interaction.channel):
            await interaction.response.send_message(
                embed=error_embed(
                    "Use a voice-channel chat",
                    "Run queue controls in the matching voice or stage channel.",
                ),
                ephemeral=True,
            )
            return False
        if can_manage_queue(interaction.user):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Staff control",
                "You need **Manage Channels** to use this queue command.",
            ),
            ephemeral=True,
        )
        return False

    @queue.command(name="lock", description="Pause new queue entries")
    @app_commands.default_permissions(manage_channels=True)
    async def queue_lock(self, interaction: discord.Interaction) -> None:
        if not await self._require_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.execute(
            "INSERT OR REPLACE INTO queue_lock (id) VALUES (?)",
            (interaction.channel.id,),
        )
        await self.bot.db.commit()
        await interaction.followup.send(
            embed=success_embed(
                "Queue locked",
                f"New entries are paused in {interaction.channel.mention}.",
            ),
            ephemeral=True,
        )
        await self.send_queue(interaction.channel)

    @queue.command(name="unlock", description="Allow new queue entries")
    @app_commands.default_permissions(manage_channels=True)
    async def queue_unlock(self, interaction: discord.Interaction) -> None:
        if not await self._require_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.db.execute(
            "DELETE FROM queue_lock WHERE id = ?",
            (interaction.channel.id,),
        )
        await self.bot.db.commit()
        await interaction.followup.send(
            embed=success_embed(
                "Queue unlocked",
                f"New entries are open in {interaction.channel.mention}.",
            ),
            ephemeral=True,
        )
        await self.send_queue(interaction.channel)

    @queue.command(name="move", description="Move a member to a queue position")
    @app_commands.default_permissions(manage_channels=True)
    async def queue_move(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        position: app_commands.Range[int, 1],
    ) -> None:
        if not await self._require_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        user_ids = await self._queue_user_ids(interaction.channel.id)
        if user.id not in user_ids:
            await interaction.followup.send(
                embed=warning_embed(
                    "Member not queued",
                    f"{user.mention} is not in this queue.",
                ),
                ephemeral=True,
            )
            return
        final_position = await self.move_user(
            user.id,
            int(position),
            interaction.channel.id,
        )
        await interaction.followup.send(
            embed=success_embed(
                "Queue updated",
                f"Moved {user.mention} to position **{final_position}**.",
            ),
            ephemeral=True,
        )
        await self.send_queue(interaction.channel)

    async def move_user(
        self,
        user_id: int,
        position: object,
        channel_id: int,
    ) -> int:
        users = await self._queue_user_ids(channel_id)
        if user_id not in users:
            raise ValueError("User is not in the queue.")
        current = users.index(user_id)
        users.remove(user_id)
        if position == "drop":
            target = min(current + 1, len(users))
        else:
            target = min(max(int(position) - 1, 0), len(users))
        users.insert(target, user_id)

        await self.bot.db.execute(
            "DELETE FROM queue WHERE channel_id = ?",
            (channel_id,),
        )
        await self.bot.db.executemany(
            "INSERT INTO queue (channel_id, user_id) VALUES (?, ?)",
            [(channel_id, queued_user_id) for queued_user_id in users],
        )
        await self.bot.db.commit()
        return target + 1

    @queue.command(name="remove", description="Remove a member from the queue")
    @app_commands.default_permissions(manage_channels=True)
    async def queue_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        cursor = await self.bot.db.execute(
            "DELETE FROM queue WHERE channel_id = ? AND user_id = ?",
            (interaction.channel.id, user.id),
        )
        await self.bot.db.commit()
        if cursor.rowcount:
            response = success_embed(
                "Member removed",
                f"{user.mention} was removed from the queue.",
            )
        else:
            response = warning_embed(
                "Member not queued",
                f"{user.mention} was not in this queue.",
            )
        await cursor.close()
        await interaction.followup.send(embed=response, ephemeral=True)
        await self.send_queue(interaction.channel)

    @commands.command(name="q", description="Queue Dashboard")
    async def queue_command(self, ctx: commands.Context) -> None:
        if not is_queue_channel(ctx.channel):
            await ctx.message.add_reaction("❌")
            return
        await ctx.message.add_reaction("✅")
        await self.send_queue(ctx.channel)

    @commands.command(name="qj", description="Queue Join")
    async def queue_join(self, ctx: commands.Context) -> None:
        if not is_queue_channel(ctx.channel):
            await ctx.message.add_reaction("❌")
            return
        async with self._lock_for(ctx.channel.id):
            user_ids = await self._queue_user_ids(ctx.channel.id)
            if ctx.author.id in user_ids:
                await ctx.message.add_reaction("⏳")
                return
            if await self._is_locked(ctx.channel.id):
                await ctx.message.add_reaction("🔒")
                return
            if not (
                ctx.author.voice
                and ctx.author.voice.channel
                and ctx.author.voice.channel.id == ctx.channel.id
            ):
                await ctx.message.add_reaction("🔇")
                return
            await self.bot.db.execute(
                """
                INSERT OR IGNORE INTO queue (channel_id, user_id)
                VALUES (?, ?)
                """,
                (ctx.channel.id, ctx.author.id),
            )
            await self.bot.db.commit()
        await ctx.message.add_reaction("✅")
        await self.send_queue(
            ctx.channel,
            content=f"{ctx.author.mention} joined the queue.",
        )

    @commands.command(name="ql", description="Queue Leave")
    async def queue_leave(self, ctx: commands.Context) -> None:
        if not is_queue_channel(ctx.channel):
            await ctx.message.add_reaction("❌")
            return
        cursor = await self.bot.db.execute(
            "DELETE FROM queue WHERE channel_id = ? AND user_id = ?",
            (ctx.channel.id, ctx.author.id),
        )
        await self.bot.db.commit()
        changed = cursor.rowcount
        await cursor.close()
        await ctx.message.add_reaction("✅" if changed else "❌")
        if changed:
            await self.send_queue(
                ctx.channel,
                content=f"{ctx.author.mention} left the queue.",
            )

    @commands.command(name="qd", description="Queue Drop 1 Place")
    async def queue_drop(self, ctx: commands.Context) -> None:
        if not is_queue_channel(ctx.channel):
            await ctx.message.add_reaction("❌")
            return
        user_ids = await self._queue_user_ids(ctx.channel.id)
        if ctx.author.id not in user_ids:
            await ctx.message.add_reaction("❌")
            return
        await self.move_user(ctx.author.id, "drop", ctx.channel.id)
        await ctx.message.add_reaction("✅")
        await self.send_queue(
            ctx.channel,
            content=f"{ctx.author.mention} moved back one place.",
        )

    @commands.command(name="qn", description="Queue Next")
    @commands.has_permissions(manage_channels=True)
    async def queue_next(self, ctx: commands.Context) -> None:
        if not is_queue_channel(ctx.channel):
            await ctx.message.add_reaction("❌")
            return
        pulled = await self.pull_from_queue(ctx.author, ctx.channel)
        await ctx.message.add_reaction("✅" if pulled else "❌")

    async def pull_from_queue(
        self,
        by: discord.abc.User,
        channel: discord.abc.GuildChannel,
    ) -> bool:
        cursor = await self.bot.db.execute(
            """
            SELECT id, user_id
            FROM queue
            WHERE channel_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (channel.id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return False

        queue_id, user_id = row
        await self.bot.db.execute(
            "DELETE FROM queue WHERE channel_id = ? AND id = ?",
            (channel.id, queue_id),
        )
        await self.bot.db.commit()
        next_ids = await self._queue_user_ids(channel.id)
        member = channel.guild.get_member(user_id)
        lines = [
            f"🎤 <@{user_id}> was pulled by {by.mention}.",
            f"It is now <@{user_id}>’s turn.",
        ]
        if next_ids:
            lines.append(f"**Up next:** <@{next_ids[0]}>")
        if not (
            member
            and member.voice
            and member.voice.channel
            and member.voice.channel.id == channel.id
        ):
            lines.append("⚠️ The pulled member is not in this voice channel.")
        await channel.send(
            embed=branded_embed(
                "Queue call",
                "\n".join(lines),
                color=INFO_COLOR,
            )
        )
        await self.send_queue(channel)
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Queue(bot))
