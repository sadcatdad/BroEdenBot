import datetime
import os
import traceback
import aiosqlite
import discord
from discord.ext import commands
from config import COLOR, TOKEN

intents = discord.Intents.all()

class BotClient(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix='!',
            intents=intents
        )

    async def setup_hook(self):
        await self.load_data()
        await self.load_all_cogs()
        await self.tree.sync()

    async def on_ready(self):
        print(f"{self.user} is ready.")
        permissions = discord.Permissions(administrator=True)
        scopes = ['bot', 'applications.commands']
        invite_url = discord.utils.oauth_url(self.user.id, permissions=permissions, scopes=scopes)
        print(f"Invite the bot using this URL:\n{invite_url}")

    async def load_all_cogs(self):
        for filename in sorted(os.listdir('cogs')):
            if filename.endswith('.py') and not filename.startswith('_'):
                try:
                    await self.load_extension(f"cogs.{filename[:-3]}")
                    print(f"Loaded extension: {filename}")
                except Exception as e:
                    traceback.print_exception(e)
                    print(f"Failed to load extension {filename}: {e}")
    
    async def load_data(self):
        self.db = await aiosqlite.connect('data.db')

    def get_embed(self):
        embed = discord.Embed(color=COLOR)
        embed.set_footer(text=self.user.name, icon_url=self.user.avatar.url)
        embed.set_image(url='attachment://bar.gif')
        file = discord.File('bar.gif', filename='bar.gif')
        return embed, file

    def get_time(self):
        return datetime.datetime.now(datetime.timezone.utc)

if __name__ == "__main__":
    bot = BotClient()
    bot.run(TOKEN)