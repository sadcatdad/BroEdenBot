"""Private AI framework diagnostics for BroEdenBot."""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.bot_admin import UNAUTHORIZED_MESSAGE, is_bot_manager
from utils.ai_kb import (
    SOURCE_TYPES,
    VISIBILITIES,
    delete_kb_source,
    get_kb_status,
    initialize_ai_kb_schema_async,
    search_kb,
    upsert_kb_source,
)
from utils.ai_config import get_ai_config
from utils.ai_service import (
    AI_BUDGET_MESSAGE,
    AI_DISABLED_MESSAGE,
    generate_ai_response,
    get_daily_ai_usage_usd,
    get_monthly_ai_usage_usd,
    initialize_ai_usage_schema,
)
from utils.ui import INFO_COLOR, SUCCESS_COLOR, branded_embed, error_embed


class AI(commands.Cog):
    ai = app_commands.Group(
        name="ai",
        description="Private AI framework tools",
    )
    kb = app_commands.Group(
        name="kb",
        description="Manage the AI knowledge base",
    )
    ai.add_command(kb)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _require_access(self, interaction: discord.Interaction) -> bool:
        if is_bot_manager(interaction.user):
            return True
        if interaction.response.is_done():
            await interaction.followup.send(
                UNAUTHORIZED_MESSAGE,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                UNAUTHORIZED_MESSAGE,
                ephemeral=True,
            )
        return False

    @ai.command(name="test", description="Run a private AI framework test")
    @app_commands.guild_only()
    async def test(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = getattr(self.bot, "db", None)
        if db is not None:
            await initialize_ai_usage_schema(db)

        result = await generate_ai_response(
            task_type="framework_test",
            prompt="Reply with exactly this short sentence: BroEdenBot AI test passed.",
            system_instruction=(
                "You are testing BroEdenBot's internal AI framework. Keep the "
                "response short and do not include secrets."
            ),
            requested_tier="default",
            max_output_tokens=80,
            temperature=0.1,
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            source_command="/ai test",
            metadata={"phase": "0"},
            db=db,
        )
        ai_config = get_ai_config()
        daily_spend = await get_daily_ai_usage_usd(db) if db is not None else 0.0
        monthly_spend = await get_monthly_ai_usage_usd(db) if db is not None else 0.0

        if result.ok:
            usage = result.usage
            embed = branded_embed(
                "AI test successful",
                color=SUCCESS_COLOR,
                footer="Private AI framework test",
            )
            embed.add_field(name="Model", value=result.model_used, inline=False)
            embed.add_field(
                name="Estimated cost",
                value=f"${result.estimated_cost_usd:.6f}",
                inline=True,
            )
            embed.add_field(
                name="Tokens",
                value=(
                    f"{usage.input_tokens:,} input / {usage.output_tokens:,} output"
                    if usage
                    else "Unavailable"
                ),
                inline=True,
            )
            embed.add_field(
                name="Budget",
                value=(
                    f"Today: ${daily_spend:.4f} / ${ai_config.budgets.daily_usd:.2f}\n"
                    f"This month: ${monthly_spend:.4f} / ${ai_config.budgets.monthly_usd:.2f}"
                ),
                inline=False,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        if result.blocked_by_budget:
            message = AI_BUDGET_MESSAGE
        elif result.error == AI_DISABLED_MESSAGE:
            message = AI_DISABLED_MESSAGE
        else:
            message = result.error or "AI test could not complete right now."
        embed = error_embed("AI test unavailable", message)
        embed.add_field(name="Model", value=result.model_used, inline=True)
        embed.add_field(
            name="Budget",
            value=(
                f"Today: ${daily_spend:.4f} / ${ai_config.budgets.daily_usd:.2f}\n"
                f"This month: ${monthly_spend:.4f} / ${ai_config.budgets.monthly_usd:.2f}"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ai.command(name="status", description="Show private AI framework status")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction):
            return
        db = getattr(self.bot, "db", None)
        if db is not None:
            await initialize_ai_usage_schema(db)
        ai_config = get_ai_config()
        daily_spend = await get_daily_ai_usage_usd(db) if db is not None else 0.0
        monthly_spend = await get_monthly_ai_usage_usd(db) if db is not None else 0.0
        embed = branded_embed(
            "AI framework status",
            color=INFO_COLOR,
            footer="Values and secrets are never displayed",
        )
        embed.add_field(
            name="Configuration",
            value=(
                f"AI enabled: **{'Yes' if ai_config.enabled else 'No'}**\n"
                f"Gemini API key present: **{'Yes' if ai_config.api_key_present else 'No'}**\n"
                f"Default model: `{ai_config.models.default}`\n"
                f"Advanced model enabled: **{'Yes' if ai_config.advanced_enabled else 'No'}**"
            ),
            inline=False,
        )
        embed.add_field(
            name="Budget",
            value=(
                f"Today: **${daily_spend:.4f}** / ${ai_config.budgets.daily_usd:.2f}\n"
                f"This month: **${monthly_spend:.4f}** / ${ai_config.budgets.monthly_usd:.2f}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @kb.command(name="import", description="Import or replace an AI KB source")
    @app_commands.describe(
        source_name="Unique source name, such as server-faq",
        source_type="Type of server knowledge",
        visibility="public for /ask, staff for staff-only tools",
        content="Markdown or plain text content",
        attachment="Optional .txt or .md file",
    )
    @app_commands.choices(
        source_type=[
            app_commands.Choice(name=value, value=value)
            for value in sorted(SOURCE_TYPES)
        ],
        visibility=[
            app_commands.Choice(name=value, value=value)
            for value in sorted(VISIBILITIES)
        ],
    )
    @app_commands.guild_only()
    async def kb_import(
        self,
        interaction: discord.Interaction,
        source_name: str,
        source_type: app_commands.Choice[str],
        visibility: app_commands.Choice[str],
        content: str = "",
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        if not await self._require_access(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pieces = [content.strip()] if content and content.strip() else []
        if attachment is not None:
            filename = attachment.filename or ""
            if not filename.casefold().endswith((".txt", ".md", ".markdown")):
                await interaction.followup.send(
                    "Only .txt, .md, and .markdown uploads are supported.",
                    ephemeral=True,
                )
                return
            if attachment.size and attachment.size > 2 * 1024 * 1024:
                await interaction.followup.send(
                    "That file is too large. The Phase 1 limit is 2 MB.",
                    ephemeral=True,
                )
                return
            try:
                uploaded = (await attachment.read()).decode("utf-8")
            except UnicodeDecodeError:
                await interaction.followup.send(
                    "That file is not valid UTF-8 text.",
                    ephemeral=True,
                )
                return
            pieces.append(uploaded.strip())
        try:
            result = upsert_kb_source(
                source_name=source_name,
                source_type=source_type.value,
                visibility=visibility.value,
                raw_text="\n\n".join(piece for piece in pieces if piece),
                metadata={
                    "imported_by": str(interaction.user.id),
                    "source": "discord",
                },
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        db = getattr(self.bot, "db", None)
        if db is not None:
            await initialize_ai_kb_schema_async(db)
        await interaction.followup.send(
            (
                "KB source imported.\n"
                f"Source: `{result['source_name']}`\n"
                f"Type: `{result['source_type']}`\n"
                f"Visibility: `{result['visibility']}`\n"
                f"Chunks saved: **{result['chunk_count']}**"
            ),
            ephemeral=True,
        )

    @kb.command(name="status", description="Show AI KB status")
    @app_commands.guild_only()
    async def kb_status(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction):
            return
        status = get_kb_status()
        latest = status["latest_source"]
        by_type = "\n".join(
            f"`{row['source_type']}`: {row['chunk_count']}"
            for row in status["by_type"]
        ) or "No chunks yet."
        embed = branded_embed(
            "AI Knowledge Base Status",
            color=INFO_COLOR,
            footer="No prompts or responses are stored here",
        )
        embed.add_field(
            name="Totals",
            value=(
                f"Sources: **{status['total_sources']}**\n"
                f"Chunks: **{status['total_chunks']}**\n"
                f"Public chunks: **{status['public_chunks']}**\n"
                f"Staff chunks: **{status['staff_chunks']}**"
            ),
            inline=False,
        )
        embed.add_field(name="By source type", value=by_type[:1024], inline=False)
        embed.add_field(
            name="Last updated",
            value=(
                f"`{latest['source_name']}` at `{latest['updated_at']}`"
                if latest
                else "No sources yet."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @kb.command(name="search", description="Search AI KB chunks")
    @app_commands.describe(
        query="Search text",
        visibility="Which visibility to search",
        limit="Maximum results",
    )
    @app_commands.choices(
        visibility=[
            app_commands.Choice(name="all", value="all"),
            app_commands.Choice(name="public", value="public"),
            app_commands.Choice(name="staff", value="staff"),
        ]
    )
    @app_commands.guild_only()
    async def kb_search(
        self,
        interaction: discord.Interaction,
        query: str,
        visibility: Optional[app_commands.Choice[str]] = None,
        limit: app_commands.Range[int, 1, 10] = 5,
    ) -> None:
        if not await self._require_access(interaction):
            return
        results = search_kb(
            query=query,
            visibility=visibility.value if visibility else "all",
            limit=limit,
        )
        if not results:
            await interaction.response.send_message(
                "No matching KB chunks found.",
                ephemeral=True,
            )
            return
        lines = []
        for item in results:
            title = item.get("section_title") or "General"
            lines.append(
                f"**{item['source_name']} / {title}** "
                f"`{item['source_type']}` `{item['source_visibility']}`\n"
                f"{item['excerpt']}"
            )
        await interaction.response.send_message(
            embed=branded_embed(
                "AI KB Search",
                description="\n\n".join(lines)[:3900],
                footer=f"{len(results)} result(s)",
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @kb.command(name="delete", description="Delete an AI KB source")
    @app_commands.describe(source_name="Source name to delete")
    @app_commands.guild_only()
    async def kb_delete(
        self,
        interaction: discord.Interaction,
        source_name: str,
    ) -> None:
        if not await self._require_access(interaction):
            return
        try:
            deleted = delete_kb_source(source_name)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Deleted `{source_name}` with **{deleted}** chunk(s).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AI(bot))
