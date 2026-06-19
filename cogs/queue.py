# -*- coding: utf-8 -*-

import asyncio
import io
from discord.ext import commands
import discord
from PIL import Image, ImageDraw, ImageFont
import requests

class Queue(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.history = {}

    async def cog_load(self):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER, user_id INTEGER)")
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS queue_lock (id INTEGER PRIMARY KEY)")
        await self.bot.db.commit()

    queue = discord.app_commands.Group(name="queue", description="Queue commands")

    @queue.command(name="dashboard", description="Create a Queue Dashboard")
    async def queue_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed, file = self.bot.get_embed()
        embed.description = f"Queue Dashboard is being sent!"
        await interaction.followup.send(embed=embed, file=file)

        await self.send_queue(interaction.channel)

    async def create_queue_dashboard(self, channel):
        embed, file = self.bot.get_embed()
        
        cur = await self.bot.db.execute("SELECT * FROM queue_lock WHERE id = ?", (channel.id,))
        lock = await cur.fetchone()
        
        embed.title = "QUEUE"
        if lock:
            embed.title += " (Locked)"

        embed.description = ""
        
        cur = await self.bot.db.execute("SELECT user_id FROM queue WHERE channel_id = ? ORDER BY id ASC", (channel.id,))
        res = await cur.fetchall()

        i=0
        for item in res:
            if user:= self.bot.get_user(item[0]):
                if i == 0:
                    file = await asyncio.to_thread(self.create_banner, user)
                i+=1
                embed.description += f"`{i}: `{user.mention}\n"
        
        embed.set_footer(text="!q = show | !qj = join | !ql - leave | !qn - pull | !qd = delay", icon_url=self.bot.user.display_avatar.url)
        
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Join", style=discord.ButtonStyle.green, custom_id="queue|join"))
        view.add_item(discord.ui.Button(label="Leave", style=discord.ButtonStyle.red, custom_id="queue|leave"))
        view.add_item(discord.ui.Button(label="Delay", style=discord.ButtonStyle.gray, custom_id="queue|delay"))
        view.add_item(discord.ui.Button(label="Pull", style=discord.ButtonStyle.blurple, custom_id="queue|pull"))

        return embed, file, view

    async def update_queue(self, interaction: discord.Interaction):
        embed, file, view = await self.create_queue_dashboard(interaction.channel)
        if embed.description.strip() != interaction.message.embeds[0].description:
            self.history[interaction.channel.id] = await interaction.edit_original_response(embed=embed, attachments=[file], view=view)
        
    async def send_queue(self, channel, content: str = None):
        try:
            if channel.id in self.history:
                await self.history[channel.id].delete()
        except:
            pass
            
        embed, file, view = await self.create_queue_dashboard(channel)
        message = await channel.send(content=content, embed=embed, file=file, view=view)
        self.history[channel.id] = message

    def create_banner(self, user: discord.Member):
        background = Image.open('assets/up_next.png').convert("RGBA")
        avatar = Image.open(requests.get(user.avatar.url, stream=True).raw).convert("RGBA")
        avatar = avatar.resize((167, 167))

        mask = Image.new("L", avatar.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, 167, 167), fill=255)
        avatar.putalpha(mask)

        background.paste(avatar, (667, 45), avatar)

        draw = ImageDraw.Draw(background)
        font = ImageFont.truetype("assets/calibri.ttf", 40)
        draw.text((120, 150), f"@{user.display_name}", font=font, fill=(255, 255, 255))
        with io.BytesIO() as image_binary:
            background.save(image_binary, 'PNG')
            image_binary.seek(0)
            file = discord.File(fp=image_binary, filename='bar.gif')
            
        return file
    
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            if interaction.data['custom_id'].startswith("queue|"):
                await interaction.response.defer()
                _, action = interaction.data['custom_id'].split("|")
                embed, file = self.bot.get_embed()
                cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? AND user_id = ?", (interaction.channel.id, interaction.user.id,))
                res = await cur.fetchone()
                if action == "join":
                    cur = await self.bot.db.execute("SELECT * FROM queue_lock WHERE id = ?", (interaction.channel.id,))
                    lock = await cur.fetchone()
                    if lock:
                        embed.description = f"This queue is locked for now!"
                        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
                        return
                    if not res:
                        if interaction.user.voice and interaction.user.voice.channel == interaction.channel:
                            await self.bot.db.execute("INSERT INTO queue (channel_id, user_id) VALUES (?, ?)", (interaction.channel.id, interaction.user.id))
                            await self.bot.db.commit()
                            embed.description = f"You have successfully joined the queue!"
                        else:
                            embed.description = f"You must be in queue voice channel to join the queue!"
                    else:
                        embed.description = f"You are already in the queue!"
                    await interaction.followup.send(embed=embed, file=file, ephemeral=True)
                elif action == "leave":
                    if res:
                        await self.bot.db.execute("DELETE FROM queue WHERE channel_id = ? AND user_id = ?", (interaction.channel.id, interaction.user.id))
                        await self.bot.db.commit()
                        embed.description = f"You have successfully left the queue!"
                    else:
                        embed.description = f"You are not in the queue!"
                    await interaction.followup.send(embed=embed, file=file, ephemeral=True)
                elif action == "delay":
                    if res:
                        await self.move_user(interaction.user, 'drop', interaction.channel.id)
                        embed.description = f"You have successfully delayed your position in the queue!"
                    else:
                        embed.description = f"You are not in the queue!"
                    await interaction.followup.send(embed=embed, file=file, ephemeral=True)
                elif action == "pull":
                    return await self.pull_from_queue(interaction.user, interaction.channel)
                
                await self.update_queue(interaction)

    @queue.command(name='lock', description='Lock the queue')
    async def queue_lock(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.bot.db.execute("INSERT OR REPLACE INTO queue_lock (id) VALUES (?)", (interaction.channel.id,))
        await self.bot.db.commit()
        embed, file = self.bot.get_embed()
        embed.description = f"Queue for {interaction.channel.mention} is now locked!"
        await interaction.followup.send(embed=embed, file=file)
    
    @queue.command(name='unlock', description='Unlock the queue')
    async def queue_unlock(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.bot.db.execute("DELETE FROM queue_lock WHERE id = ?", (interaction.channel.id,))
        await self.bot.db.commit()
        embed, file = self.bot.get_embed()
        embed.description = f"Queue for {interaction.channel.mention} is now unlocked!"
        await interaction.followup.send(embed=embed, file=file)

    @queue.command(name='move', description='Move a user in the queue')
    async def queue_move(self, interaction: discord.Interaction, user: discord.User, position: int):
        await interaction.response.defer()
        position = abs(position)
        embed, file = self.bot.get_embed()
        cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? AND user_id = ?", (interaction.channel.id, user.id,))
        res = await cur.fetchone()
        if not res:
            embed.description = f"{user.mention} not found in the queue!"
            return await interaction.followup.send(embed=embed, file=file, ephemeral=True)

        await self.move_user(user, position, interaction.channel.id)
        embed.description = f"Moved {user.mention} to position {position} in the queue!"
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        await self.send_queue(interaction.channel)
    
    async def move_user(self, user, position, channel_id):
        cur = await self.bot.db.execute("SELECT user_id FROM queue WHERE channel_id = ? ORDER BY id ASC", (channel_id,))
        res = await cur.fetchall()
        users = [item[0] for item in res]
        if position == 'drop':
            current = users.index(user.id)
            users.remove(user.id)
            users.insert(current+1, user.id)
        else:
            users.remove(user.id)        
            users.insert(position-1, user.id)

        await self.bot.db.execute("DELETE FROM queue WHERE channel_id = ?", (channel_id,))
        for user_id in users:
            await self.bot.db.execute("INSERT INTO queue (channel_id, user_id) VALUES (?, ?)", (channel_id, user_id,))        
        await self.bot.db.commit()

    @queue.command(name='remove', description='Remove a user from the queue')
    async def queue_remove(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer()
        embed, file = self.bot.get_embed()
        await self.bot.db.execute("DELETE FROM queue WHERE channel_id = ? AND user_id = ?", (interaction.channel.id, user.id,))
        await self.bot.db.commit()
        embed.description = f"{user.mention} has been removed from the queue!"
        await interaction.followup.send(embed=embed, file=file, ephemeral=True)
        await self.send_queue(interaction.channel)
    
    @commands.command(name='q', description='Queue Dashboard')
    async def queue_command(self, ctx: commands.Context):
        await ctx.message.add_reaction("✅")
        await self.send_queue(ctx.channel)

    @commands.command(name='qj', description='Queue Join')
    async def queue_join(self, ctx: commands.Context):
        cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? AND user_id = ?", (ctx.channel.id, ctx.author.id,))
        res = await cur.fetchone()
        if not res:
            if ctx.author.voice and ctx.author.voice.channel == ctx.channel:
                await self.bot.db.execute("SELECT * FROM queue_lock WHERE id = ?", (ctx.channel.id,))
                lock = await cur.fetchone()
                if not lock:
                    await self.bot.db.execute("INSERT OR IGNORE INTO queue (channel_id, user_id) VALUES (?, ?)", (ctx.channel.id, ctx.author.id))
                    await self.bot.db.commit()
                    await ctx.message.add_reaction("✅")
                    await self.send_queue(ctx.channel, content=f"{ctx.author.mention} has joined the queue!")
                else:
                    await ctx.message.add_reaction("🔒")
            else:
                await ctx.message.add_reaction("❌")
                await ctx.message.add_reaction("🔇")
        else:
            await ctx.message.add_reaction("❌")
            await ctx.message.add_reaction("⏳")
    
    @commands.command(name='ql', description='Queue Leave')
    async def queue_leave(self, ctx: commands.Context):
        cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? AND user_id = ?", (ctx.channel.id, ctx.author.id,))
        res = await cur.fetchone()
        if res:
            await self.bot.db.execute("DELETE FROM queue WHERE channel_id = ? AND user_id = ?", (ctx.channel.id, ctx.author.id))
            await self.bot.db.commit()
            await ctx.message.add_reaction("✅")
            await self.send_queue(ctx.channel, content=f"{ctx.author.mention} has left the queue!")
        else:
            await ctx.message.add_reaction("❌")

    @commands.command(name='qd', description='Queue Drop 1 Place')
    async def queue_drop(self, ctx: commands.Context):
        cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? AND user_id = ?", (ctx.channel.id, ctx.author.id,))
        res = await cur.fetchone()
        if res:
            await self.move_user(ctx.author, 'drop', ctx.channel.id)
            await ctx.message.add_reaction("✅")
            await self.send_queue(ctx.channel, content=f"{ctx.author.mention} has dropped 1 place in the queue!")
        else:
            await ctx.message.add_reaction("❌")

    @commands.command(name='qn', description='Queue Next')
    async def queue_next(self, ctx: commands.Context):
        await ctx.message.add_reaction("✅")
        await self.pull_from_queue(ctx.author, ctx.channel)

    async def pull_from_queue(self, by, channel):
        cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? ORDER BY id ASC LIMIT 1", (channel.id,))
        res = await cur.fetchone()
        if res:
            id, channel_id, user_id = res
            user = channel.guild.get_member(user_id)
            await self.bot.db.execute("DELETE FROM queue WHERE channel_id = ? AND id = ?", (channel.id, id,))
            await self.bot.db.commit()
            cur = await self.bot.db.execute("SELECT * FROM queue WHERE channel_id = ? ORDER BY id ASC LIMIT 1", (channel.id,))
            next_res = await cur.fetchone()

            embed, file = self.bot.get_embed()
            embed.description = f"<@{user_id}> was pulled by {by.mention} from the queue!"
            await channel.send(embed=embed, file=file)

            content = f"It is now <@{user_id}> turn."
            if next_res:
                content += f" Next in queue: <@{next_res[2]}>!"
            if not (user and user.voice and user.voice.channel == channel):
                content += f"\n⚠️ {f'<@{user_id}>'} is not in the voice channel!"
            await self.send_queue(channel, content=content)

async def setup(bot):
    await bot.add_cog(Queue(bot))