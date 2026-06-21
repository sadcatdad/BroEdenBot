# -*- coding: utf-8 -*-

import asyncio
import io
import math
from discord.ext import commands
import discord
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
import requests
from io import BytesIO

from config import COLOR


class Leaderboards(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS leaderboards (name PRIMARY KEY)")
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS points (id INTEGER, leaderboard TEXT, points REAL, PRIMARY KEY (id, leaderboard))")
        await self.bot.db.commit()
    
    leaderboard = discord.app_commands.Group(name="leaderboard", description="Leaderboard commands")

    @staticmethod
    def parse_points(value: str):
        try:
            points = round(abs(float(value)), 2)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(points) or points <= 0:
            return None
        return points

    @leaderboard.command(name="create", description="Create a leaderboard")
    async def create_leaderboard(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        await self.bot.db.execute("INSERT OR REPLACE INTO leaderboards (name) VALUES (?)", (name,))
        await self.bot.db.commit()
        embed = discord.Embed(color=COLOR)
        embed.description = f"Leaderboard `{name}` successfully created."
        await interaction.followup.send(embed=embed)
    
    @leaderboard.command(name="delete", description="Delete a leaderboard")
    async def delete_leaderboard(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        embed = discord.Embed(color=COLOR)
        cur = await self.bot.db.execute("SELECT name FROM leaderboards WHERE name = ?", (name,))
        res = await cur.fetchone()
        if res:
            await self.bot.db.execute("DELETE FROM leaderboards WHERE name = ?", (name,))
            await self.bot.db.execute("DELETE FROM points WHERE leaderboard = ?", (name,))
            await self.bot.db.commit()
            embed.description = f"Leaderboard `{name}` successfully deleted."
        else:
            embed.description = f"Leaderboard `{name}` does not exist."
        await interaction.followup.send(embed=embed)
    
    @discord.app_commands.command(name="leaderboards", description="Show a leaderboard")
    async def show_leaderboard(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        cur = await self.bot.db.execute("SELECT name FROM leaderboards WHERE name = ?", (name,))
        res = await cur.fetchone()
        if res:
            file, view = await self.get_leaderboard_banner(name, 0)
            await interaction.followup.send(file=file, view=view)
        else:
            embed = discord.Embed(color=COLOR)
            embed.description = f"Leaderboard `{name}` does not exist."
            await interaction.followup.send(embed=embed)

    @leaderboard.command(name="add", description="Add points to a user in a leaderboard")
    async def add_points(self, interaction: discord.Interaction, leaderboard: str, user: discord.User, points: str):
        await interaction.response.defer()
        embed = discord.Embed(color=COLOR)
        points = self.parse_points(points)
        if points is None:
            embed.description = "Invalid points value. Please provide a valid number."
            await interaction.followup.send(embed=embed)
            return
        
        if user.bot:
            embed.description = "You cannot add points to a bot."
            await interaction.followup.send(embed=embed)
            return
        
        cur = await self.bot.db.execute("SELECT name FROM leaderboards WHERE name = ?", (leaderboard,))
        res = await cur.fetchone()
        if res:
            await self.bot.db.execute(
                """
                INSERT INTO points (id, leaderboard, points)
                VALUES (?, ?, ?)
                ON CONFLICT (id, leaderboard)
                DO UPDATE SET points = points + excluded.points
                """,
                (user.id, leaderboard, points),
            )
            await self.bot.db.commit()
            embed.description = f"Added {points:,} points to {user.mention} in leaderboard `{leaderboard}`."
        else:
            embed.description = f"Leaderboard `{leaderboard}` does not exist."
        await interaction.followup.send(embed=embed)
    
    @leaderboard.command(name="remove", description="Remove points from a user in a leaderboard")
    async def remove_points(self, interaction: discord.Interaction, leaderboard: str, user: discord.User, points: str):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(color=COLOR)
        points = self.parse_points(points)
        if points is None:
            embed.description = "Invalid points value. Please provide a valid number."
            await interaction.followup.send(embed=embed)
            return
        
        if user.bot:
            embed.description = "You cannot remove points from a bot."
            await interaction.followup.send(embed=embed)
            return

        cur = await self.bot.db.execute("SELECT name FROM leaderboards WHERE name = ?", (leaderboard,))
        res = await cur.fetchone()
        if res:
            await self.bot.db.execute("INSERT INTO points (id, leaderboard, points) VALUES (?, ?, 0) ON CONFLICT(id, leaderboard) DO UPDATE SET points = MAX(0, points - ?)", (user.id, leaderboard, points))
            await self.bot.db.commit()
            embed.description = f"Removed {points:,} points from {user.mention} in leaderboard `{leaderboard}`."
        else:
            embed.description = f"Leaderboard `{leaderboard}` does not exist."
        await interaction.followup.send(embed=embed)
    
    @delete_leaderboard.autocomplete("name")
    @show_leaderboard.autocomplete("name")
    @add_points.autocomplete("leaderboard")
    @remove_points.autocomplete("leaderboard")
    async def show_leaderboard_autocomplete(self, interaction: discord.Interaction, current: str):
        await interaction.response.defer(ephemeral=True)
        cur = await self.bot.db.execute("SELECT name FROM leaderboards WHERE lower(name) LIKE ? LIMIT 25", (f"%{current.lower().strip()}%",))
        res = await cur.fetchall()
        return [discord.app_commands.Choice(name=item[0], value=item[0]) for item in res]
    
    async def get_leaderboard_banner(self, leaderboard: str, page: int):
        cur = await self.bot.db.execute("SELECT COUNT(*) FROM points WHERE leaderboard = ?", (leaderboard,))
        res = await cur.fetchone()
        total_pages = max(1, -(-res[0] // 10))
        page = min(max(0, page), total_pages - 1)

        cur = await self.bot.db.execute("SELECT id, points FROM points WHERE leaderboard = ? ORDER BY points DESC LIMIT ?, 10", (leaderboard, 10*page,))
        res = await cur.fetchall()

        file = await asyncio.to_thread(self.create_leaderboard_banner, leaderboard, res, page)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Prev", style=discord.ButtonStyle.gray, custom_id=f"leaderboard|prev|{leaderboard}|{page}", disabled=page == 0))
        view.add_item(discord.ui.Button(label=f"{page+1}/{total_pages}", style=discord.ButtonStyle.gray, custom_id=f"lll", disabled=True))
        view.add_item(discord.ui.Button(label="Next", style=discord.ButtonStyle.gray, custom_id=f"leaderboard|next|{leaderboard}|{page}", disabled=page + 1 >= total_pages))

        return file, view
    
    def create_leaderboard_banner(self, title: str, data, page: int):
        def get_rank_color(rank):
            if rank == 0:
                return "#FFD700"
            elif rank == 1:
                return "#C0C0C0"
            elif rank == 2:
                return "#CD7F32"
            return "white"
        
        width = 500
        row_height = 70
        spacing = 10
        header_height = 80
        bar_width = 6
        content_rows = max(1, min(len(data), 10))
        height = header_height + (row_height + spacing) * content_rows

        background_color = "#1E1E1E"
        img = Image.new("RGB", (width, height), background_color)
        draw = ImageDraw.Draw(img)

        font_title = ImageFont.truetype("assets/OpenSansEmoji.ttf", 26, encoding='unic')
        font_name = ImageFont.truetype("assets/calibri-regular.ttf", 20)
        font_points = ImageFont.truetype("assets/calibri-regular.ttf", 22)

        draw.rectangle([0, 0, width, header_height], fill="#131416")
        _, _, w, _ = draw.textbbox((0, 0), title, font=font_title)
        draw.text(((width - w) / 2, 25), title, font=font_title, fill="white")

        if not data:
            empty_text = "No points yet."
            _, _, empty_width, _ = draw.textbbox(
                (0, 0),
                empty_text,
                font=font_name,
            )
            draw.text(
                ((width - empty_width) / 2, header_height + 30),
                empty_text,
                font=font_name,
                fill="#B9BBBE",
            )

        i=10*page
        for index, item in enumerate(data):
            user_id, points = item
            user = self.bot.get_user(user_id)
            if user:
                y = header_height + index * (row_height + spacing)

                draw.rectangle([0, y, width, y + row_height], fill="#2C2F33")

                rank_color = get_rank_color(i)
                draw.rectangle([0, y, bar_width, y + row_height], fill=rank_color)
                draw.text((bar_width + 10, y + 25), f"#{i+1}", font=font_name, fill=rank_color)

                avatar_position = (
                    bar_width + 60,
                    y + (row_height - 40) // 2,
                )
                try:
                    with requests.get(
                        user.display_avatar.url,
                        timeout=10,
                    ) as response:
                        response.raise_for_status()
                        avatar = Image.open(
                            BytesIO(response.content)
                        ).convert("RGBA")
                    avatar = avatar.resize((40, 40), Image.LANCZOS)
                    img.paste(avatar, avatar_position, avatar)
                except (
                    requests.RequestException,
                    UnidentifiedImageError,
                    OSError,
                ):
                    left, top = avatar_position
                    draw.ellipse(
                        [left, top, left + 40, top + 40],
                        fill="#5865F2",
                    )
                
                draw.text((bar_width + 110, y + 25), f"{user.display_name}", font=font_name, fill=rank_color)
                points = f"{round(points, 2):,}"
                x_, y_, w, h = draw.textbbox((0, 0), points, font=font_points)        
                draw.text((width - 50 - w, y + 25), points, font=font_points, fill=rank_color)
                i+=1

        with io.BytesIO() as image_binary:
            img.save(image_binary, 'PNG')
            image_binary.seek(0)
            file = discord.File(fp=image_binary, filename='bar.gif')
        
        return file

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component and interaction.data['custom_id'].startswith("leaderboard|"):
            await interaction.response.defer()
            _, action, title, current_page = interaction.data['custom_id'].split("|")
            current_page = (int(current_page) + 1) if action == "next" else (int(current_page) - 1)
            file, view = await self.get_leaderboard_banner(title, current_page)
            await interaction.edit_original_response(attachments=[file], view=view)
            

async def setup(bot):
    await bot.add_cog(Leaderboards(bot))
