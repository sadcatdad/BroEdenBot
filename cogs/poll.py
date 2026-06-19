# -*- coding: utf-8 -*-

import asyncio
import datetime
import re
from discord.ext import commands, tasks
import discord

from config import COLOR

EMOJIS = ["🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭", "🇮", "🇯", "🇰", "🇱", "🇲", "🇳", "🇴", "🇵", "🇶", "🇷", "🇸", "🇹", "🇺", "🇻", "🇼", "🇽", "🇾", "🇿"]

class Poll(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_manager.start()

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS poll (title TEXT, options TEXT, endtime TEXT, channel INTEGER, msg INTEGER)")
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS poll_votes (id INTEGER, user_id INTEGER, vote TEXT, PRIMARY KEY (id, user_id))")
        await self.bot.db.commit()

    def generate_bar(self, current, total):
        black = '⬛'
        blue = '🟦'
        length = 8

        percent = min(max(current / max(total, 1), 0), 1)
        filled_blocks = round(percent * length)
        empty_blocks = length - filled_blocks
        return blue * filled_blocks + black * empty_blocks

    @discord.app_commands.command(name="poll", description="Create a poll")
    @discord.app_commands.describe(question="Poll question", options="Poll options (comma separated)", time="Poll duration (e.g., 1h, 30m)")
    async def poll(self, interaction: discord.Interaction, question: str, options: str, time: str):
        await interaction.response.defer(ephemeral=True)
        embed, file = self.bot.get_embed()
        seconds = self.parse_time_input(time)
        if seconds == 0:
            embed.description = "Invalid time format. Please use a valid format like 1h, 30m, etc."
            await interaction.followup.send(embed=embed, file=file)
            return
        
        options = options.split(",")
        if len(options) > 26:
            embed.description = "You can only have up to 26 options."
            await interaction.followup.send(embed=embed, file=file)
            return
        
        end = self.bot.get_time() + datetime.timedelta(seconds=seconds)

        embed.description = "Poll is being created..."
        await interaction.followup.send(embed=embed, file=file)
    
        embed = discord.Embed(color=COLOR)
        embed.description = f"Poll is being loaded..."
        poll = await interaction.channel.send(embed=embed)

        poll_data = (question, str(options), end, interaction.channel.id, poll.id)
        await self.bot.db.execute("INSERT INTO poll (title, options, endtime, channel, msg) VALUES (?, ?, ?, ?, ?)", poll_data)
        await self.bot.db.commit()

        embed, file, view = await self.create_poll(poll_data)
        await poll.edit(embed=embed, attachments=[file], view=view)

        if not self.poll_manager.is_running():
            self.poll_manager.start()
        else:
            self.poll_manager.restart()
        
    @tasks.loop(count=1)
    async def poll_manager(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(5)
        cur = await self.bot.db.execute("SELECT * FROM poll ORDER BY endtime ASC")
        res = await cur.fetchall()
        for item in res:
            title, options, endtime, channel_id, msg_id = item
            end = datetime.datetime.fromisoformat(endtime)
            await discord.utils.sleep_until(end)
            if channel:= self.bot.get_channel(channel_id):
                try:
                    poll = await channel.fetch_message(msg_id)
                    await poll.delete()
                except:
                    pass
                
                embed, file, view = await self.create_poll(item)
                await channel.send(embed=embed, file=file, view=view)

            await self.bot.db.execute("DELETE FROM poll WHERE msg = ?", (msg_id,))
            await self.bot.db.commit()
            await asyncio.sleep(2)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component and interaction.data['custom_id'].startswith("poll"):
            await interaction.response.defer()
            cur = await self.bot.db.execute("SELECT * FROM poll WHERE msg = ?", (interaction.message.id,))
            res = await cur.fetchone()
            if res:
                title, options, endtime, channel_id, msg_id = res
                vote = interaction.data['custom_id'].split("|||")[1]
                await self.bot.db.execute("INSERT OR REPLACE INTO poll_votes (id, user_id, vote) VALUES (?, ?, ?)", (msg_id, interaction.user.id, vote,))
                await self.bot.db.commit()
                embed, file = self.bot.get_embed()
                embed.description = f"Your vote has been recorded successfully."
                msg = await interaction.followup.send(embed=embed, file=file, ephemeral=True)
                await asyncio.sleep(5)
                await msg.delete()

    async def create_poll(self, res):
        title, options, endtime, channel_id, msg_id = res
        options = eval(options)
        end = datetime.datetime.fromisoformat(str(endtime))
        now = self.bot.get_time()
        embed = discord.Embed(color=COLOR)
        embed.title = title
        embed.set_image(url="attachment://poll.png")
        if end > now:
            embed.description = f"This poll ends <t:{int(end.timestamp())}:R>\n"
            file = discord.File(fp="assets/votenow.png", filename="poll.png")
        else:
            embed.description = f"# RESULTS ARE IN!"
            file = discord.File(fp="assets/results.png", filename="poll.png")

        cur = await self.bot.db.execute("SELECT COUNT(*) FROM poll_votes WHERE id = ?", (msg_id,))
        res = await cur.fetchone()
        total = res[0]

        if end > now:
            for i, option in enumerate(options):
                embed.description += f"\n{EMOJIS[i]} {option.strip()}"
        else:
            for i, option in enumerate(options):
                cur = await self.bot.db.execute("SELECT COUNT(*) FROM poll_votes WHERE id = ? AND vote = ?", (msg_id, option,))
                res = await cur.fetchone()
                count = res[0]
                embed.add_field(name=f"{EMOJIS[i]} {option.strip()}", value=f"{self.generate_bar(count, total)} ({count:,})", inline=False)
                    
        view = discord.ui.View(timeout=None)
        for i, option in enumerate(options):
            view.add_item(discord.ui.Button(label=option, custom_id=f"poll|||{option}", style=discord.ButtonStyle.gray, disabled=True if end <= now else False))
        return embed, file, view
            

    def parse_time_input(self, input_str):
        pattern = re.compile(r'(\d+)*(s|sec|second|m|min|minute|h|hour|d|day|week|month|year)s*', re.IGNORECASE)
        matches = pattern.findall(input_str)
        time_units = {'s': 0, 'sec': 0, 'second': 0, 'm': 0, 'min': 0, 'minute': 0,'h': 0,  'hour': 0, 'day': 0, 'd': 0, 'week': 0, 'month': 0, 'year': 0}
        for value, unit in matches:
            time_units[unit] += int(value)
    
        total_seconds = (
            time_units.get('year', 0) * 365 * 24 * 60 * 60 +
            time_units.get('month', 0) * 30 * 24 * 60 * 60 +
            time_units.get('week', 0) * 7 * 24 * 60 * 60 +
            time_units.get('day', 0) * 24 * 60 * 60 +
            time_units.get('d', 0) * 24 * 60 * 60 +
            time_units.get('hour', 0) * 60 * 60 +
            time_units.get('h', 0) * 60 * 60 +
            time_units.get('minute', 0) * 60 +
            time_units.get('min', 0) * 60 +
            time_units.get('m', 0) * 60 +
            time_units.get('second', 0) +
            time_units.get('sec', 0) +
            time_units.get('s', 0)
        )
    
        return total_seconds


async def setup(bot):
    await bot.add_cog(Poll(bot))