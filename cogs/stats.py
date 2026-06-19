import asyncio
import datetime
import io
import os
from typing import Dict, Iterable, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR
from utils.compact_roster import CompactRosterItem, render_compact_roster_pngs


PERMISSION_DENIED_MESSAGE = "You do not have permission to use stats commands."
DEBOUNCE_SECONDS = 2.0


def allowed_stats_role_ids() -> Set[int]:
    role_ids = set()
    for value in os.getenv("STATS_ALLOWED_ROLE_IDS", "").split(","):
        value = value.strip()
        if value.isdigit():
            role_ids.add(int(value))
    return role_ids


async def has_stats_access(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    if interaction.user.guild_permissions.administrator:
        return True

    permitted_roles = allowed_stats_role_ids()
    return any(role.id in permitted_roles for role in interaction.user.roles)


async def has_stats_delete_access(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


class RoleStatsModal(discord.ui.Modal):
    def __init__(
        self,
        cog,
        role: discord.Role,
        channel,
        image: Optional[discord.Attachment],
    ):
        super().__init__(title="Create role roster")
        self.cog = cog
        self.role = role
        self.channel = channel
        self.image = image
        self.header = discord.ui.TextInput(
            label="Header",
            placeholder=f"{role.name} Members",
            style=discord.TextStyle.short,
            required=False,
            max_length=100,
        )
        self.body = discord.ui.TextInput(
            label="Body",
            placeholder=(
                "Add supporting text. Line breaks are preserved; "
                "basic Markdown markers are removed."
            ),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.header)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._create_role_embed(
            interaction=interaction,
            role=self.role,
            title=str(self.header.value or "").strip() or f"{self.role.name} Members",
            body=str(self.body.value or ""),
            target_channel=self.channel,
            image=self.image,
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        message = "The stats embed could not be created."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class StatsDeleteSelect(discord.ui.Select):
    def __init__(self, cog, options):
        self.cog = cog
        super().__init__(
            placeholder="Choose a tracked stats page to delete",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await has_stats_delete_access(interaction):
            await interaction.response.send_message(
                "Only administrators can use /stats delete.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        summary = await self.cog._delete_tracked_pages(
            interaction.guild,
            self.values[0],
        )
        for item in self.view.children:
            item.disabled = True
        await interaction.edit_original_response(view=self.view)
        await interaction.followup.send(summary, ephemeral=True)


class StatsDeleteView(discord.ui.View):
    def __init__(self, cog, options):
        super().__init__(timeout=120)
        self.add_item(StatsDeleteSelect(cog, options))


class Stats(commands.Cog):
    stats = app_commands.Group(
        name="stats",
        description="Create and manage live role membership embeds",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_tasks: Dict[Tuple[int, int], asyncio.Task] = {}

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS role_stat_embeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                image_url TEXT,
                image_data BLOB,
                graphic_enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cursor = await self.bot.db.execute("PRAGMA table_info(role_stat_embeds)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        if "image_url" not in columns:
            await self.bot.db.execute(
                "ALTER TABLE role_stat_embeds ADD COLUMN image_url TEXT"
            )
        if "image_data" not in columns:
            await self.bot.db.execute(
                "ALTER TABLE role_stat_embeds ADD COLUMN image_data BLOB"
            )
        if "graphic_enabled" not in columns:
            await self.bot.db.execute(
                """
                ALTER TABLE role_stat_embeds
                ADD COLUMN graphic_enabled INTEGER NOT NULL DEFAULT 1
                """
            )
        await self.bot.db.commit()

    async def cog_unload(self) -> None:
        for task in self._refresh_tasks.values():
            task.cancel()
        self._refresh_tasks.clear()

    @stats.command(name="role", description="Create a live role roster graphic")
    @app_commands.describe(
        role="Role whose current members should be listed",
        channel="Channel where the embed should be sent",
        image="Optional banner image rendered into the roster card",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        channel: Optional[discord.TextChannel] = None,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if target_channel is None or not hasattr(target_channel, "send"):
            await interaction.response.send_message(
                "I cannot send a stats embed in that channel.", ephemeral=True
            )
            return

        target_guild = getattr(target_channel, "guild", None)
        if target_guild is None or target_guild.id != interaction.guild_id:
            await interaction.response.send_message(
                "The stats embed must be sent in this server.", ephemeral=True
            )
            return

        if image is not None and not self._is_image_attachment(image):
            await interaction.response.send_message(
                "The image option must be an image attachment.", ephemeral=True
            )
            return
        if image is not None and image.size > 8_000_000:
            await interaction.response.send_message(
                "The banner image must be 8 MB or smaller.", ephemeral=True
            )
            return

        await interaction.response.send_modal(
            RoleStatsModal(
                cog=self,
                role=role,
                channel=target_channel,
                image=image,
            )
        )

    async def _create_role_embed(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        title: Optional[str],
        body: str,
        target_channel,
        image: Optional[discord.Attachment],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        now = self._utcnow()
        image_url = None
        image_data = None

        if image is not None:
            try:
                image_data = await image.read()
                image_url = image.url
            except (discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "I could not download the selected image.", ephemeral=True
                )
                return

        try:
            roster_files = await self._build_roster_files(
                role,
                title,
                body,
                now,
                image_data,
            )
        except Exception:
            await interaction.followup.send(
                "I could not generate the roster graphic.", ephemeral=True
            )
            return
        try:
            message = await target_channel.send(
                files=roster_files,
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "I could not send the stats embed in that channel. "
                "Please check my channel permissions.",
                ephemeral=True,
            )
            return

        timestamp = now.isoformat()
        try:
            await self.bot.db.execute(
                """
                INSERT INTO role_stat_embeds (
                    guild_id,
                    channel_id,
                    message_id,
                    role_id,
                    title,
                    body,
                    image_url,
                    image_data,
                    graphic_enabled,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.guild_id,
                    target_channel.id,
                    message.id,
                    role.id,
                    title or "",
                    body,
                    image_url,
                    image_data,
                    1,
                    interaction.user.id,
                    timestamp,
                    timestamp,
                ),
            )
            await self.bot.db.commit()
        except Exception:
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            raise

        await interaction.followup.send(
            f"Created a tracked stats embed for {role.mention} in "
            f"{target_channel.mention}.",
            ephemeral=True,
        )

    @stats.command(
        name="refresh",
        description="Refresh all tracked role embeds in this server",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await self._tracked_rows(guild_id=interaction.guild_id)

        refreshed = 0
        failed = 0
        for row in rows:
            if await self._refresh_row(row):
                refreshed += 1
            else:
                failed += 1

        if not rows:
            message = "There are no tracked role embeds in this server."
        else:
            message = f"Refreshed {refreshed} tracked role embed(s)."
            if failed:
                message += f" {failed} could not be refreshed."

        await interaction.followup.send(message, ephemeral=True)

    @stats.command(
        name="reset",
        description="Delete all tracked stats pages in this server",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_delete_access)
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = await self._delete_tracked_pages(interaction.guild, "all")
        await interaction.followup.send(summary, ephemeral=True)

    @stats.command(
        name="delete",
        description="Delete one or all tracked stats pages",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_delete_access)
    async def delete(
        self,
        interaction: discord.Interaction,
    ) -> None:
        rows = await self._delete_menu_rows(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                "There are no tracked stats pages in this server.",
                ephemeral=True,
            )
            return

        options = [
            discord.SelectOption(
                label="Delete all tracked stats pages",
                value="all",
                description=f"Remove all {len(rows)} tracked page(s)",
                emoji="🗑️",
            )
        ]
        for message_id, channel_id, role_id, title in rows[:24]:
            channel = self._get_channel(interaction.guild, channel_id)
            role = interaction.guild.get_role(role_id)
            page_title = title or (
                f"{role.name} Members" if role else f"Role {role_id} Members"
            )
            channel_name = getattr(channel, "name", f"channel-{channel_id}")
            option_label = f"{page_title} - #{channel_name}"
            options.append(
                discord.SelectOption(
                    label=option_label[:100],
                    value=str(message_id),
                    description="Tracked role stats page",
                    emoji="📊",
                )
            )

        await interaction.response.send_message(
            "Select the tracked stats page you want to delete.",
            view=StatsDeleteView(self, options),
            ephemeral=True,
        )

    async def _delete_menu_rows(self, guild_id: int):
        cursor = await self.bot.db.execute(
            """
            SELECT message_id, channel_id, role_id, title
            FROM role_stat_embeds
            WHERE guild_id = ?
            ORDER BY created_at DESC
            """,
            (guild_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _delete_tracked_pages(
        self,
        guild: discord.Guild,
        selection: str,
    ) -> str:
        if selection == "all":
            cursor = await self.bot.db.execute(
                """
                SELECT id, channel_id, message_id
                FROM role_stat_embeds
                WHERE guild_id = ?
                """,
                (guild.id,),
            )
        elif (
            selection.isdigit()
            and 0 < int(selection) <= 9_223_372_036_854_775_807
        ):
            cursor = await self.bot.db.execute(
                """
                SELECT id, channel_id, message_id
                FROM role_stat_embeds
                WHERE guild_id = ? AND message_id = ?
                """,
                (guild.id, int(selection)),
            )
        else:
            return "That tracked stats selection is no longer valid."

        rows = await cursor.fetchall()
        await cursor.close()
        if not rows:
            return "No matching tracked stats pages were found."

        record_ids = [row[0] for row in rows]
        placeholders = ", ".join("?" for _ in record_ids)
        await self.bot.db.execute(
            f"DELETE FROM role_stat_embeds WHERE id IN ({placeholders})",
            record_ids,
        )
        await self.bot.db.commit()

        deleted_messages = 0
        failed_messages = 0
        for _, channel_id, message_id in rows:
            channel = self._get_channel(guild, channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                failed_messages += 1
                continue
            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
                deleted_messages += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                failed_messages += 1

        return (
            f"Removed {len(rows)} tracked page(s). "
            f"Deleted {deleted_messages} Discord message(s). "
            f"Failed to delete {failed_messages}."
        )

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        before_role_ids = {role.id for role in before.roles}
        after_role_ids = {role.id for role in after.roles}
        changed_role_ids = before_role_ids.symmetric_difference(after_role_ids)
        await self._queue_tracked_role_refreshes(after.guild.id, changed_role_ids)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._queue_tracked_role_refreshes(
            member.guild.id, (role.id for role in member.roles)
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._queue_tracked_role_refreshes(
            member.guild.id, (role.id for role in member.roles)
        )

    async def _queue_tracked_role_refreshes(
        self, guild_id: int, role_ids: Iterable[int]
    ) -> None:
        role_ids = set(role_ids)
        if not role_ids:
            return

        placeholders = ", ".join("?" for _ in role_ids)
        cursor = await self.bot.db.execute(
            f"""
            SELECT DISTINCT role_id
            FROM role_stat_embeds
            WHERE guild_id = ? AND role_id IN ({placeholders})
            """,
            (guild_id, *role_ids),
        )
        tracked_role_ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()

        for role_id in tracked_role_ids:
            key = (guild_id, role_id)
            existing_task = self._refresh_tasks.get(key)
            if existing_task:
                existing_task.cancel()
            self._refresh_tasks[key] = asyncio.create_task(
                self._debounced_role_refresh(guild_id, role_id)
            )

    async def _debounced_role_refresh(self, guild_id: int, role_id: int) -> None:
        key = (guild_id, role_id)
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            rows = await self._tracked_rows(guild_id=guild_id, role_id=role_id)
            for row in rows:
                await self._refresh_row(row)
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task()
            if self._refresh_tasks.get(key) is current_task:
                self._refresh_tasks.pop(key, None)

    async def _tracked_rows(
        self,
        guild_id: Optional[int] = None,
        role_id: Optional[int] = None,
    ):
        clauses = []
        parameters = []

        if guild_id is not None:
            clauses.append("guild_id = ?")
            parameters.append(guild_id)
        if role_id is not None:
            clauses.append("role_id = ?")
            parameters.append(role_id)

        query = """
            SELECT
                id,
                guild_id,
                channel_id,
                message_id,
                role_id,
                title,
                body,
                image_url,
                image_data,
                graphic_enabled
            FROM role_stat_embeds
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        cursor = await self.bot.db.execute(query, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _refresh_row(self, row) -> bool:
        (
            record_id,
            guild_id,
            channel_id,
            message_id,
            role_id,
            title,
            body,
            image_url,
            image_data,
            graphic_enabled,
        ) = row

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False

        role = guild.get_role(role_id)
        channel = self._get_channel(guild, channel_id)
        if role is None or channel is None or not hasattr(channel, "fetch_message"):
            return False

        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            return False

        if image_data is None and message is not None:
            legacy_banner = next(
                (
                    attachment
                    for attachment in message.attachments
                    if not self._is_roster_filename(attachment.filename, role.id)
                    and self._is_image_attachment(attachment)
                ),
                None,
            )
            if legacy_banner is not None:
                try:
                    image_data = await legacy_banner.read()
                    image_url = legacy_banner.url
                    await self.bot.db.execute(
                        """
                        UPDATE role_stat_embeds
                        SET image_url = ?, image_data = ?
                        WHERE id = ?
                        """,
                        (image_url, image_data, record_id),
                    )
                    await self.bot.db.commit()
                except (discord.Forbidden, discord.HTTPException):
                    image_data = None

        now = self._utcnow()
        try:
            roster_files = await self._build_roster_files(
                role,
                title,
                body,
                now,
                image_data,
            )
        except Exception:
            return False
        roster_assets = [
            (roster_file.filename, roster_file.fp.getvalue())
            for roster_file in roster_files
        ]
        if message is None:
            return await self._recreate_tracked_message(
                record_id=record_id,
                channel=channel,
                old_message=None,
                roster_assets=roster_assets,
                updated_at=now,
            )

        try:
            await message.edit(
                content=None,
                embeds=[],
                attachments=roster_files,
            )
        except discord.NotFound:
            return await self._recreate_tracked_message(
                record_id=record_id,
                channel=channel,
                old_message=None,
                roster_assets=roster_assets,
                updated_at=now,
            )
        except (discord.Forbidden, discord.HTTPException):
            return await self._recreate_tracked_message(
                record_id=record_id,
                channel=channel,
                old_message=message,
                roster_assets=roster_assets,
                updated_at=now,
            )

        await self.bot.db.execute(
            "UPDATE role_stat_embeds SET updated_at = ? WHERE id = ?",
            (now.isoformat(), record_id),
        )
        await self.bot.db.commit()
        return True

    async def _recreate_tracked_message(
        self,
        record_id: int,
        channel,
        old_message,
        roster_assets,
        updated_at: datetime.datetime,
    ) -> bool:
        replacement_files = [
            discord.File(io.BytesIO(data), filename=filename)
            for filename, data in roster_assets
        ]

        try:
            new_message = await channel.send(files=replacement_files)
        except (discord.Forbidden, discord.HTTPException):
            return False

        if old_message is not None:
            try:
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await new_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return False

        await self.bot.db.execute(
            """
            UPDATE role_stat_embeds
            SET message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_message.id, updated_at.isoformat(), record_id),
        )
        await self.bot.db.commit()
        return True

    async def _build_roster_files(
        self,
        role: discord.Role,
        title: Optional[str],
        body: Optional[str],
        updated_at: datetime.datetime,
        banner_bytes: Optional[bytes],
    ):
        members = sorted(
            role.members,
            key=lambda member: self._member_username(member).casefold(),
        )
        items = [
            CompactRosterItem(
                label=self._member_username(member),
                avatar_url=str(member.display_avatar.replace(size=32).url),
            )
            for member in members
        ]
        pngs = await render_compact_roster_pngs(
            title=title or f"{role.name} Members",
            body=body or "",
            role_name=role.name,
            items=items,
            updated_at=updated_at,
            accent_color=role.color.value or COLOR,
            include_avatars=True,
            banner_bytes=banner_bytes,
        )
        return [
            discord.File(
                fp=io.BytesIO(png),
                filename=self._roster_filename(page_number, len(pngs)),
            )
            for page_number, png in enumerate(pngs, start=1)
        ]

    @staticmethod
    def _roster_filename(page_number: int, page_count: int) -> str:
        if page_count == 1:
            return "role_roster.png"
        return f"role_roster_{page_number}.png"

    @staticmethod
    def _is_roster_filename(filename: str, role_id: int) -> bool:
        return (
            filename == f"role-roster-{role_id}.png"
            or filename == "role_roster.png"
            or (
                filename.startswith("role_roster_")
                and filename.endswith(".png")
            )
        )

    @staticmethod
    def _member_username(member: discord.Member) -> str:
        return (
            getattr(member, "name", None)
            or getattr(member, "global_name", None)
            or str(member.id)
        )

    @staticmethod
    def _get_channel(guild: discord.Guild, channel_id: int):
        get_channel_or_thread = getattr(guild, "get_channel_or_thread", None)
        if get_channel_or_thread:
            return get_channel_or_thread(channel_id)
        return guild.get_channel(channel_id)

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        if attachment.content_type:
            return attachment.content_type.startswith("image/")
        return attachment.filename.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        )

    @staticmethod
    def _utcnow() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            command_name = getattr(interaction.command, "name", None)
            if command_name in {"delete", "reset"}:
                message = f"Only administrators can use /stats {command_name}."
            else:
                message = PERMISSION_DENIED_MESSAGE
        else:
            message = "The stats command could not be completed."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stats(bot))
