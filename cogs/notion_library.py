"""Periodic read-only Notion sync. Disabled until a server token is configured."""
import asyncio
import logging
import os
from discord.ext import commands, tasks
from utils.notion_library import sync_library

log = logging.getLogger(__name__)


class NotionLibrary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.refresh.start()

    def cog_unload(self):
        self.refresh.cancel()

    @tasks.loop(minutes=5)
    async def refresh(self):
        try:
            result = await asyncio.to_thread(sync_library)
            if result.get("failed"):
                log.warning("Notion library sync: %s page(s) failed", result["failed"])
        except Exception:
            log.warning("Notion library sync failed; check server configuration")


async def setup(bot):
    if os.getenv("NOTION_LIBRARY_TOKEN", "").strip():
        await bot.add_cog(NotionLibrary(bot))
