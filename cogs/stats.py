import asyncio
import csv
import datetime
import io
import os
from typing import Dict, Iterable, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR
from utils.compact_roster import CompactRosterItem, render_compact_roster_pngs
from utils.stats_reports import (
    render_missingrole_report,
    render_report_error,
    render_rolecompare_report,
)


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


class StatsExportView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Export Members to CSV",
        style=discord.ButtonStyle.secondary,
        emoji="📄",
        custom_id="stats:export_members_csv",
    )
    async def export_csv(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not await has_stats_access(interaction):
            await interaction.response.send_message(
                PERMISSION_DENIED_MESSAGE,
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        file = await self.cog._report_csv_file(
            interaction.guild,
            interaction.message.id,
        )
        if file is None:
            await interaction.followup.send(
                "This tracked stats report could not be found.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file, ephemeral=True)


class Stats(commands.Cog):
    stats = app_commands.Group(
        name="stats",
        description="Create and manage live role membership embeds",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._refresh_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._export_view = StatsExportView(self)

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
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS tracked_stats_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                report_type TEXT NOT NULL,
                role_1_id INTEGER,
                role_2_id INTEGER,
                has_role_id INTEGER,
                missing_role_id INTEGER,
                title TEXT,
                body TEXT,
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
        self.bot.add_view(self._export_view)

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
        report_rows = await self._tracked_report_rows(
            guild_id=interaction.guild_id
        )

        refreshed = 0
        failed = 0
        for row in rows:
            if await self._refresh_row(row):
                refreshed += 1
            else:
                failed += 1
        for row in report_rows:
            if await self._refresh_report_row(row):
                refreshed += 1
            else:
                failed += 1

        total = len(rows) + len(report_rows)
        if not total:
            message = "There are no tracked stats pages in this server."
        else:
            message = f"Refreshed {refreshed} tracked stats page(s)."
            if failed:
                message += f" {failed} could not be refreshed."

        await interaction.followup.send(message, ephemeral=True)

    @stats.command(
        name="rolecompare",
        description="Create a tracked visual comparison of two roles",
    )
    @app_commands.describe(
        role_1="First role to compare",
        role_2="Second role to compare",
        title="Optional report title",
        body="Optional report description",
        channel="Channel where the report should be posted",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def rolecompare(
        self,
        interaction: discord.Interaction,
        role_1: discord.Role,
        role_2: discord.Role,
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        body: Optional[app_commands.Range[str, 1, 500]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if not self._valid_target_channel(interaction, target_channel):
            await interaction.response.send_message(
                "The stats report must be sent in a text channel in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        report_title = title or f"{role_1.name} vs {role_2.name}"
        created = await self._create_tracked_report(
            interaction=interaction,
            target_channel=target_channel,
            report_type="rolecompare",
            title=report_title,
            body=body or "",
            role_1=role_1,
            role_2=role_2,
        )
        if created:
            await interaction.followup.send(
                f"Created the tracked role comparison in {target_channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I could not create the role comparison report.",
                ephemeral=True,
            )

    @stats.command(
        name="missingrole",
        description="Create a tracked visual missing-role audit",
    )
    @app_commands.describe(
        has_role="Role members must currently have",
        missing_role="Role members must not currently have",
        title="Optional report title",
        body="Optional report description",
        channel="Channel where the report should be posted",
    )
    @app_commands.guild_only()
    @app_commands.check(has_stats_access)
    async def missingrole(
        self,
        interaction: discord.Interaction,
        has_role: discord.Role,
        missing_role: discord.Role,
        title: Optional[app_commands.Range[str, 1, 100]] = None,
        body: Optional[app_commands.Range[str, 1, 500]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target_channel = channel or interaction.channel
        if not self._valid_target_channel(interaction, target_channel):
            await interaction.response.send_message(
                "The stats report must be sent in a text channel in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        report_title = title or f"Missing {missing_role.name}"
        created = await self._create_tracked_report(
            interaction=interaction,
            target_channel=target_channel,
            report_type="missingrole",
            title=report_title,
            body=body or "",
            has_role=has_role,
            missing_role=missing_role,
        )
        if created:
            await interaction.followup.send(
                f"Created the tracked missing-role report in {target_channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "I could not create the missing-role report.",
                ephemeral=True,
            )

    async def _create_tracked_report(
        self,
        *,
        interaction: discord.Interaction,
        target_channel,
        report_type: str,
        title: str,
        body: str,
        role_1: Optional[discord.Role] = None,
        role_2: Optional[discord.Role] = None,
        has_role: Optional[discord.Role] = None,
        missing_role: Optional[discord.Role] = None,
    ) -> bool:
        now = self._utcnow()
        png = await self._render_report_png(
            report_type=report_type,
            title=title,
            body=body,
            updated_at=now,
            role_1=role_1,
            role_2=role_2,
            has_role=has_role,
            missing_role=missing_role,
        )
        file = discord.File(
            io.BytesIO(png),
            filename=f"{report_type}_report.png",
        )
        try:
            message = await target_channel.send(
                file=file,
                view=StatsExportView(self),
            )
        except (discord.Forbidden, discord.HTTPException):
            return False

        timestamp = now.isoformat()
        try:
            await self.bot.db.execute(
                """
                INSERT INTO tracked_stats_reports (
                    guild_id,
                    channel_id,
                    message_id,
                    report_type,
                    role_1_id,
                    role_2_id,
                    has_role_id,
                    missing_role_id,
                    title,
                    body,
                    created_by,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.guild_id,
                    target_channel.id,
                    message.id,
                    report_type,
                    role_1.id if role_1 else None,
                    role_2.id if role_2 else None,
                    has_role.id if has_role else None,
                    missing_role.id if missing_role else None,
                    title,
                    body,
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
        return True

    async def _render_report_png(
        self,
        *,
        report_type: str,
        title: str,
        body: str,
        updated_at: datetime.datetime,
        role_1: Optional[discord.Role] = None,
        role_2: Optional[discord.Role] = None,
        has_role: Optional[discord.Role] = None,
        missing_role: Optional[discord.Role] = None,
    ) -> bytes:
        if report_type == "rolecompare" and role_1 and role_2:
            data = self._calculate_rolecompare(role_1, role_2)
            return await asyncio.to_thread(
                render_rolecompare_report,
                title=title,
                body=body,
                role_1_name=role_1.name,
                role_2_name=role_2.name,
                counts=data["counts"],
                updated_at=updated_at,
                accent_color=role_1.color.value or COLOR,
            )

        if report_type == "missingrole" and has_role and missing_role:
            data = self._calculate_missingrole(has_role, missing_role)
            return await asyncio.to_thread(
                render_missingrole_report,
                title=title,
                body=body,
                has_role_name=has_role.name,
                missing_role_name=missing_role.name,
                has_role_total=data["has_role_total"],
                missing_role_total=data["missing_role_total"],
                missing_count=len(data["members"]),
                missing_percent=data["missing_percent"],
                updated_at=updated_at,
                accent_color=has_role.color.value or COLOR,
            )

        return await asyncio.to_thread(
            render_report_error,
            title=title or "Stats report",
            message="One or more configured roles no longer exist.",
            updated_at=updated_at,
            accent_color=COLOR,
        )

    @staticmethod
    def _calculate_rolecompare(
        role_1: discord.Role,
        role_2: discord.Role,
    ):
        role_1_members = {member.id: member for member in role_1.members}
        role_2_members = {member.id: member for member in role_2.members}
        both_ids = role_1_members.keys() & role_2_members.keys()
        role_1_only_ids = role_1_members.keys() - role_2_members.keys()
        role_2_only_ids = role_2_members.keys() - role_1_members.keys()
        return {
            "counts": {
                "role_1_total": len(role_1_members),
                "role_2_total": len(role_2_members),
                "both": len(both_ids),
                "role_1_only": len(role_1_only_ids),
                "role_2_only": len(role_2_only_ids),
            },
            "both": [role_1_members[user_id] for user_id in both_ids],
            "role_1_only": [
                role_1_members[user_id] for user_id in role_1_only_ids
            ],
            "role_2_only": [
                role_2_members[user_id] for user_id in role_2_only_ids
            ],
        }

    @staticmethod
    def _calculate_missingrole(
        has_role: discord.Role,
        missing_role: discord.Role,
    ):
        missing_role_member_ids = {member.id for member in missing_role.members}
        members = [
            member
            for member in has_role.members
            if member.id not in missing_role_member_ids
        ]
        has_role_total = len(has_role.members)
        return {
            "has_role_total": has_role_total,
            "missing_role_total": len(missing_role.members),
            "members": members,
            "missing_percent": (
                len(members) / has_role_total * 100 if has_role_total else 0
            ),
        }

    async def _tracked_report_rows(
        self,
        *,
        guild_id: Optional[int] = None,
        report_id: Optional[int] = None,
    ):
        clauses = []
        parameters = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            parameters.append(guild_id)
        if report_id is not None:
            clauses.append("id = ?")
            parameters.append(report_id)

        query = """
            SELECT
                id,
                guild_id,
                channel_id,
                message_id,
                report_type,
                role_1_id,
                role_2_id,
                has_role_id,
                missing_role_id,
                title,
                body
            FROM tracked_stats_reports
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        cursor = await self.bot.db.execute(query, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _refresh_report_row(self, row) -> bool:
        (
            record_id,
            guild_id,
            channel_id,
            message_id,
            report_type,
            role_1_id,
            role_2_id,
            has_role_id,
            missing_role_id,
            title,
            body,
        ) = row
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        channel = self._get_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return False

        role_1 = guild.get_role(role_1_id) if role_1_id else None
        role_2 = guild.get_role(role_2_id) if role_2_id else None
        has_role = guild.get_role(has_role_id) if has_role_id else None
        missing_role = guild.get_role(missing_role_id) if missing_role_id else None
        now = self._utcnow()
        png = await self._render_report_png(
            report_type=report_type,
            title=title or "Stats report",
            body=body or "",
            updated_at=now,
            role_1=role_1,
            role_2=role_2,
            has_role=has_role,
            missing_role=missing_role,
        )
        asset = (f"{report_type}_report.png", png)
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            message = None
        except (discord.Forbidden, discord.HTTPException):
            return False

        file = discord.File(io.BytesIO(png), filename=asset[0])
        if message is not None:
            try:
                await message.edit(
                    content=None,
                    embeds=[],
                    attachments=[file],
                    view=StatsExportView(self),
                )
                await self.bot.db.execute(
                    "UPDATE tracked_stats_reports SET updated_at = ? WHERE id = ?",
                    (now.isoformat(), record_id),
                )
                await self.bot.db.commit()
                return True
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        replacement = discord.File(io.BytesIO(png), filename=asset[0])
        try:
            new_message = await channel.send(
                file=replacement,
                view=StatsExportView(self),
            )
        except (discord.Forbidden, discord.HTTPException):
            return False

        if message is not None:
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await new_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                return False

        await self.bot.db.execute(
            """
            UPDATE tracked_stats_reports
            SET message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_message.id, now.isoformat(), record_id),
        )
        await self.bot.db.commit()
        return True

    async def _report_csv_file(
        self,
        guild: discord.Guild,
        message_id: int,
    ) -> Optional[discord.File]:
        cursor = await self.bot.db.execute(
            """
            SELECT
                report_type,
                role_1_id,
                role_2_id,
                has_role_id,
                missing_role_id
            FROM tracked_stats_reports
            WHERE guild_id = ? AND message_id = ?
            """,
            (guild.id, message_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return None

        report_type, role_1_id, role_2_id, has_role_id, missing_role_id = row
        generated_at = self._utcnow().isoformat()
        output = io.StringIO(newline="")
        writer = csv.writer(output)

        if report_type == "rolecompare":
            role_1 = guild.get_role(role_1_id)
            role_2 = guild.get_role(role_2_id)
            if role_1 is None or role_2 is None:
                return None
            data = self._calculate_rolecompare(role_1, role_2)
            writer.writerow(
                [
                    "category",
                    "user_id",
                    "username",
                    "display_name",
                    "role_1_name",
                    "role_2_name",
                    "generated_at",
                ]
            )
            for category in ("role_1_only", "role_2_only", "both"):
                members = sorted(
                    data[category],
                    key=lambda member: self._member_username(member).casefold(),
                )
                for member in members:
                    writer.writerow(
                        [
                            category,
                            member.id,
                            self._member_username(member),
                            member.display_name,
                            role_1.name,
                            role_2.name,
                            generated_at,
                        ]
                    )
            filename = "rolecompare_members.csv"
        elif report_type == "missingrole":
            has_role = guild.get_role(has_role_id)
            missing_role = guild.get_role(missing_role_id)
            if has_role is None or missing_role is None:
                return None
            data = self._calculate_missingrole(has_role, missing_role)
            writer.writerow(
                [
                    "user_id",
                    "username",
                    "display_name",
                    "has_role_name",
                    "missing_role_name",
                    "generated_at",
                ]
            )
            for member in sorted(
                data["members"],
                key=lambda member: self._member_username(member).casefold(),
            ):
                writer.writerow(
                    [
                        member.id,
                        self._member_username(member),
                        member.display_name,
                        has_role.name,
                        missing_role.name,
                        generated_at,
                    ]
                )
            filename = "missingrole_members.csv"
        else:
            return None

        return discord.File(
            io.BytesIO(output.getvalue().encode("utf-8-sig")),
            filename=filename,
        )

    @staticmethod
    def _valid_target_channel(interaction, channel) -> bool:
        return bool(
            channel
            and hasattr(channel, "send")
            and getattr(channel, "guild", None)
            and channel.guild.id == interaction.guild_id
        )

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
        for source, message_id, channel_id, role_id, title in rows[:24]:
            channel = self._get_channel(interaction.guild, channel_id)
            role = interaction.guild.get_role(role_id) if role_id else None
            page_title = title or (
                f"{role.name} Members" if role else f"Role {role_id} Members"
            )
            channel_name = getattr(channel, "name", f"channel-{channel_id}")
            option_label = f"{page_title} - #{channel_name}"
            options.append(
                discord.SelectOption(
                    label=option_label[:100],
                    value=f"{source}:{message_id}",
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
            SELECT source, message_id, channel_id, role_id, title, created_at
            FROM (
                SELECT
                    'roster' AS source,
                    message_id,
                    channel_id,
                    role_id,
                    title,
                    created_at
                FROM role_stat_embeds
                WHERE guild_id = ?
                UNION ALL
                SELECT
                    'report' AS source,
                    message_id,
                    channel_id,
                    COALESCE(role_1_id, has_role_id) AS role_id,
                    title,
                    created_at
                FROM tracked_stats_reports
                WHERE guild_id = ?
            )
            ORDER BY created_at DESC
            """,
            (guild_id, guild_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row[:5] for row in rows]

    async def _delete_tracked_pages(
        self,
        guild: discord.Guild,
        selection: str,
    ) -> str:
        if selection == "all":
            cursor = await self.bot.db.execute(
                """
                SELECT 'roster', id, channel_id, message_id
                FROM role_stat_embeds
                WHERE guild_id = ?
                UNION ALL
                SELECT 'report', id, channel_id, message_id
                FROM tracked_stats_reports
                WHERE guild_id = ?
                """,
                (guild.id, guild.id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        elif ":" in selection:
            source, raw_message_id = selection.split(":", 1)
            if source not in {"roster", "report"} or not raw_message_id.isdigit():
                return "That tracked stats selection is no longer valid."
            table = (
                "role_stat_embeds"
                if source == "roster"
                else "tracked_stats_reports"
            )
            cursor = await self.bot.db.execute(
                f"""
                SELECT ?, id, channel_id, message_id
                FROM {table}
                WHERE guild_id = ? AND message_id = ?
                """,
                (source, guild.id, int(raw_message_id)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        else:
            return "That tracked stats selection is no longer valid."

        if not rows:
            return "No matching tracked stats pages were found."

        roster_ids = [row[1] for row in rows if row[0] == "roster"]
        report_ids = [row[1] for row in rows if row[0] == "report"]
        if roster_ids:
            placeholders = ", ".join("?" for _ in roster_ids)
            await self.bot.db.execute(
                f"DELETE FROM role_stat_embeds WHERE id IN ({placeholders})",
                roster_ids,
            )
        if report_ids:
            placeholders = ", ".join("?" for _ in report_ids)
            await self.bot.db.execute(
                f"DELETE FROM tracked_stats_reports WHERE id IN ({placeholders})",
                report_ids,
            )
        await self.bot.db.commit()

        deleted_messages = 0
        failed_messages = 0
        for _, _, channel_id, message_id in rows:
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
        cursor = await self.bot.db.execute(
            f"""
            SELECT DISTINCT id
            FROM tracked_stats_reports
            WHERE guild_id = ?
              AND (
                    role_1_id IN ({placeholders})
                 OR role_2_id IN ({placeholders})
                 OR has_role_id IN ({placeholders})
                 OR missing_role_id IN ({placeholders})
              )
            """,
            (
                guild_id,
                *role_ids,
                *role_ids,
                *role_ids,
                *role_ids,
            ),
        )
        tracked_report_ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()

        for role_id in tracked_role_ids:
            key = (guild_id, role_id)
            existing_task = self._refresh_tasks.get(key)
            if existing_task:
                existing_task.cancel()
            self._refresh_tasks[key] = asyncio.create_task(
                self._debounced_role_refresh(guild_id, role_id)
            )
        for report_id in tracked_report_ids:
            key = (guild_id, -report_id)
            existing_task = self._refresh_tasks.get(key)
            if existing_task:
                existing_task.cancel()
            self._refresh_tasks[key] = asyncio.create_task(
                self._debounced_report_refresh(guild_id, report_id)
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

    async def _debounced_report_refresh(
        self,
        guild_id: int,
        report_id: int,
    ) -> None:
        key = (guild_id, -report_id)
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
            rows = await self._tracked_report_rows(report_id=report_id)
            for row in rows:
                await self._refresh_report_row(row)
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
