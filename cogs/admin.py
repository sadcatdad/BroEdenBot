import asyncio
import os
from pathlib import Path
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import COLOR


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEALTH_ENV_VARS = (
    "GEMINI_API_KEY",
    "MODAI_MODEL",
    "MODAI_FALLBACK_MODEL",
    "ASK_MODEL",
    "ASK_FALLBACK_MODEL",
    "ASK_ALLOWED_CHANNEL_IDS",
    "ASK_COOLDOWN_SECONDS",
    "MODAI_ALLOWED_ROLE_IDS",
    "STAFF_NOTES_ALLOWED_ROLE_IDS",
    "STATS_ALLOWED_ROLE_IDS",
    "VCSTATS_ALLOWED_ROLE_IDS",
    "VCREWARDS_ALLOWED_ROLE_IDS",
    "BANK_ALLOWED_ROLE_IDS",
    "VCXP_ENABLED",
    "VCXP_TRIGGER_ROLE_ID",
)


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


class Admin(commands.Cog):
    admin = app_commands.Group(
        name="admin",
        description="Private bot administration tools",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _table_exists(self, table_name: str) -> bool:
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def _last_import(self, guild_id: int) -> Optional[tuple]:
        if not await self._table_exists("stats_activity_imports"):
            return None
        cursor = await self.bot.db.execute(
            """
            SELECT imported_at, filename, status, messages_imported,
                   duplicates_skipped
            FROM stats_activity_imports
            WHERE guild_id = ?
            ORDER BY imported_at DESC, id DESC
            LIMIT 1
            """,
            (guild_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @staticmethod
    def _database_files() -> List[Path]:
        files = set(PROJECT_ROOT.glob("*.db"))
        files.update(PROJECT_ROOT.glob("*.sqlite"))
        files.update(PROJECT_ROOT.glob("*.sqlite3"))
        data_dir = PROJECT_ROOT / "data"
        if data_dir.is_dir():
            files.update(data_dir.glob("*.db"))
            files.update(data_dir.glob("*.sqlite"))
            files.update(data_dir.glob("*.sqlite3"))
        return sorted(files, key=lambda path: str(path).casefold())

    @staticmethod
    async def _git_hash() -> Optional[str]:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--short",
                "HEAD",
                cwd=PROJECT_ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=3)
        except (OSError, asyncio.TimeoutError):
            return None
        if process.returncode != 0:
            return None
        return stdout.decode("utf-8", "replace").strip() or None

    @admin.command(name="health", description="Show private bot health details")
    @app_commands.guild_only()
    async def health(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not (
            interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message(
                "Only administrators can use /admin health.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        bot_member = guild.me
        role = (
            guild.get_role(int(os.getenv("VCXP_TRIGGER_ROLE_ID", "0") or 0))
            if os.getenv("VCXP_TRIGGER_ROLE_ID", "").strip().isdigit()
            else None
        )
        can_manage_role = bool(
            role
            and bot_member
            and bot_member.guild_permissions.manage_roles
            and bot_member.top_role > role
            and not role.managed
        )
        git_hash = await self._git_hash()
        last_import = await self._last_import(guild.id)

        embed = discord.Embed(
            title="BroEdenBot health",
            color=discord.Color(COLOR),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Bot process",
            value=(
                f"User: **{self.bot.user}**\n"
                f"Latency: **{self.bot.latency * 1000:.0f} ms**\n"
                f"Server: **{discord.utils.escape_markdown(guild.name)}**\n"
                "Running inside bot process: **Yes**\n"
                f"Git commit: **{git_hash or 'Unavailable'}**"
            )[:1024],
            inline=False,
        )

        extensions = sorted(self.bot.extensions)
        extension_text = ", ".join(extensions) or "None"
        if len(extension_text) > 1000:
            extension_text = extension_text[:997].rstrip(", ") + "…"
        embed.add_field(
            name=f"Loaded extensions ({len(extensions)})",
            value=extension_text,
            inline=False,
        )

        databases = self._database_files()
        database_text = "\n".join(
            f"`{path.relative_to(PROJECT_ROOT)}` — {_format_bytes(path.stat().st_size)}"
            for path in databases
        ) or "No database files found."
        embed.add_field(name="Database files", value=database_text[:1024], inline=False)

        configured = [
            f"{'✅' if os.getenv(name, '').strip() else '⚠️'} `{name}`"
            for name in HEALTH_ENV_VARS
        ]
        midpoint = (len(configured) + 1) // 2
        embed.add_field(
            name="Environment 1/2",
            value="\n".join(configured[:midpoint]),
            inline=True,
        )
        embed.add_field(
            name="Environment 2/2",
            value="\n".join(configured[midpoint:]),
            inline=True,
        )

        embed.add_field(
            name="VCXP safety",
            value=(
                f"Enabled: **{'Yes' if os.getenv('VCXP_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'} else 'No'}**\n"
                f"Trigger role exists: **{'Yes' if role else 'No'}**\n"
                f"Bot can manage role: **{'Yes' if can_manage_role else 'No'}**"
            ),
            inline=False,
        )
        if last_import:
            imported_at, filename, status, imported, duplicates = last_import
            embed.add_field(
                name="Last historical import",
                value=(
                    f"When: `{imported_at}`\n"
                    f"File: `{filename or 'Unknown'}`\n"
                    f"Status: **{status or 'Unknown'}**\n"
                    f"Imported: **{int(imported or 0):,}** • "
                    f"Duplicates: **{int(duplicates or 0):,}**"
                )[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Last historical import",
                value="No recorded import batch was found.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
