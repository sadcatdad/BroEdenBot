import logging
from typing import Any, List

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import get_csv_ids_setting

MAX_NOTE_LENGTH = 2_000
MAX_VIEW_NOTES = 50
NOTES_PER_EMBED = 5
logger = logging.getLogger(__name__)


def _truncate(value: Any, limit: int, fallback: str = "Not provided") -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        text = fallback
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _display_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown date"
    return text[:16].replace("T", " ") + " UTC"


class StaffNotes(commands.Cog):
    staffnote = app_commands.Group(
        name="staffnote",
        description="Private staff-written notes for moderation context",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                target_display_name TEXT,
                note TEXT NOT NULL,
                created_by_id INTEGER NOT NULL,
                created_by_display_name TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                is_deleted INTEGER DEFAULT 0
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_staff_notes_guild_target
            ON staff_notes (guild_id, target_user_id, is_deleted, created_at)
            """
        )
        await self.bot.db.commit()

    @staticmethod
    def _is_administrator(interaction: discord.Interaction) -> bool:
        return bool(
            interaction.guild
            and isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        )

    def _has_access(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        allowed_role_ids = set(get_csv_ids_setting("STAFF_NOTES_ALLOWED_ROLE_IDS"))
        return any(
            role.id in allowed_role_ids for role in interaction.user.roles
        )

    async def _deny_if_unauthorised(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if self._has_access(interaction):
            return False
        await interaction.response.send_message(
            "Staff notes are limited to administrators and approved staff roles.",
            ephemeral=True,
        )
        return True

    @staticmethod
    def _log_database_error(operation: str, exc: Exception) -> None:
        logger.error(
            "Staff notes database failure: operation=%s error_type=%s",
            operation,
            type(exc).__name__,
        )

    @staffnote.command(name="add", description="Add a private staff note")
    @app_commands.describe(
        user="Member the note is about",
        note="Staff-written note",
    )
    @app_commands.guild_only()
    async def add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        note: app_commands.Range[str, 1, MAX_NOTE_LENGTH],
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return

        created_at = discord.utils.utcnow().isoformat()
        try:
            cursor = await self.bot.db.execute(
                """
                INSERT INTO staff_notes (
                    guild_id,
                    target_user_id,
                    target_display_name,
                    note,
                    created_by_id,
                    created_by_display_name,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.guild_id,
                    user.id,
                    user.display_name,
                    str(note).strip(),
                    interaction.user.id,
                    interaction.user.display_name,
                    created_at,
                ),
            )
            await self.bot.db.commit()
            note_id = cursor.lastrowid
            await cursor.close()
        except Exception as exc:
            self._log_database_error("add", exc)
            await interaction.response.send_message(
                "The staff note could not be saved. Please try again later.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Staff note #{note_id} was added for {user.mention}.",
            ephemeral=True,
        )

    @staffnote.command(name="view", description="View active notes for a member")
    @app_commands.describe(user="Member whose staff notes to view")
    @app_commands.guild_only()
    async def view(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cursor = await self.bot.db.execute(
                """
                SELECT id, note, created_by_display_name, created_by_id,
                       created_at, updated_at
                FROM staff_notes
                WHERE guild_id = ?
                  AND target_user_id = ?
                  AND is_deleted = 0
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (interaction.guild_id, user.id, MAX_VIEW_NOTES),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        except Exception as exc:
            self._log_database_error("view", exc)
            await interaction.followup.send(
                "Staff notes could not be loaded. Please try again later.",
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send(
                f"No active staff notes were found for {user.mention}.",
                ephemeral=True,
            )
            return

        embeds = self._build_note_embeds(user, rows)
        await interaction.followup.send(embeds=embeds, ephemeral=True)

    @staffnote.command(name="delete", description="Soft-delete a staff note")
    @app_commands.describe(note_id="ID of the staff note to delete")
    @app_commands.guild_only()
    async def delete(
        self,
        interaction: discord.Interaction,
        note_id: app_commands.Range[int, 1],
    ) -> None:
        if not self._is_administrator(interaction):
            await interaction.response.send_message(
                "Only administrators can delete staff notes.",
                ephemeral=True,
            )
            return

        updated_at = discord.utils.utcnow().isoformat()
        try:
            cursor = await self.bot.db.execute(
                """
                UPDATE staff_notes
                SET is_deleted = 1,
                    updated_at = ?
                WHERE id = ?
                  AND guild_id = ?
                  AND is_deleted = 0
                """,
                (updated_at, note_id, interaction.guild_id),
            )
            changed = cursor.rowcount
            await cursor.close()
            await self.bot.db.commit()
        except Exception as exc:
            self._log_database_error("delete", exc)
            await interaction.response.send_message(
                "The staff note could not be deleted. Please try again later.",
                ephemeral=True,
            )
            return

        if not changed:
            await interaction.response.send_message(
                "No active staff note with that ID was found in this server.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Staff note #{note_id} was soft-deleted.",
            ephemeral=True,
        )

    @staffnote.command(name="edit", description="Edit a staff note")
    @app_commands.describe(
        note_id="ID of the staff note to edit",
        note="Replacement staff note text",
    )
    @app_commands.guild_only()
    async def edit(
        self,
        interaction: discord.Interaction,
        note_id: app_commands.Range[int, 1],
        note: app_commands.Range[str, 1, MAX_NOTE_LENGTH],
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return

        try:
            cursor = await self.bot.db.execute(
                """
                SELECT created_by_id
                FROM staff_notes
                WHERE id = ?
                  AND guild_id = ?
                  AND is_deleted = 0
                """,
                (note_id, interaction.guild_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        except Exception as exc:
            self._log_database_error("edit_lookup", exc)
            await interaction.response.send_message(
                "The staff note could not be loaded. Please try again later.",
                ephemeral=True,
            )
            return

        if row is None:
            await interaction.response.send_message(
                "No active staff note with that ID was found in this server.",
                ephemeral=True,
            )
            return
        if not self._is_administrator(interaction) and row[0] != interaction.user.id:
            await interaction.response.send_message(
                "Only administrators or the original note author can edit this note.",
                ephemeral=True,
            )
            return

        updated_at = discord.utils.utcnow().isoformat()
        try:
            cursor = await self.bot.db.execute(
                """
                UPDATE staff_notes
                SET note = ?,
                    updated_at = ?
                WHERE id = ?
                  AND guild_id = ?
                  AND is_deleted = 0
                """,
                (
                    str(note).strip(),
                    updated_at,
                    note_id,
                    interaction.guild_id,
                ),
            )
            await cursor.close()
            await self.bot.db.commit()
        except Exception as exc:
            self._log_database_error("edit", exc)
            await interaction.response.send_message(
                "The staff note could not be updated. Please try again later.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Staff note #{note_id} was updated.",
            ephemeral=True,
        )

    @staffnote.command(
        name="summary",
        description="Show a concise non-AI summary of a member's notes",
    )
    @app_commands.describe(user="Member whose active notes to summarize")
    @app_commands.guild_only()
    async def summary(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ) -> None:
        if await self._deny_if_unauthorised(interaction):
            return

        try:
            count_cursor = await self.bot.db.execute(
                """
                SELECT COUNT(*), MIN(created_at), MAX(created_at)
                FROM staff_notes
                WHERE guild_id = ?
                  AND target_user_id = ?
                  AND is_deleted = 0
                """,
                (interaction.guild_id, user.id),
            )
            count_row = await count_cursor.fetchone()
            await count_cursor.close()
            notes_cursor = await self.bot.db.execute(
                """
                SELECT id, note, created_at
                FROM staff_notes
                WHERE guild_id = ?
                  AND target_user_id = ?
                  AND is_deleted = 0
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (interaction.guild_id, user.id),
            )
            recent_rows = await notes_cursor.fetchall()
            await notes_cursor.close()
        except Exception as exc:
            self._log_database_error("summary", exc)
            await interaction.response.send_message(
                "The staff note summary could not be loaded. Please try again later.",
                ephemeral=True,
            )
            return

        note_count = count_row[0] if count_row else 0
        if not note_count:
            await interaction.response.send_message(
                f"No active staff notes were found for {user.mention}.",
                ephemeral=True,
            )
            return

        recent_text = "\n".join(
            f"**#{row[0]} • {_display_date(row[2])}** — "
            f"{_truncate(row[1], 350)}"
            for row in recent_rows
        )
        embed = discord.Embed(
            title="Staff Note Summary",
            description=f"Member: {user.mention}\nActive notes: **{note_count}**",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Date range",
            value=(
                f"{_display_date(count_row[1])} to "
                f"{_display_date(count_row[2])}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Most recent notes",
            value=_truncate(recent_text, 1_024),
            inline=False,
        )
        embed.set_footer(
            text="Manual staff notes only. This summary does not use AI."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _build_note_embeds(
        user: discord.Member,
        rows: List[Any],
    ) -> List[discord.Embed]:
        embeds = []
        total_pages = (len(rows) + NOTES_PER_EMBED - 1) // NOTES_PER_EMBED
        for page_index in range(total_pages):
            page_rows = rows[
                page_index * NOTES_PER_EMBED:
                (page_index + 1) * NOTES_PER_EMBED
            ]
            embed = discord.Embed(
                title="Active Staff Notes",
                description=(
                    f"Member: {user.mention}\n"
                    f"Showing {len(rows)} newest active note(s), "
                    f"page {page_index + 1}/{total_pages}."
                ),
                color=discord.Color.blurple(),
            )
            for row in page_rows:
                author = _truncate(row[2], 80, f"User {row[3]}")
                edited = f" • edited {_display_date(row[5])}" if row[5] else ""
                embed.add_field(
                    name=_truncate(
                        f"#{row[0]} • {_display_date(row[4])} • {author}",
                        256,
                    ),
                    value=_truncate(row[1], 800) + edited,
                    inline=False,
                )
            embed.set_footer(
                text=(
                    "Private manual staff notes. "
                    f"At most the newest {MAX_VIEW_NOTES} are shown."
                )
            )
            embeds.append(embed)
        return embeds


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffNotes(bot))
