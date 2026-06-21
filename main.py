import datetime
import logging
import os
from pathlib import Path
import traceback

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR, TOKEN
from utils.ui import error_embed


PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.all()


class BroEdenCommandTree(app_commands.CommandTree):
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        logger.error(
            "Unhandled application command error",
            exc_info=(type(error), error, error.__traceback__),
        )
        message = error_embed(
            "Something went sideways",
            "The command could not be completed. Please try again in a moment.",
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=message, ephemeral=True)
            else:
                await interaction.response.send_message(
                    embed=message,
                    ephemeral=True,
                )
        except discord.HTTPException:
            logger.exception("Could not deliver application-command error")


class BotClient(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=BroEdenCommandTree,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True,
                replied_user=False,
            ),
        )
        self.db = None
        self._ready_logged = False

    async def setup_hook(self):
        await self.load_data()
        await self.load_all_cogs()
        await self.tree.sync()

    async def on_ready(self):
        if self._ready_logged:
            return
        self._ready_logged = True
        try:
            await self.change_presence(
                activity=discord.Game(name="/ask • /guide search"),
            )
        except discord.HTTPException:
            logger.warning("Could not update bot presence")
        logger.info("%s is ready in %s guild(s)", self.user, len(self.guilds))
        permissions = discord.Permissions(administrator=True)
        scopes = ["bot", "applications.commands"]
        invite_url = discord.utils.oauth_url(self.user.id, permissions=permissions, scopes=scopes)
        logger.info("Invite URL: %s", invite_url)

    async def load_all_cogs(self):
        failures = []
        for filename in sorted(os.listdir("cogs")):
            if filename.endswith(".py") and not filename.startswith("_"):
                try:
                    extension = f"cogs.{filename[:-3]}"
                    await self.load_extension(extension)
                    logger.info("Loaded extension: %s", extension)
                except Exception as exc:
                    failures.append(filename)
                    traceback.print_exception(exc)
                    logger.error(
                        "Failed to load extension %s: %s",
                        filename,
                        type(exc).__name__,
                    )
        if failures:
            logger.warning("Bot started with failed cogs: %s", ", ".join(failures))

    async def load_data(self):
        self.db = await aiosqlite.connect(PROJECT_ROOT / "data.db")
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.execute("PRAGMA busy_timeout = 30000")

    async def close(self):
        if self.db is not None:
            await self.db.close()
            self.db = None
        await super().close()

    def get_embed(self):
        embed = discord.Embed(color=COLOR)
        if self.user:
            embed.set_footer(
                text=f"{self.user.name} • Bro Eden",
                icon_url=self.user.display_avatar.url,
            )
        embed.set_image(url="attachment://bar.gif")
        file = discord.File(PROJECT_ROOT / "bar.gif", filename="bar.gif")
        return embed, file

    def get_time(self):
        return datetime.datetime.now(datetime.timezone.utc)

if __name__ == "__main__":
    bot = BotClient()
    bot.run(TOKEN)
