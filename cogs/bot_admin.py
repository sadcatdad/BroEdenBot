"""Owner-only Discord controls for BroEdenBot process management."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from utils.ai_config import get_ai_config
from utils.ai_service import (
    get_daily_ai_usage_usd,
    get_monthly_ai_usage_usd,
    initialize_ai_usage_schema,
)
from utils.settings import (
    EDITABLE_SETTING_KEYS,
    get_bool_setting,
    get_csv_ids_setting,
    get_setting,
)
from utils.ui import (
    INFO_COLOR,
    branded_embed,
    error_embed,
    warning_embed,
)


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = PROJECT_ROOT / "imports" / "discord_history"
ARCHIVE_ROOT = IMPORT_ROOT / "archive"
SUPPORTED_IMPORT_SUFFIXES = {".json", ".csv"}
SKIPPED_IMPORT_FOLDERS = {
    "archive",
    "archived",
    "broken_exports",
    "repaired_from_pi",
}
UNAUTHORIZED_MESSAGE = "You do not have permission to use bot management commands."

STATUS_ENV_VARS = (
    "GEMINI_API_KEY",
    "AI_ENABLED",
    "AI_MODEL_FAST",
    "AI_MODEL_DEFAULT",
    "AI_MODEL_ADVANCED",
    "AI_ENABLE_ADVANCED_MODEL",
    "AI_DAILY_BUDGET_USD",
    "AI_MONTHLY_BUDGET_USD",
    "AI_MAX_INPUT_TOKENS",
    "AI_MAX_OUTPUT_TOKENS",
    "AI_DEFAULT_TEMPERATURE",
    "AI_MEMBER_COOLDOWN_SECONDS",
    "AI_STAFF_COOLDOWN_SECONDS",
    "AI_LOG_PROMPTS",
    "AI_LOG_RESPONSES",
    "AI_DASHBOARD_VISIBLE",
    "MODAI_MODEL",
    "MODAI_FALLBACK_MODEL",
    "ASK_MODEL",
    "ASK_FALLBACK_MODEL",
    "ASK_ALLOWED_CHANNEL_IDS",
    "ASK_COOLDOWN_SECONDS",
    "MODAI_ALLOWED_ROLE_IDS",
    "MESSAGE_CONTEXT_ENABLED",
    "MESSAGE_CONTEXT_CHANNEL_IDS",
    "MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS",
    "MESSAGE_CONTEXT_ALLOWED_ROLE_IDS",
    "MESSAGE_CONTEXT_DB_PATH",
    "MESSAGE_CONTEXT_TRACK_DELETES",
    "MESSAGE_CONTEXT_TRACK_EDITS",
    "MESSAGE_CONTEXT_IGNORE_BOTS",
    "MESSAGE_CONTEXT_RETENTION_DAYS",
    "STAFF_AI_ALLOWED_ROLE_IDS",
    "STAFF_AI_MODEL",
    "STAFF_AI_FALLBACK_MODEL",
    "STAFF_CONTEXT_ENABLED",
    "STAFF_CONTEXT_CHANNEL_IDS",
    "STAFF_CONTEXT_DB_PATH",
    "STAFF_CONTEXT_TRACK_DELETES",
    "STAFF_NOTES_ALLOWED_ROLE_IDS",
    "REMINDER_ALLOWED_ROLE_IDS",
    "REMINDER_TIMEZONE",
    "CHECKLIST_ALLOWED_ROLE_IDS",
    "STATS_ALLOWED_ROLE_IDS",
    "ACTIVITY_EXCLUDED_ROLE_IDS",
    "VCSTATS_ALLOWED_ROLE_IDS",
    "VC_EXCLUDED_ROLE_IDS",
    "EXCLUDED_VOICE_CHANNEL_IDS",
    "VCREWARDS_ALLOWED_ROLE_IDS",
    "BANK_ALLOWED_ROLE_IDS",
    "VCXP_ENABLED",
    "VCXP_TRIGGER_ROLE_ID",
    "BOT_OWNER_USER_IDS",
    "BOT_OWNER_ALLOW_ADMINS",
)

RESTART_COMMAND = (
    "sudo",
    "-n",
    "systemd-run",
    "--unit=broedenbot-restart",
    "--collect",
    "/bin/bash",
    "-lc",
    "sleep 2; systemctl restart broedenbot",
)
DEPLOY_COMMAND = (
    "sudo",
    "-n",
    "systemd-run",
    "--unit=broedenbot-deploy",
    "--collect",
    "--working-directory=/home/sadcatdad/BroEdenBot",
    "/bin/bash",
    "-lc",
    "./deploy.sh",
)

SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"token|secret|password|passwd|api[_-]?key|authorization"
    r")(\s*[:=]\s*)([^\s,;]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
DISCORD_TOKEN_RE = re.compile(
    r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}\b"
)
TRACEBACK_LINE_RE = re.compile(
    r"(?i)(Traceback \(most recent call last\)|"
    r'\bFile "[^"]+", line \d+|'
    r"^\s*(?:[\^~]+\s*)$)"
)


def parse_user_ids(value: Optional[str]) -> set[int]:
    user_ids: set[int] = set()
    for item in re.split(r"[\s,]+", value or ""):
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError:
            continue
        if user_id > 0:
            user_ids.add(user_id)
    return user_ids


def env_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def is_bot_manager(user: object) -> bool:
    user_id = getattr(user, "id", None)
    if user_id in get_csv_ids_setting("BOT_OWNER_USER_IDS"):
        return True
    if not env_enabled("BOT_OWNER_ALLOW_ADMINS"):
        return False
    permissions = getattr(user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def sanitize_logs(text: str) -> str:
    safe_lines = []
    for line in text.replace("\x00", "").splitlines():
        if TRACEBACK_LINE_RE.search(line):
            if not safe_lines or safe_lines[-1] != "[traceback detail omitted]":
                safe_lines.append("[traceback detail omitted]")
            continue
        line = SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", line)
        line = BEARER_RE.sub("Bearer [REDACTED]", line)
        line = DISCORD_TOKEN_RE.sub("[REDACTED DISCORD TOKEN]", line)
        safe_lines.append(line.replace("```", "``\u200b`"))
    return "\n".join(safe_lines).strip() or "No log output was returned."


async def run_fixed_command(
    command: Sequence[str],
    *,
    timeout: float,
) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=PROJECT_ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        logger.warning("Fixed management command timed out")
        return 1, "", "TimeoutError"
    except OSError as exc:
        logger.warning(
            "Fixed management command failed to start: %s",
            type(exc).__name__,
        )
        return 1, "", type(exc).__name__
    return (
        process.returncode,
        stdout.decode("utf-8", "replace").strip(),
        stderr.decode("utf-8", "replace").strip(),
    )


def database_files() -> list[Path]:
    files: set[Path] = set()
    for folder in (PROJECT_ROOT, PROJECT_ROOT / "data"):
        if not folder.is_dir():
            continue
        for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
            files.update(path for path in folder.glob(pattern) if path.is_file())
    return sorted(files, key=lambda path: str(path).casefold())


def import_folder_summary() -> tuple[int, int, int]:
    active_count = 0
    active_size = 0
    if IMPORT_ROOT.is_dir():
        for path in IMPORT_ROOT.rglob("*"):
            if not path.is_file():
                continue
            relative_parts = {
                part.casefold() for part in path.relative_to(IMPORT_ROOT).parts[:-1]
            }
            if relative_parts & SKIPPED_IMPORT_FOLDERS:
                continue
            if path.suffix.casefold() not in SUPPORTED_IMPORT_SUFFIXES:
                continue
            active_count += 1
            try:
                active_size += path.stat().st_size
            except OSError:
                pass

    archived_count = 0
    if ARCHIVE_ROOT.is_dir():
        archived_count = sum(
            1
            for path in ARCHIVE_ROOT.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in SUPPORTED_IMPORT_SUFFIXES
        )
    return active_count, active_size, archived_count


class ManagementConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        action_label: str,
        progress_message: str,
        failure_message: str,
        command: Sequence[str],
    ) -> None:
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.action_label = action_label
        self.progress_message = progress_message
        self.failure_message = failure_message
        self.command = tuple(command)
        self.message: Optional[discord.InteractionMessage] = None

        self.confirm_button.label = f"Confirm {action_label}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            UNAUTHORIZED_MESSAGE,
            ephemeral=True,
        )
        return False

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=warning_embed(
                        f"{self.action_label} cancelled",
                        "The confirmation expired after 60 seconds.",
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not is_bot_manager(interaction.user):
            await interaction.response.send_message(
                UNAUTHORIZED_MESSAGE,
                ephemeral=True,
            )
            return

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=branded_embed(
                f"⚙️ {self.action_label} requested",
                self.progress_message,
                color=INFO_COLOR,
                footer="Private bot management",
            ),
            view=self,
        )

        returncode, _, _ = await run_fixed_command(self.command, timeout=10)
        if returncode != 0:
            await interaction.edit_original_response(
                embed=error_embed(
                    f"{self.action_label} could not start",
                    self.failure_message,
                ),
                view=None,
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=warning_embed(
                f"{self.action_label} cancelled",
                "No changes were made.",
            ),
            view=self,
        )
        self.stop()


class BotAdmin(commands.Cog):
    management = app_commands.Group(
        name="bot",
        description="Private BroEdenBot management tools",
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

    @management.command(name="help", description="Show bot management help")
    @app_commands.guild_only()
    async def help_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction):
            return
        embed = branded_embed(
            "🛠️ BroEdenBot Management",
            "Private controls for checking and maintaining the bot.",
            color=INFO_COLOR,
            footer="Owner-only unless administrator access is explicitly enabled",
        )
        embed.add_field(
            name="/bot status",
            value="Bot health, databases, cogs, Git status, imports, and configuration checks.",
            inline=False,
        )
        embed.add_field(
            name="/bot logs",
            value="Recent `broedenbot` systemd logs, with secret redaction.",
            inline=False,
        )
        embed.add_field(
            name="/bot restart",
            value="Confirm and restart BroEdenBot through systemd.",
            inline=False,
        )
        embed.add_field(
            name="/bot deploy",
            value="Confirm, pull the latest code, install dependencies, and restart.",
            inline=False,
        )
        embed.add_field(
            name="Historical imports stay terminal-only",
            value=(
                "Use `bedimportdry` and `bedimport` on the Pi.\n"
                "Export transfer remains Mac-side with `bedsync`."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @management.command(name="status", description="Show private bot health details")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild = interaction.guild
        git_hash_result, git_branch_result, git_status_result, service_result = (
            await asyncio.gather(
                run_fixed_command(
                    ("git", "rev-parse", "--short", "HEAD"),
                    timeout=4,
                ),
                run_fixed_command(
                    ("git", "rev-parse", "--abbrev-ref", "HEAD"),
                    timeout=4,
                ),
                run_fixed_command(
                    ("git", "status", "--porcelain", "--untracked-files=normal"),
                    timeout=4,
                ),
                run_fixed_command(
                    ("systemctl", "is-active", "broedenbot"),
                    timeout=4,
                ),
            )
        )
        git_hash = (
            git_hash_result[1] if git_hash_result[0] == 0 else None
        )
        git_branch = (
            git_branch_result[1] if git_branch_result[0] == 0 else None
        )
        git_status_returncode, git_status_stdout, _ = git_status_result
        if git_status_returncode == 0:
            tree_status = "Clean" if not git_status_stdout else "Changes present"
        else:
            tree_status = "Unavailable"
        service_state = service_result[1] or "Unavailable"

        runtime = branded_embed(
            "🩺 BroEdenBot Status",
            color=INFO_COLOR,
            timestamp=True,
            footer="Private bot management",
        )
        runtime.add_field(
            name="Runtime",
            value=(
                f"Bot: **{discord.utils.escape_markdown(str(self.bot.user))}**\n"
                f"Latency: **{self.bot.latency * 1000:.0f} ms**\n"
                f"Server: **{discord.utils.escape_markdown(guild.name)}**\n"
                f"Systemd: **{service_state or 'Unavailable'}**"
            )[:1024],
            inline=False,
        )
        runtime.add_field(
            name="Git",
            value=(
                f"Commit: **{git_hash or 'Unavailable'}**\n"
                f"Branch: **{git_branch or 'Unavailable'}**\n"
                f"Working tree: **{tree_status}**"
            ),
            inline=True,
        )
        extensions = sorted(self.bot.extensions)
        extension_text = ", ".join(extensions) or "None"
        runtime.add_field(
            name=f"Loaded cogs ({len(extensions)})",
            value=extension_text[:1024],
            inline=False,
        )
        failed_extensions = getattr(self.bot, "failed_extensions", {})
        if failed_extensions:
            runtime.add_field(
                name=f"Failed cogs ({len(failed_extensions)})",
                value="\n".join(
                    f"`{name}` — {error_type}"
                    for name, error_type in sorted(failed_extensions.items())
                )[:1024],
                inline=False,
            )

        storage = branded_embed(
            "💾 Storage & Imports",
            color=INFO_COLOR,
            footer="Metadata only; message content is not displayed",
        )
        databases = database_files()
        database_lines = []
        for path in databases:
            try:
                size = format_bytes(path.stat().st_size)
            except OSError:
                size = "Unavailable"
            database_lines.append(
                f"`{path.relative_to(PROJECT_ROOT)}` — {size}"
            )
        database_text = "\n".join(database_lines) or (
            "No SQLite database files found."
        )
        storage.add_field(
            name=f"Database files ({len(databases)})",
            value=database_text[:1024],
            inline=False,
        )

        active_count, active_size, archived_count = import_folder_summary()
        storage.add_field(
            name="Import folders",
            value=(
                f"Active files: **{active_count:,}**\n"
                f"Active size: **{format_bytes(active_size)}**\n"
                f"Archived files: **{archived_count:,}**"
            ),
            inline=True,
        )
        import_status_error = False
        try:
            last_import = await self._last_import(guild.id)
        except sqlite3.Error as exc:
            logger.warning(
                "Could not read historical import status: %s",
                type(exc).__name__,
            )
            last_import = None
            import_status_error = True
        if last_import:
            imported_at, filename, status, imported, duplicates = last_import
            last_import_text = (
                f"When: `{imported_at}`\n"
                f"File: `{Path(filename).name if filename else 'Unknown'}`\n"
                f"Status: **{status or 'Unknown'}**\n"
                f"Imported: **{int(imported or 0):,}**\n"
                f"Duplicates: **{int(duplicates or 0):,}**"
            )
        elif import_status_error:
            last_import_text = "Historical import status is temporarily unavailable."
        else:
            last_import_text = "No recorded historical import was found."
        storage.add_field(
            name="Last historical import",
            value=last_import_text[:1024],
            inline=False,
        )

        configuration = branded_embed(
            "🔐 Configuration & VCXP",
            color=INFO_COLOR,
            footer="Values and secrets are never displayed",
        )
        configured = [
            f"{'✅' if (get_setting(name, '') if name in EDITABLE_SETTING_KEYS else os.getenv(name, '')).strip() else '⚠️'} `{name}`"
            for name in STATUS_ENV_VARS
        ]
        midpoint = (len(configured) + 1) // 2
        configuration.add_field(
            name="Configuration 1/2",
            value="\n".join(configured[:midpoint]),
            inline=True,
        )
        configuration.add_field(
            name="Configuration 2/2",
            value="\n".join(configured[midpoint:]),
            inline=True,
        )

        trigger_role_text = (get_setting("VCXP_TRIGGER_ROLE_ID", "") or "").strip()
        trigger_role_id = int(trigger_role_text) if trigger_role_text.isdigit() else 0
        trigger_role = guild.get_role(trigger_role_id) if trigger_role_id else None
        bot_member = guild.me
        can_manage_role = bool(
            trigger_role
            and bot_member
            and bot_member.guild_permissions.manage_roles
            and bot_member.top_role > trigger_role
            and not trigger_role.managed
        )
        configuration.add_field(
            name="VCXP safety",
            value=(
                f"Enabled: **{'Yes' if get_bool_setting('VCXP_ENABLED') else 'No'}**\n"
                f"Trigger role configured: **{'Yes' if trigger_role_id else 'No'}**\n"
                f"Trigger role found: **{'Yes' if trigger_role else 'No'}**\n"
                f"Bot can manage role: **{'Yes' if can_manage_role else 'No'}**"
            ),
            inline=False,
        )
        ai_config = get_ai_config()
        await initialize_ai_usage_schema(self.bot.db)
        daily_ai_spend = await get_daily_ai_usage_usd(self.bot.db)
        monthly_ai_spend = await get_monthly_ai_usage_usd(self.bot.db)
        configuration.add_field(
            name="AI framework",
            value=(
                f"Enabled: **{'Yes' if ai_config.enabled else 'No'}**\n"
                f"Gemini API key present: **{'Yes' if ai_config.api_key_present else 'No'}**\n"
                f"Default model: `{ai_config.models.default}`\n"
                f"Daily spend: **${daily_ai_spend:.4f}** / ${ai_config.budgets.daily_usd:.2f}\n"
                f"Monthly spend: **${monthly_ai_spend:.4f}** / ${ai_config.budgets.monthly_usd:.2f}"
            )[:1024],
            inline=False,
        )

        await interaction.followup.send(
            embeds=[runtime, storage, configuration],
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @management.command(name="logs", description="Show recent BroEdenBot logs")
    @app_commands.describe(lines="Number of recent log lines to return")
    @app_commands.guild_only()
    async def logs(
        self,
        interaction: discord.Interaction,
        lines: app_commands.Range[int, 1, 200] = 50,
    ) -> None:
        if not await self._require_access(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        returncode, stdout, _ = await run_fixed_command(
            (
                "journalctl",
                "-u",
                "broedenbot",
                "-n",
                str(lines),
                "--no-pager",
            ),
            timeout=8,
        )
        if returncode != 0:
            await interaction.followup.send(
                embed=error_embed(
                    "Logs unavailable",
                    "Recent systemd logs could not be read on this host.",
                ),
                ephemeral=True,
            )
            return

        safe_logs = sanitize_logs(stdout)
        if len(safe_logs) <= 1_850:
            await interaction.followup.send(
                f"```text\n{safe_logs}\n```",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        file = discord.File(
            io.BytesIO(safe_logs.encode("utf-8")),
            filename="broedenbot-logs.txt",
        )
        await interaction.followup.send(
            "Recent BroEdenBot logs:",
            file=file,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _confirmation(
        self,
        interaction: discord.Interaction,
        *,
        action_label: str,
        progress_message: str,
        failure_message: str,
        command: Sequence[str],
    ) -> None:
        if not await self._require_access(interaction):
            return
        view = ManagementConfirmView(
            requester_id=interaction.user.id,
            action_label=action_label,
            progress_message=progress_message,
            failure_message=failure_message,
            command=command,
        )
        await interaction.response.send_message(
            embed=warning_embed(
                f"Confirm {action_label.lower()}",
                (
                    f"Only continue if you intend to {action_label.lower()} "
                    "the live BroEdenBot service."
                ),
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @management.command(name="restart", description="Restart BroEdenBot")
    @app_commands.guild_only()
    async def restart(self, interaction: discord.Interaction) -> None:
        await self._confirmation(
            interaction,
            action_label="Restart",
            progress_message="Restarting BroEdenBot…",
            failure_message=(
                "Restart could not start. Check sudoers/systemd-run "
                "permissions on the Pi."
            ),
            command=RESTART_COMMAND,
        )

    @management.command(name="deploy", description="Deploy the latest BroEdenBot")
    @app_commands.guild_only()
    async def deploy(self, interaction: discord.Interaction) -> None:
        await self._confirmation(
            interaction,
            action_label="Deploy",
            progress_message="Deploy started. The bot may restart shortly.",
            failure_message=(
                "Deploy could not start. Check sudoers/systemd-run "
                "permissions on the Pi."
            ),
            command=DEPLOY_COMMAND,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotAdmin(bot))
