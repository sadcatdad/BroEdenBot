import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.knowledge import search_server_knowledge


SUPPORT_MESSAGE = (
    "I couldn’t find that in the guide. Please submit a ticket in "
    "<#1300632962127368283> if you need help."
)
SOURCE_LABELS = {
    "Bro Eden Survival Guide": "Survival Guide",
    "Bro Eden Rules": "Rules",
}


class Guide(commands.Cog):
    guide = app_commands.Group(
        name="guide",
        description="Search public Bro Eden guidance",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_use_by_user = {}
        self._cooldown_lock = asyncio.Lock()

    async def _is_rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        async with self._cooldown_lock:
            last_use = self._last_use_by_user.get(user_id)
            if last_use is not None and now - last_use < 15:
                return True
            self._last_use_by_user[user_id] = now
            return False

    @guide.command(name="search", description="Search the public guide and rules")
    @app_commands.describe(query="Keywords or topic to search for")
    @app_commands.guild_only()
    async def search(
        self,
        interaction: discord.Interaction,
        query: app_commands.Range[str, 2, 200],
    ) -> None:
        if await self._is_rate_limited(interaction.user.id):
            await interaction.response.send_message(
                "Please wait a few seconds before searching the guide again.",
                ephemeral=True,
            )
            return

        results = search_server_knowledge(
            query.strip(),
            max_results=3,
            max_excerpt_chars=450,
        )
        if not results:
            await interaction.response.send_message(
                SUPPORT_MESSAGE,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        embed = discord.Embed(
            title="Guide search results",
            description=f"Best public matches for **{discord.utils.escape_markdown(query)}**",
            color=discord.Color.green(),
        )
        for source, heading, excerpt in results:
            label = SOURCE_LABELS.get(source, source)
            embed.add_field(
                name=f"{label} — {heading}"[:256],
                value=excerpt[:1024] or "Matching section found.",
                inline=False,
            )
        embed.set_footer(text="Deterministic keyword search of public guidance only.")
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Guide(bot))
