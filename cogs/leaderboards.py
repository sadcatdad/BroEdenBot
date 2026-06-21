from __future__ import annotations

import hashlib
import io
import math
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR
from utils.ranked_graphic import (
    RankedGraphicItem,
    RankedGraphicSection,
    render_ranked_graphic,
)
from utils.ui import error_embed, success_embed, warning_embed


PAGE_SIZE = 10
MAX_LEADERBOARD_NAME = 50


def parse_points(value: str) -> Optional[float]:
    try:
        points = round(abs(float(value)), 2)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(points) or points <= 0:
        return None
    return points


def normalize_leaderboard_name(value: str) -> Optional[str]:
    name = " ".join(value.split())
    if not name or len(name) > MAX_LEADERBOARD_NAME:
        return None
    return name


def can_manage_leaderboards(member: object) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (permissions.administrator or permissions.manage_guild)
    )


class Leaderboards(commands.Cog):
    leaderboard = app_commands.Group(
        name="leaderboard",
        description="Leaderboard management",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            "CREATE TABLE IF NOT EXISTS leaderboards (name TEXT PRIMARY KEY)"
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER,
                leaderboard TEXT,
                points REAL,
                PRIMARY KEY (id, leaderboard)
            )
            """
        )
        await self.bot.db.commit()

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        if can_manage_leaderboards(interaction.user):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Staff control",
                "You need **Manage Server** to change leaderboards.",
            ),
            ephemeral=True,
        )
        return False

    @leaderboard.command(name="create", description="Create a leaderboard")
    @app_commands.default_permissions(manage_guild=True)
    async def create_leaderboard(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_LEADERBOARD_NAME],
    ) -> None:
        if not await self._require_manager(interaction):
            return
        normalized = normalize_leaderboard_name(str(name))
        if normalized is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid name",
                    f"Use a name between 1 and {MAX_LEADERBOARD_NAME} characters.",
                ),
                ephemeral=True,
            )
            return
        cursor = await self.bot.db.execute(
            "INSERT OR IGNORE INTO leaderboards (name) VALUES (?)",
            (normalized,),
        )
        await self.bot.db.commit()
        created = cursor.rowcount > 0
        await cursor.close()
        embed = (
            success_embed(
                "Leaderboard created",
                f"`{discord.utils.escape_markdown(normalized)}` is ready.",
            )
            if created
            else warning_embed(
                "Already exists",
                f"`{discord.utils.escape_markdown(normalized)}` already exists.",
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @leaderboard.command(name="delete", description="Delete a leaderboard")
    @app_commands.default_permissions(manage_guild=True)
    async def delete_leaderboard(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        cursor = await self.bot.db.execute(
            "DELETE FROM leaderboards WHERE name = ?",
            (name,),
        )
        changed = cursor.rowcount
        await cursor.close()
        if changed:
            await self.bot.db.execute(
                "DELETE FROM points WHERE leaderboard = ?",
                (name,),
            )
            await self.bot.db.commit()
            embed = success_embed(
                "Leaderboard deleted",
                f"`{discord.utils.escape_markdown(name)}` and its scores were removed.",
            )
        else:
            embed = warning_embed(
                "Not found",
                f"`{discord.utils.escape_markdown(name)}` does not exist.",
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="leaderboards",
        description="Show a polished leaderboard",
    )
    async def show_leaderboard(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await interaction.response.defer(thinking=True)
        if not await self._leaderboard_exists(name):
            await interaction.followup.send(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(name)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        file, view = await self.get_leaderboard_banner(name, 0)
        await interaction.followup.send(file=file, view=view)

    @leaderboard.command(name="add", description="Add points to a member")
    @app_commands.default_permissions(manage_guild=True)
    async def add_points(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        user: discord.Member,
        points: str,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        parsed_points = parse_points(points)
        if parsed_points is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid points",
                    "Enter a positive finite number greater than zero.",
                ),
                ephemeral=True,
            )
            return
        if user.bot:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bots are excluded",
                    "Points can only be assigned to community members.",
                ),
                ephemeral=True,
            )
            return
        if not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.execute(
            """
            INSERT INTO points (id, leaderboard, points)
            VALUES (?, ?, ?)
            ON CONFLICT (id, leaderboard)
            DO UPDATE SET points = points + excluded.points
            """,
            (user.id, leaderboard, parsed_points),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(
                "Points added",
                f"Added **{parsed_points:,}** to {user.mention} in "
                f"`{discord.utils.escape_markdown(leaderboard)}`.",
            ),
            ephemeral=True,
        )

    @leaderboard.command(name="remove", description="Remove points from a member")
    @app_commands.default_permissions(manage_guild=True)
    async def remove_points(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        user: discord.Member,
        points: str,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        parsed_points = parse_points(points)
        if parsed_points is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid points",
                    "Enter a positive finite number greater than zero.",
                ),
                ephemeral=True,
            )
            return
        if user.bot:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bots are excluded",
                    "Points can only be removed from community members.",
                ),
                ephemeral=True,
            )
            return
        if not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            """
            INSERT INTO points (id, leaderboard, points)
            VALUES (?, ?, 0)
            ON CONFLICT(id, leaderboard)
            DO UPDATE SET points = MAX(0, points - ?)
            """,
            (user.id, leaderboard, parsed_points),
        )
        await self.bot.db.commit()
        await interaction.response.send_message(
            embed=success_embed(
                "Points removed",
                f"Removed **{parsed_points:,}** from {user.mention} in "
                f"`{discord.utils.escape_markdown(leaderboard)}`.",
            ),
            ephemeral=True,
        )

    async def _leaderboard_exists(self, name: str) -> bool:
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM leaderboards WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    @delete_leaderboard.autocomplete("name")
    @show_leaderboard.autocomplete("name")
    @add_points.autocomplete("leaderboard")
    @remove_points.autocomplete("leaderboard")
    async def leaderboard_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        cursor = await self.bot.db.execute(
            """
            SELECT name
            FROM leaderboards
            WHERE lower(name) LIKE ?
            ORDER BY name COLLATE NOCASE
            LIMIT 25
            """,
            (f"%{current.casefold().strip()}%",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            app_commands.Choice(name=row[0], value=row[0])
            for row in rows
        ]

    @staticmethod
    def _token(name: str) -> str:
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

    async def _name_from_token(self, token: str) -> Optional[str]:
        cursor = await self.bot.db.execute("SELECT name FROM leaderboards")
        rows = await cursor.fetchall()
        await cursor.close()
        for (name,) in rows:
            if self._token(name) == token:
                return name
        return None

    async def get_leaderboard_banner(
        self,
        leaderboard: str,
        page: int,
    ) -> tuple[discord.File, discord.ui.View]:
        cursor = await self.bot.db.execute(
            "SELECT COUNT(*) FROM points WHERE leaderboard = ? AND points > 0",
            (leaderboard,),
        )
        total_entries = int((await cursor.fetchone())[0])
        await cursor.close()
        total_pages = max(1, math.ceil(total_entries / PAGE_SIZE))
        page = min(max(0, page), total_pages - 1)
        cursor = await self.bot.db.execute(
            """
            SELECT id, points
            FROM points
            WHERE leaderboard = ? AND points > 0
            ORDER BY points DESC, id ASC
            LIMIT ? OFFSET ?
            """,
            (leaderboard, PAGE_SIZE, PAGE_SIZE * page),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        items = []
        for user_id, points in rows:
            user = self.bot.get_user(user_id)
            formatted_points = f"{points:,.2f}".rstrip("0").rstrip(".")
            items.append(
                RankedGraphicItem(
                    label=(
                        user.display_name
                        if user is not None
                        else f"User {user_id}"
                    ),
                    value=f"{formatted_points} pts",
                    subtitle=(
                        f"@{user.name}"
                        if user is not None
                        else f"Discord ID {user_id}"
                    ),
                    avatar_url=(
                        str(user.display_avatar.replace(size=64).url)
                        if user is not None
                        else None
                    ),
                    score=float(points),
                )
            )
        png = await render_ranked_graphic(
            title=leaderboard,
            subtitle=f"Page {page + 1} of {total_pages}",
            sections=[
                RankedGraphicSection(
                    "Member leaderboard",
                    items,
                    rank_start=page * PAGE_SIZE + 1,
                )
            ],
            updated_at=datetime.now(timezone.utc),
            accent_color=COLOR,
            total_entries=total_entries,
        )
        file = discord.File(io.BytesIO(png), filename="leaderboard.png")
        token = self._token(leaderboard)
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Previous",
                emoji="◀️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"leaderboard|prev|{page}|{token}",
                disabled=page == 0,
            )
        )
        view.add_item(
            discord.ui.Button(
                label=f"Page {page + 1} of {total_pages}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"leaderboard|page|{page}|{token}",
                disabled=True,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Next",
                emoji="▶️",
                style=discord.ButtonStyle.primary,
                custom_id=f"leaderboard|next|{page}|{token}",
                disabled=page + 1 >= total_pages,
            )
        )
        return file, view

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        if not custom_id.startswith("leaderboard|"):
            return
        parts = custom_id.split("|")
        if (
            len(parts) != 4
            or parts[1] not in {"prev", "next"}
            or not parts[2].isdigit()
        ):
            return
        name = await self._name_from_token(parts[3])
        if name is None:
            await interaction.response.send_message(
                "This leaderboard is no longer available.",
                ephemeral=True,
            )
            return
        page = int(parts[2]) + (1 if parts[1] == "next" else -1)
        await interaction.response.defer()
        file, view = await self.get_leaderboard_banner(name, page)
        await interaction.edit_original_response(attachments=[file], view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboards(bot))
