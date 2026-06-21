from __future__ import annotations

import asyncio
import hashlib
import io
import math
from typing import Optional

import discord
import requests
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from config import COLOR
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

        file = await asyncio.to_thread(
            self.create_leaderboard_banner,
            leaderboard,
            rows,
            page,
            total_entries,
        )
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

    def create_leaderboard_banner(
        self,
        title: str,
        data: list[tuple[int, float]],
        page: int,
        total_entries: int,
    ) -> discord.File:
        width = 760
        header_height = 130
        row_height = 72
        gap = 8
        padding = 26
        rows = max(1, len(data))
        height = header_height + padding + rows * (row_height + gap) + 26
        image = Image.new("RGB", (width, height), "#121419")
        draw = ImageDraw.Draw(image)
        title_font = ImageFont.truetype("assets/OpenSansEmoji.ttf", 34)
        subtitle_font = ImageFont.truetype("assets/calibri-regular.ttf", 18)
        name_font = ImageFont.truetype("assets/calibri-regular.ttf", 22)
        points_font = ImageFont.truetype("assets/calibri-regular.ttf", 23)
        rank_font = ImageFont.truetype("assets/calibri-regular.ttf", 20)

        draw.rounded_rectangle(
            (16, 16, width - 16, header_height),
            radius=22,
            fill="#1D2027",
        )
        draw.rounded_rectangle(
            (16, 16, 24, header_height),
            radius=4,
            fill=f"#{COLOR:06x}",
        )
        safe_title = title if len(title) <= 36 else title[:35] + "…"
        draw.text((44, 36), safe_title, font=title_font, fill="#F2F3F5")
        draw.text(
            (45, 86),
            f"{total_entries:,} ranked member(s) • Page {page + 1}",
            font=subtitle_font,
            fill="#AAB1BD",
        )

        if not data:
            draw.rounded_rectangle(
                (padding, header_height + padding, width - padding, height - 26),
                radius=18,
                fill="#1D2027",
            )
            empty = "No points have been awarded yet."
            text_width = draw.textlength(empty, font=name_font)
            draw.text(
                ((width - text_width) / 2, header_height + padding + 26),
                empty,
                font=name_font,
                fill="#AAB1BD",
            )

        rank_colors = {0: "#FFD166", 1: "#D6D9E0", 2: "#D9965B"}
        for index, (user_id, points) in enumerate(data):
            absolute_rank = page * PAGE_SIZE + index
            y = header_height + padding + index * (row_height + gap)
            fill = "#22262E" if index % 2 == 0 else "#1D2027"
            draw.rounded_rectangle(
                (padding, y, width - padding, y + row_height),
                radius=16,
                fill=fill,
            )
            rank_color = rank_colors.get(absolute_rank, "#D7DAE0")
            rank_text = f"#{absolute_rank + 1}"
            draw.text((44, y + 24), rank_text, font=rank_font, fill=rank_color)

            user = self.bot.get_user(user_id)
            name = user.display_name if user else f"User {user_id}"
            avatar_x = 106
            avatar_y = y + 11
            avatar_drawn = False
            if user is not None:
                try:
                    with requests.get(
                        user.display_avatar.url,
                        timeout=5,
                    ) as response:
                        response.raise_for_status()
                        with Image.open(io.BytesIO(response.content)) as source:
                            avatar = source.convert("RGBA").resize(
                                (50, 50),
                                Image.Resampling.LANCZOS,
                            )
                    mask = Image.new("L", (50, 50), 0)
                    ImageDraw.Draw(mask).ellipse((0, 0, 49, 49), fill=255)
                    avatar.putalpha(mask)
                    image.paste(avatar, (avatar_x, avatar_y), avatar)
                    avatar_drawn = True
                except (
                    requests.RequestException,
                    UnidentifiedImageError,
                    OSError,
                ):
                    pass
            if not avatar_drawn:
                draw.ellipse(
                    (avatar_x, avatar_y, avatar_x + 50, avatar_y + 50),
                    fill=f"#{COLOR:06x}",
                )
                initial = name[:1].upper() or "?"
                initial_width = draw.textlength(initial, font=rank_font)
                draw.text(
                    (avatar_x + (50 - initial_width) / 2, avatar_y + 14),
                    initial,
                    font=rank_font,
                    fill="#FFFFFF",
                )

            shown_name = name if len(name) <= 28 else name[:27] + "…"
            draw.text((176, y + 24), shown_name, font=name_font, fill="#F2F3F5")
            points_text = f"{round(points, 2):,} pts"
            points_width = draw.textlength(points_text, font=points_font)
            pill_left = width - padding - points_width - 34
            draw.rounded_rectangle(
                (pill_left, y + 17, width - padding - 12, y + 56),
                radius=18,
                fill="#303641",
            )
            draw.text(
                (pill_left + 16, y + 25),
                points_text,
                font=points_font,
                fill="#FFFFFF",
            )

        output = io.BytesIO()
        image.save(output, "PNG", optimize=True)
        output.seek(0)
        return discord.File(output, filename="leaderboard.png")

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
