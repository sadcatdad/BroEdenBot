"""Private AI framework diagnostics for BroEdenBot."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs.bot_admin import UNAUTHORIZED_MESSAGE, is_bot_manager
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AI(bot))
