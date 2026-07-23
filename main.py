import datetime
import logging
import os
import re
from pathlib import Path
from typing import Optional, Set

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR, TOKEN
from utils.ai_kb import initialize_ai_kb_schema_async
from utils.ai_service import initialize_ai_usage_schema
from utils.live_knowledge import initialize_live_knowledge_schema
from utils.settings import initialize_settings_from_env, settings_database_path
from utils.sqlite import configure_connection
from utils.ui import error_embed
from utils.visual_studio import initialize_visual_studio_schema


PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


COG_MODULE_REQUIREMENTS = {
    "ai.py": {"ask", "mod_ai", "staff_ai"},
    "ask.py": {"ask"},
    "bank.py": {"bank"},
    "checklist.py": {"checklists"},
    "disboard_bumps.py": {"bumps"},
    "events.py": {"events"},
    "knowledge_sources.py": {"knowledge"},
    "leaderboards.py": {"stats"},
    "message_context.py": {"message_context"},
    "mod_ai.py": {"mod_ai"},
    "poll.py": {"polls"},
    "queue.py": {"karaoke"},
    "reminder.py": {"reminders"},
    "rulecard.py": {"rulecards"},
    "staff_ai.py": {"staff_ai"},
    "staff_notes.py": {"staff_notes"},
    "stats.py": {"stats"},
    "streaks.py": {"streaks"},
    "vc_stats.py": {"vc_stats", "vc_xp"},
    "visual_assets.py": {"visual"},
}


def configured_modules() -> Optional[Set[str]]:
    """Return configured modules, or None to preserve legacy load-all behavior."""
    raw_value = os.getenv("ENABLED_MODULES", "").strip()
    if not raw_value:
        return None
    return {
        item.strip().casefold()
        for item in re.split(r"[\s,]+", raw_value)
        if item.strip()
    }


def cog_is_enabled(filename: str, enabled_modules: Optional[Set[str]]) -> bool:
    requirements = COG_MODULE_REQUIREMENTS.get(filename)
    if filename == "events.py" and enabled_modules is not None:
        return {"events", "reminders"}.issubset(enabled_modules)
    return requirements is None or enabled_modules is None or bool(
        requirements & enabled_modules
    )

intents = discord.Intents.none()
intents.guilds = True
intents.emojis_and_stickers = True
intents.members = True
intents.guild_messages = True
intents.message_content = True
intents.voice_states = True


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
        self.failed_extensions: dict[str, str] = {}

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
                activity=discord.Game(name="/ask"),
            )
        except discord.HTTPException:
            logger.warning("Could not update bot presence")
        logger.info("%s is ready in %s guild(s)", self.user, len(self.guilds))
        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            manage_roles=True,
        )
        scopes = ["bot", "applications.commands"]
        invite_url = discord.utils.oauth_url(self.user.id, permissions=permissions, scopes=scopes)
        logger.info("Invite URL: %s", invite_url)

    async def load_all_cogs(self):
        self.failed_extensions.clear()
        enabled_modules = configured_modules()
        for filename in sorted(os.listdir("cogs")):
            if filename.endswith(".py") and not filename.startswith("_"):
                if not cog_is_enabled(filename, enabled_modules):
                    logger.info("Skipped disabled extension: cogs.%s", filename[:-3])
                    continue
                try:
                    extension = f"cogs.{filename[:-3]}"
                    await self.load_extension(extension)
                    logger.info("Loaded extension: %s", extension)
                except Exception as exc:
                    self.failed_extensions[filename] = type(exc).__name__
                    logger.exception(
                        "Failed to load extension %s",
                        filename,
                    )
        if self.failed_extensions:
            logger.warning(
                "Bot started with failed cogs: %s",
                ", ".join(sorted(self.failed_extensions)),
            )

    async def load_data(self):
        initialize_settings_from_env()
        initialize_visual_studio_schema()
        self.db = await aiosqlite.connect(settings_database_path())
        self.db.row_factory = aiosqlite.Row
        journal_mode = await configure_connection(
            self.db,
            foreign_keys=True,
        )
        if journal_mode != "wal":
            logger.warning(
                "Shared database journal mode is %s instead of WAL",
                journal_mode,
            )
        await initialize_ai_usage_schema(self.db)
        await initialize_ai_kb_schema_async(self.db)
        await initialize_live_knowledge_schema(self.db)

    async def close(self):
        try:
            await super().close()
        finally:
            if self.db is not None:
                await self.db.close()
                self.db = None

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
