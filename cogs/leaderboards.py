from __future__ import annotations

import colorsys
import hashlib
import io
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from config import COLOR
from utils.access import (
    configured_admin_role_ids,
    is_configured_owner,
    is_configured_staff,
)
from utils.audit_log import publish_audit
from utils.ranked_graphic import (
    RankedGraphicItem,
    RankedGraphicSection,
    render_ranked_graphic,
)
from utils.settings import get_csv_ids_setting, get_setting
from utils.ui import error_embed, success_embed, warning_embed


PAGE_SIZE = 10
MAX_LEADERBOARD_NAME = 50
MAX_LEADERBOARD_DESCRIPTION = 500
MAX_BANNER_BYTES = 8_000_000


logger = logging.getLogger(__name__)


def parse_points(value: str) -> Optional[float]:
    try:
        points = round(abs(float(value)), 2)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(points) or points <= 0:
        return None
    return points


def format_points(value: float) -> str:
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


def normalize_leaderboard_name(value: str) -> Optional[str]:
    name = " ".join(value.split())
    if not name or len(name) > MAX_LEADERBOARD_NAME:
        return None
    return name


def can_manage_leaderboards(member: object) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (permissions.administrator or permissions.manage_guild)
    )


def normalize_accent_color(value: str) -> Optional[str]:
    cleaned = str(value or "").strip()
    if cleaned.casefold() == "auto":
        return "auto"
    hexadecimal = cleaned.removeprefix("#")
    if len(hexadecimal) != 6:
        return None
    try:
        int(hexadecimal, 16)
    except ValueError:
        return None
    return f"#{hexadecimal.upper()}"


def resolve_leaderboard_accent(
    setting: Optional[str],
    banner_bytes: Optional[bytes],
    fallback: int = COLOR,
) -> int:
    normalized = normalize_accent_color(setting or "auto")
    if normalized and normalized != "auto":
        return int(normalized[1:], 16)
    if not banner_bytes:
        return fallback
    try:
        with Image.open(io.BytesIO(banner_bytes)) as source:
            sample = source.convert("RGB")
            sample.thumbnail((96, 96), Image.Resampling.LANCZOS)
            quantized = sample.quantize(colors=16)
            palette = quantized.getpalette() or []
            candidates = []
            for count, color_index in quantized.getcolors() or []:
                offset = color_index * 3
                red, green, blue = palette[offset : offset + 3]
                _, saturation, value = colorsys.rgb_to_hsv(
                    red / 255,
                    green / 255,
                    blue / 255,
                )
                if saturation < 0.22 or value < 0.24 or value > 0.96:
                    continue
                score = count * (0.4 + saturation) * (1 - abs(value - 0.68))
                candidates.append((score, red, green, blue))
        if candidates:
            _, red, green, blue = max(candidates)
            return (red << 16) | (green << 8) | blue
    except (OSError, ValueError):
        pass
    return fallback


class LeaderboardSetupModal(discord.ui.Modal):
    def __init__(
        self,
        cog,
        *,
        leaderboard_name: str,
        editing: bool,
        current_description: str = "",
        current_accent: str = "auto",
        image_url: Optional[str] = None,
        image_data: Optional[bytes] = None,
    ) -> None:
        super().__init__(
            title=("Edit leaderboard" if editing else "Create leaderboard")
        )
        self.cog = cog
        self.leaderboard_name = leaderboard_name
        self.editing = editing
        self.image_url = image_url
        self.image_data = image_data
        self.description = discord.ui.TextInput(
            label="Description",
            default=current_description[:MAX_LEADERBOARD_DESCRIPTION],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=MAX_LEADERBOARD_DESCRIPTION,
        )
        self.accent = discord.ui.TextInput(
            label="Accent color",
            default=current_accent or "auto",
            placeholder="auto or #F97316",
            max_length=7,
        )
        self.add_item(self.description)
        self.add_item(self.accent)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        accent_color = normalize_accent_color(str(self.accent.value))
        if accent_color is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid accent color",
                    "Enter `auto` or a six-digit hex color such as `#F97316`.",
                ),
                ephemeral=True,
            )
            return
        changed = await self.cog._save_leaderboard_presentation(
            name=self.leaderboard_name,
            header="Leaderboard",
            description=str(self.description.value).strip(),
            image_url=self.image_url,
            image_data=self.image_data,
            accent_color=accent_color,
            editing=self.editing,
        )
        if not changed:
            embed = warning_embed(
                "Leaderboard unavailable",
                "That leaderboard was changed or removed before the form was saved.",
            )
        else:
            embed = success_embed(
                "Leaderboard updated" if self.editing else "Leaderboard created",
                f"`{discord.utils.escape_markdown(self.leaderboard_name)}` is ready.",
            )
            if not self.editing and interaction.guild is not None:
                await publish_audit(
                    self.cog.bot,
                    interaction.guild,
                    "Leaderboard created",
                    (
                        f"Created by: {interaction.user.mention}\n"
                        f"Leaderboard: `{discord.utils.escape_markdown(self.leaderboard_name)}`\n"
                        f"Channel: {interaction.channel.mention}"
                    ),
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LeaderboardConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: "Leaderboards",
        requester_id: int,
        leaderboard: str,
        action: str,
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.requester_id = requester_id
        self.leaderboard = leaderboard
        self.action = action

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who started this confirmation can use it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.action == "reset":
            await self.cog._execute_reset(interaction, self.leaderboard)
        else:
            await self.cog._execute_delete(interaction, self.leaderboard)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            embed=warning_embed(
                f"Leaderboard {self.action} cancelled",
                "No changes were made.",
            ),
            view=None,
        )
        self.stop()


class Leaderboards(commands.Cog):
    leaderboard = app_commands.Group(
        name="leaderboard",
        description="Leaderboard management",
    )
    leaderboard_roles = app_commands.Group(
        name="role",
        description="Manage live leaderboard milestone roles",
        parent=leaderboard,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboards (
                name TEXT PRIMARY KEY,
                header TEXT,
                description TEXT,
                image_url TEXT,
                image_data BLOB,
                accent_color TEXT
            )
            """
        )
        cursor = await self.bot.db.execute("PRAGMA table_info(leaderboards)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for name, definition in (
            ("header", "TEXT"),
            ("description", "TEXT"),
            ("image_url", "TEXT"),
            ("image_data", "BLOB"),
            ("accent_color", "TEXT"),
        ):
            if name not in columns:
                await self.bot.db.execute(
                    f"ALTER TABLE leaderboards ADD COLUMN {name} {definition}"
                )
        await self.bot.db.execute(
            "UPDATE leaderboards SET header = name WHERE header IS NULL OR header = ''"
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER,
                leaderboard TEXT,
                points REAL,
                PRIMARY KEY (id, leaderboard)
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE TABLE IF NOT EXISTS leaderboard_role_milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                leaderboard TEXT NOT NULL,
                role_id TEXT NOT NULL,
                threshold REAL NOT NULL CHECK (threshold > 0),
                created_by_user_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, leaderboard, role_id),
                UNIQUE (guild_id, role_id)
            )
            """
        )
        await self.bot.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_leaderboard_role_milestones_lookup
            ON leaderboard_role_milestones (guild_id, leaderboard, threshold)
            """
        )
        await self.bot.db.commit()

    async def _require_manager(self, interaction: discord.Interaction) -> bool:
        if can_manage_leaderboards(interaction.user):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Staff control",
                "You need **Manage Server** to change leaderboards.",
            ),
            ephemeral=True,
        )
        return False

    async def _require_score_staff(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if is_configured_staff(interaction.user):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Staff-only command",
                (
                    "Only configured Owner, Administrator, Moderator, or "
                    "Staff roles can change leaderboard points."
                ),
            ),
            ephemeral=True,
        )
        return False

    async def _require_owner(self, interaction: discord.Interaction) -> bool:
        if is_configured_owner(interaction.user):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Owner-only command",
                "Only a configured bot owner can delete leaderboards.",
            ),
            ephemeral=True,
        )
        return False

    @staticmethod
    def _can_reset(member: discord.Member) -> bool:
        if is_configured_owner(member):
            return True
        permissions = getattr(member, "guild_permissions", None)
        if permissions and permissions.administrator:
            return True
        allowed = configured_admin_role_ids()
        allowed.update(get_csv_ids_setting("LEADERBOARD_RESET_ROLE_IDS"))
        return any(role.id in allowed for role in member.roles)

    async def _require_reset_access(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and self._can_reset(
            interaction.user
        ):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "Reset access required",
                "Only the configured Lead Host role, administrators, or Owner can reset leaderboards.",
            ),
            ephemeral=True,
        )
        return False

    async def _points_embed(self, member: discord.abc.User) -> discord.Embed:
        cursor = await self.bot.db.execute(
            """
            SELECT p.leaderboard, p.points,
                   1 + (
                       SELECT COUNT(*)
                       FROM points ranked
                       WHERE ranked.leaderboard = p.leaderboard
                         AND ranked.points > 0
                         AND (
                             ranked.points > p.points
                             OR (ranked.points = p.points AND ranked.id < p.id)
                         )
                   ) AS placement
            FROM points p
            WHERE p.id = ? AND p.points > 0
            ORDER BY p.leaderboard COLLATE NOCASE
            LIMIT 21
            """,
            (member.id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        display_name = discord.utils.escape_markdown(
            getattr(member, "display_name", getattr(member, "name", "Member"))
        )
        username = discord.utils.escape_markdown(
            getattr(member, "name", str(display_name))
        )
        streak_line = await self._points_streak_line(member.id)
        identity = f"**{display_name}** · @{username}\n{streak_line}"
        if rows:
            lines = [identity, ""]
            lines.extend(
                f"**{discord.utils.escape_markdown(str(name))}**: "
                f"`{format_points(value)} Points | #{int(placement)}`"
                for name, value, placement in rows[:20]
            )
            if len(rows) > 20:
                lines.append("*Showing the first 20 leaderboards.*")
            description = "\n".join(lines)
        else:
            description = f"{identity}\n\nNo leaderboard points yet."
        return discord.Embed(
            title="LEADERBOARD POINTS",
            description=description,
            color=COLOR,
        )

    async def _points_streak_line(self, user_id: int) -> str:
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'member_streaks'"
        )
        exists = await cursor.fetchone()
        await cursor.close()
        if not exists:
            return "🔥 Current streak: **0 days** · Longest: **0 days**"
        cursor = await self.bot.db.execute(
            """
            SELECT current_streak, longest_streak, last_qualified_date
            FROM member_streaks WHERE user_id = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (str(user_id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            current, longest = 0, 0
        else:
            current, longest, last_date = int(row[0]), int(row[1]), row[2]
            timezone_name = str(
                get_setting("STREAK_TIMEZONE", "America/Chicago") or ""
            )
            try:
                streak_timezone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                streak_timezone = ZoneInfo("America/Chicago")
            today = datetime.now(streak_timezone).date()
            if str(last_date or "") < (today - timedelta(days=1)).isoformat():
                current = 0
        return (
            f"🔥 Current streak: **{current} days** · "
            f"Longest: **{longest} days**"
        )

    @staticmethod
    def _role_management_error(
        guild: discord.Guild,
        actor: discord.Member,
        role: discord.Role,
    ) -> Optional[str]:
        bot_member = guild.me
        if role == guild.default_role:
            return "The `@everyone` role cannot be used as a milestone reward."
        if role.managed:
            return "That role is managed by Discord, a bot, or an integration."
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return "Bro Eden needs **Manage Roles** to manage milestone rewards."
        if role >= bot_member.top_role:
            return "Move the Bro Eden role above the selected reward role."
        if actor.id != guild.owner_id and role >= actor.top_role:
            return "The selected role must be below your highest Discord role."
        return None

    async def _milestone_rows(
        self,
        guild_id: int,
        leaderboard: Optional[str] = None,
    ) -> list[tuple]:
        if leaderboard is None:
            cursor = await self.bot.db.execute(
                """
                SELECT id, leaderboard, role_id, threshold
                FROM leaderboard_role_milestones
                WHERE guild_id = ?
                ORDER BY leaderboard COLLATE NOCASE, threshold, id
                """,
                (str(guild_id),),
            )
        else:
            cursor = await self.bot.db.execute(
                """
                SELECT id, leaderboard, role_id, threshold
                FROM leaderboard_role_milestones
                WHERE guild_id = ? AND leaderboard = ?
                ORDER BY threshold, id
                """,
                (str(guild_id), leaderboard),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _member_points(self, user_id: int, leaderboard: str) -> float:
        cursor = await self.bot.db.execute(
            "SELECT points FROM points WHERE id = ? AND leaderboard = ?",
            (user_id, leaderboard),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return float(row[0] or 0) if row else 0.0

    async def _reconcile_member_milestones(
        self,
        guild: discord.Guild,
        member: discord.Member,
        leaderboard: str,
    ) -> dict[str, int]:
        result = {"added": 0, "removed": 0, "failed": 0}
        points = await self._member_points(member.id, leaderboard)
        rows = await self._milestone_rows(guild.id, leaderboard)
        for _, _, role_id, threshold in rows:
            role = guild.get_role(int(role_id))
            if role is None:
                result["failed"] += 1
                logger.warning(
                    "Leaderboard milestone role missing guild_id=%s leaderboard=%r role_id=%s",
                    guild.id,
                    leaderboard,
                    role_id,
                )
                continue
            should_have_role = points >= float(threshold)
            has_role = role in member.roles
            if should_have_role == has_role:
                continue
            try:
                if should_have_role:
                    await member.add_roles(
                        role,
                        reason=(
                            f"Leaderboard milestone: {leaderboard} "
                            f"at {float(threshold):g} points"
                        ),
                    )
                    result["added"] += 1
                    logger.info(
                        "Leaderboard milestone role added guild_id=%s user_id=%s "
                        "leaderboard=%r role_id=%s threshold=%s points=%s",
                        guild.id,
                        member.id,
                        leaderboard,
                        role.id,
                        threshold,
                        points,
                    )
                    await publish_audit(
                        self.bot,
                        guild,
                        "Milestone role awarded",
                        (
                            f"Member: <@{member.id}>\nRole: {role.mention}\n"
                            f"Leaderboard: `{discord.utils.escape_markdown(leaderboard)}`\n"
                            f"Points: **{format_points(points)}**\n"
                            f"Requirement: **{format_points(threshold)}**"
                        ),
                    )
                else:
                    await member.remove_roles(
                        role,
                        reason=(
                            f"Below leaderboard milestone: {leaderboard} "
                            f"at {float(threshold):g} points"
                        ),
                    )
                    result["removed"] += 1
                    logger.info(
                        "Leaderboard milestone role removed guild_id=%s user_id=%s "
                        "leaderboard=%r role_id=%s threshold=%s points=%s",
                        guild.id,
                        member.id,
                        leaderboard,
                        role.id,
                        threshold,
                        points,
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                result["failed"] += 1
                logger.warning(
                    "Leaderboard milestone role update failed guild_id=%s user_id=%s "
                    "leaderboard=%r role_id=%s action=%s error=%s",
                    guild.id,
                    member.id,
                    leaderboard,
                    role.id,
                    "add" if should_have_role else "remove",
                    type(exc).__name__,
                )
        return result

    async def _sync_milestones(
        self,
        guild: discord.Guild,
        leaderboard: Optional[str] = None,
    ) -> dict[str, int]:
        summary = {"members": 0, "added": 0, "removed": 0, "failed": 0}
        milestones = await self._milestone_rows(guild.id, leaderboard)
        leaderboard_names = sorted({str(row[1]) for row in milestones})
        for name in leaderboard_names:
            cursor = await self.bot.db.execute(
                "SELECT id FROM points WHERE leaderboard = ?",
                (name,),
            )
            point_member_ids = {int(row[0]) for row in await cursor.fetchall()}
            await cursor.close()
            role_member_ids: set[int] = set()
            for row in milestones:
                if row[1] != name:
                    continue
                role = guild.get_role(int(row[2]))
                if role is not None:
                    role_member_ids.update(member.id for member in role.members)
            for member_id in sorted(point_member_ids | role_member_ids):
                member = guild.get_member(member_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(member_id)
                    except discord.NotFound:
                        continue
                    except (discord.Forbidden, discord.HTTPException):
                        summary["failed"] += 1
                        continue
                if member.bot:
                    continue
                outcome = await self._reconcile_member_milestones(
                    guild,
                    member,
                    name,
                )
                summary["members"] += 1
                for key in ("added", "removed", "failed"):
                    summary[key] += outcome[key]
        return summary

    async def _clear_milestone_roles(
        self,
        guild: discord.Guild,
        milestone_rows: list[tuple],
        *,
        reason: str,
    ) -> dict[str, int]:
        result = {"removed": 0, "failed": 0}
        for _, _, role_id, _ in milestone_rows:
            role = guild.get_role(int(role_id))
            if role is None:
                continue
            for member in list(role.members):
                try:
                    await member.remove_roles(role, reason=reason)
                    result["removed"] += 1
                except (discord.Forbidden, discord.HTTPException) as exc:
                    result["failed"] += 1
                    logger.warning(
                        "Leaderboard milestone cleanup failed guild_id=%s "
                        "user_id=%s role_id=%s error=%s",
                        guild.id,
                        member.id,
                        role.id,
                        type(exc).__name__,
                    )
        return result

    @staticmethod
    async def _read_banner_attachment(
        image: Optional[discord.Attachment],
    ) -> tuple[Optional[str], Optional[bytes]]:
        if image is None:
            return None, None
        is_image = bool(
            (image.content_type and image.content_type.startswith("image/"))
            or image.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")
            )
        )
        if not is_image:
            raise ValueError("The banner must be an image attachment.")
        if image.size > MAX_BANNER_BYTES:
            raise ValueError("The banner image must be 8 MB or smaller.")
        try:
            return image.url, await image.read()
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise ValueError("I could not download the selected banner image.") from exc

    async def _leaderboard_presentation(self, name: str):
        cursor = await self.bot.db.execute(
            """
            SELECT header, description, image_url, image_data, accent_color
            FROM leaderboards WHERE name = ?
            """,
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _save_leaderboard_presentation(
        self,
        *,
        name: str,
        header: str,
        description: str,
        image_url: Optional[str],
        image_data: Optional[bytes],
        accent_color: str,
        editing: bool,
    ) -> bool:
        if editing:
            cursor = await self.bot.db.execute(
                """
                UPDATE leaderboards
                SET header = ?, description = ?,
                    image_url = COALESCE(?, image_url),
                    image_data = COALESCE(?, image_data),
                    accent_color = ?
                WHERE name = ?
                """,
                (
                    header,
                    description,
                    image_url,
                    image_data,
                    accent_color,
                    name,
                ),
            )
        else:
            cursor = await self.bot.db.execute(
                """
                INSERT OR IGNORE INTO leaderboards (
                    name, header, description, image_url, image_data,
                    accent_color
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    header,
                    description,
                    image_url,
                    image_data,
                    accent_color,
                ),
            )
        changed = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        return changed

    @leaderboard_roles.command(
        name="add",
        description="Create an Owner-only live role milestone",
    )
    @app_commands.describe(
        leaderboard="Leaderboard that controls this reward",
        role="Role awarded at the milestone",
        points="Required point total, such as 500 or 1000.5",
    )
    @app_commands.guild_only()
    async def add_role_milestone(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        role: discord.Role,
        points: str,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        threshold = parse_points(points)
        if threshold is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid milestone",
                    "Enter a positive point requirement greater than zero.",
                ),
                ephemeral=True,
            )
            return
        role_error = self._role_management_error(
            interaction.guild,
            interaction.user,
            role,
        )
        if role_error:
            await interaction.response.send_message(
                embed=error_embed("Role cannot be managed", role_error),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cursor = await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO leaderboard_role_milestones (
                guild_id, leaderboard, role_id, threshold,
                created_by_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(interaction.guild_id),
                leaderboard,
                str(role.id),
                threshold,
                str(interaction.user.id),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        changed = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        if not changed:
            await interaction.followup.send(
                embed=warning_embed(
                    "Role milestone already configured",
                    (
                        "That role is already controlled by a leaderboard milestone. "
                        "Remove its existing rule before assigning it again."
                    ),
                ),
                ephemeral=True,
            )
            return
        logger.info(
            "Leaderboard milestone created guild_id=%s leaderboard=%r role_id=%s "
            "threshold=%s actor_id=%s",
            interaction.guild_id,
            leaderboard,
            role.id,
            threshold,
            interaction.user.id,
        )
        summary = await self._sync_milestones(interaction.guild, leaderboard)
        await interaction.followup.send(
            embed=success_embed(
                "Leaderboard role milestone created",
                (
                    f"Leaderboard: `{discord.utils.escape_markdown(leaderboard)}`\n"
                    f"Role: {role.mention}\n"
                    f"Required points: **{threshold:,}**\n\n"
                    "Members receive this role at or above the requirement and "
                    "lose it if they fall below it.\n"
                    f"Initial sync: **{summary['added']} added**, "
                    f"**{summary['removed']} removed**, **{summary['failed']} failed**."
                ),
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @leaderboard_roles.command(
        name="remove",
        description="Remove an Owner-only live role milestone",
    )
    @app_commands.describe(role="Configured milestone role to stop managing")
    @app_commands.guild_only()
    async def remove_role_milestone(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        cursor = await self.bot.db.execute(
            """
            DELETE FROM leaderboard_role_milestones
            WHERE guild_id = ? AND role_id = ?
            """,
            (str(interaction.guild_id), str(role.id)),
        )
        changed = cursor.rowcount
        await cursor.close()
        await self.bot.db.commit()
        if changed:
            logger.info(
                "Leaderboard milestone removed guild_id=%s role_id=%s actor_id=%s",
                interaction.guild_id,
                role.id,
                interaction.user.id,
            )
        if not changed:
            embed = warning_embed(
                "Milestone not found",
                f"{role.mention} is not controlled by a leaderboard milestone.",
            )
        else:
            embed = success_embed(
                "Leaderboard role milestone removed",
                (
                    f"{role.mention} is no longer controlled by leaderboard points. "
                    "Existing role assignments were left unchanged."
                ),
            )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @leaderboard_roles.command(
        name="list",
        description="List Owner-only live role milestones",
    )
    @app_commands.guild_only()
    async def list_role_milestones(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        rows = await self._milestone_rows(interaction.guild_id)
        if not rows:
            description = "No leaderboard role milestones are configured."
        else:
            lines = []
            for _, leaderboard, role_id, threshold in rows[:50]:
                role = interaction.guild.get_role(int(role_id))
                role_text = role.mention if role else f"Deleted role `{role_id}`"
                lines.append(
                    f"{role_text} — **{float(threshold):,g}** points in "
                    f"`{discord.utils.escape_markdown(str(leaderboard))}`"
                )
            if len(rows) > 50:
                lines.append(f"…and {len(rows) - 50} more rules.")
            description = "\n".join(lines)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Leaderboard role milestones",
                description=description[:4096],
                color=COLOR,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @leaderboard_roles.command(
        name="sync",
        description="Recheck all Owner-only leaderboard role milestones",
    )
    @app_commands.describe(
        leaderboard="Optional leaderboard to sync; omit to sync every milestone"
    )
    @app_commands.guild_only()
    async def sync_role_milestones(
        self,
        interaction: discord.Interaction,
        leaderboard: Optional[str] = None,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if leaderboard and not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await self._sync_milestones(interaction.guild, leaderboard)
        await interaction.followup.send(
            embed=success_embed(
                "Leaderboard roles synchronized",
                (
                    f"Members checked: **{summary['members']}**\n"
                    f"Roles added: **{summary['added']}**\n"
                    f"Roles removed: **{summary['removed']}**\n"
                    f"Failed updates: **{summary['failed']}**"
                ),
            ),
            ephemeral=True,
        )

    @leaderboard.command(name="create", description="Create a leaderboard")
    @app_commands.default_permissions(manage_guild=True)
    async def create_leaderboard(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, MAX_LEADERBOARD_NAME],
        image: Optional[discord.Attachment] = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        normalized = normalize_leaderboard_name(str(name))
        if normalized is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid name",
                    f"Use a name between 1 and {MAX_LEADERBOARD_NAME} characters.",
                ),
                ephemeral=True,
            )
            return
        if await self._leaderboard_exists(normalized):
            await interaction.response.send_message(
                embed=warning_embed(
                "Already exists",
                f"`{discord.utils.escape_markdown(normalized)}` already exists.",
                ),
                ephemeral=True,
            )
            return
        try:
            image_url, image_data = await self._read_banner_attachment(image)
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed("Invalid banner", str(exc)),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            LeaderboardSetupModal(
                self,
                leaderboard_name=normalized,
                editing=False,
                image_url=image_url,
                image_data=image_data,
            )
        )

    @leaderboard.command(
        name="edit",
        description="Edit a leaderboard description, accent, or banner",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def edit_leaderboard(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        image: Optional[discord.Attachment] = None,
    ) -> None:
        if not await self._require_manager(interaction):
            return
        presentation = await self._leaderboard_presentation(leaderboard)
        if presentation is None:
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        try:
            image_url, image_data = await self._read_banner_attachment(image)
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed("Invalid banner", str(exc)),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            LeaderboardSetupModal(
                self,
                leaderboard_name=leaderboard,
                editing=True,
                current_description=presentation[1] or "",
                current_accent=presentation[4] or "auto",
                image_url=image_url,
                image_data=image_data,
            )
        )

    @leaderboard.command(name="delete", description="Delete a leaderboard")
    async def delete_leaderboard(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self._require_owner(interaction):
            return
        if not await self._leaderboard_exists(name):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(name)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=warning_embed(
                "Confirm leaderboard deletion",
                (
                    f"Are you sure you wish to delete "
                    f"`{discord.utils.escape_markdown(name)}`? This cannot be undone."
                ),
            ),
            view=LeaderboardConfirmationView(
                self,
                interaction.user.id,
                name,
                "delete",
            ),
            ephemeral=True,
        )

    async def _execute_delete(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        milestone_rows = await self._milestone_rows(
            interaction.guild_id,
            name,
        )
        cursor = await self.bot.db.execute(
            "DELETE FROM leaderboards WHERE name = ?",
            (name,),
        )
        changed = cursor.rowcount
        await cursor.close()
        if changed:
            await self.bot.db.execute(
                "DELETE FROM points WHERE leaderboard = ?",
                (name,),
            )
            await self.bot.db.execute(
                "DELETE FROM leaderboard_role_milestones WHERE leaderboard = ?",
                (name,),
            )
            await self.bot.db.commit()
            cleanup = await self._clear_milestone_roles(
                interaction.guild,
                milestone_rows,
                reason=f"Leaderboard deleted: {name}",
            )
            embed = success_embed(
                "Leaderboard deleted",
                (
                    f"`{discord.utils.escape_markdown(name)}` and its scores were removed."
                    f"\nMilestone roles removed: **{cleanup['removed']}**; "
                    f"failed: **{cleanup['failed']}**."
                ),
            )
        else:
            embed = warning_embed(
                "Not found",
                f"`{discord.utils.escape_markdown(name)}` does not exist.",
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
        if changed:
            await publish_audit(
                self.bot,
                interaction.guild,
                "Leaderboard deleted",
                (
                    f"Deleted by: {interaction.user.mention}\n"
                    f"Leaderboard: `{discord.utils.escape_markdown(name)}`\n"
                    f"Milestone roles removed: **{cleanup['removed']}**\n"
                    f"Failed role removals: **{cleanup['failed']}**"
                ),
            )

    @leaderboard.command(name="reset", description="Reset all leaderboard points")
    @app_commands.describe(leaderboard="Leaderboard whose scores should be reset")
    @app_commands.guild_only()
    async def reset_leaderboard(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
    ) -> None:
        if not await self._require_reset_access(interaction):
            return
        if not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=warning_embed(
                "Confirm leaderboard reset",
                (
                    f"Are you sure you wish to reset "
                    f"`{discord.utils.escape_markdown(leaderboard)}`? "
                    "This cannot be undone."
                ),
            ),
            view=LeaderboardConfirmationView(
                self,
                interaction.user.id,
                leaderboard,
                "reset",
            ),
            ephemeral=True,
        )

    async def _execute_reset(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cursor = await self.bot.db.execute(
            "DELETE FROM points WHERE leaderboard = ?",
            (leaderboard,),
        )
        removed_scores = cursor.rowcount
        await cursor.close()
        await self.bot.db.commit()
        milestone_summary = await self._sync_milestones(
            interaction.guild,
            leaderboard,
        )
        await interaction.followup.send(
            embed=success_embed(
                "Leaderboard reset",
                (
                    f"Removed **{removed_scores}** score entries from "
                    f"`{discord.utils.escape_markdown(leaderboard)}`.\n"
                    f"Milestone roles removed: **{milestone_summary['removed']}**; "
                    f"failed: **{milestone_summary['failed']}**."
                ),
            ),
            ephemeral=True,
        )
        await publish_audit(
            self.bot,
            interaction.guild,
            "Leaderboard reset",
            (
                f"Reset by: {interaction.user.mention}\n"
                f"Leaderboard: `{discord.utils.escape_markdown(leaderboard)}`\n"
                f"Score entries removed: **{removed_scores}**\n"
                f"Milestone roles removed: **{milestone_summary['removed']}**\n"
                f"Failed role removals: **{milestone_summary['failed']}**"
            ),
        )

    @app_commands.command(
        name="leaderboards",
        description="Show a polished leaderboard",
    )
    async def show_leaderboard(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        await interaction.response.defer(thinking=True)
        if not await self._leaderboard_exists(name):
            await interaction.followup.send(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(name)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        file, view = await self.get_leaderboard_banner(
            name,
            0,
            guild=interaction.guild,
        )
        await interaction.followup.send(file=file, view=view)

    @app_commands.command(name="points", description="Show leaderboard points")
    @app_commands.describe(user="Member to inspect; omit to view your own points")
    @app_commands.guild_only()
    async def points_slash(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
    ) -> None:
        target = user or interaction.user
        await interaction.response.send_message(
            embed=await self._points_embed(target),
        )

    @commands.command(name="points", description="Show your leaderboard points")
    async def points_prefix(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        await ctx.send(embed=await self._points_embed(ctx.author))

    @leaderboard.command(name="add", description="Add points to a member")
    async def add_points(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        user: discord.Member,
        points: str,
    ) -> None:
        if not await self._require_score_staff(interaction):
            return
        parsed_points = parse_points(points)
        if parsed_points is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid points",
                    "Enter a positive finite number greater than zero.",
                ),
                ephemeral=True,
            )
            return
        if user.bot:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bots are excluded",
                    "Points can only be assigned to community members.",
                ),
                ephemeral=True,
            )
            return
        if not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.execute(
            """
            INSERT INTO points (id, leaderboard, points)
            VALUES (?, ?, ?)
            ON CONFLICT (id, leaderboard)
            DO UPDATE SET points = points + excluded.points
            """,
            (user.id, leaderboard, parsed_points),
        )
        await self.bot.db.commit()
        milestone_result = await self._reconcile_member_milestones(
            interaction.guild,
            user,
            leaderboard,
        )
        milestone_note = (
            "\nMilestone role update failed; check the bot logs and role hierarchy."
            if milestone_result["failed"]
            else ""
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Points added",
                f"Added **{parsed_points:,}** to {user.mention} in "
                f"`{discord.utils.escape_markdown(leaderboard)}`."
                f"{milestone_note}",
            ),
            ephemeral=True,
        )

    @leaderboard.command(name="remove", description="Remove points from a member")
    async def remove_points(
        self,
        interaction: discord.Interaction,
        leaderboard: str,
        user: discord.Member,
        points: str,
    ) -> None:
        if not await self._require_score_staff(interaction):
            return
        parsed_points = parse_points(points)
        if parsed_points is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid points",
                    "Enter a positive finite number greater than zero.",
                ),
                ephemeral=True,
            )
            return
        if user.bot:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bots are excluded",
                    "Points can only be removed from community members.",
                ),
                ephemeral=True,
            )
            return
        if not await self._leaderboard_exists(leaderboard):
            await interaction.response.send_message(
                embed=warning_embed(
                    "Leaderboard not found",
                    f"`{discord.utils.escape_markdown(leaderboard)}` does not exist.",
                ),
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            """
            INSERT INTO points (id, leaderboard, points)
            VALUES (?, ?, 0)
            ON CONFLICT(id, leaderboard)
            DO UPDATE SET points = MAX(0, points - ?)
            """,
            (user.id, leaderboard, parsed_points),
        )
        await self.bot.db.commit()
        milestone_result = await self._reconcile_member_milestones(
            interaction.guild,
            user,
            leaderboard,
        )
        milestone_note = (
            "\nMilestone role update failed; check the bot logs and role hierarchy."
            if milestone_result["failed"]
            else ""
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Points removed",
                f"Removed **{parsed_points:,}** from {user.mention} in "
                f"`{discord.utils.escape_markdown(leaderboard)}`."
                f"{milestone_note}",
            ),
            ephemeral=True,
        )

    async def _leaderboard_exists(self, name: str) -> bool:
        cursor = await self.bot.db.execute(
            "SELECT 1 FROM leaderboards WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    @delete_leaderboard.autocomplete("name")
    @edit_leaderboard.autocomplete("leaderboard")
    @show_leaderboard.autocomplete("name")
    @add_points.autocomplete("leaderboard")
    @remove_points.autocomplete("leaderboard")
    @reset_leaderboard.autocomplete("leaderboard")
    @add_role_milestone.autocomplete("leaderboard")
    @sync_role_milestones.autocomplete("leaderboard")
    async def leaderboard_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        cursor = await self.bot.db.execute(
            """
            SELECT name
            FROM leaderboards
            WHERE lower(name) LIKE ?
            ORDER BY name COLLATE NOCASE
            LIMIT 25
            """,
            (f"%{current.casefold().strip()}%",),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            app_commands.Choice(name=row[0], value=row[0])
            for row in rows
        ]

    @staticmethod
    def _token(name: str) -> str:
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

    async def _name_from_token(self, token: str) -> Optional[str]:
        cursor = await self.bot.db.execute("SELECT name FROM leaderboards")
        rows = await cursor.fetchall()
        await cursor.close()
        for (name,) in rows:
            if self._token(name) == token:
                return name
        return None

    async def get_leaderboard_banner(
        self,
        leaderboard: str,
        page: int,
        *,
        guild: Optional[discord.Guild] = None,
    ) -> tuple[discord.File, discord.ui.View]:
        get_cog = getattr(self.bot, "get_cog", None)
        bump_cog = get_cog("DisboardBumps") if callable(get_cog) else None
        bump_renderer = getattr(
            bump_cog,
            "render_managed_leaderboard_page",
            None,
        )
        if (
            guild is not None
            and leaderboard
            == getattr(bump_cog, "managed_leaderboard_name", None)
            and callable(bump_renderer)
        ):
            return await bump_renderer(guild, page)

        presentation = await self._leaderboard_presentation(leaderboard)
        description = presentation[1] if presentation and presentation[1] else ""
        banner_bytes = presentation[3] if presentation else None
        accent_color = resolve_leaderboard_accent(
            presentation[4] if presentation else "auto",
            banner_bytes,
        )
        cursor = await self.bot.db.execute(
            "SELECT COUNT(*) FROM points WHERE leaderboard = ? AND points > 0",
            (leaderboard,),
        )
        total_entries = int((await cursor.fetchone())[0])
        await cursor.close()
        total_pages = max(1, math.ceil(total_entries / PAGE_SIZE))
        page = min(max(0, page), total_pages - 1)
        cursor = await self.bot.db.execute(
            """
            SELECT id, points
            FROM points
            WHERE leaderboard = ? AND points > 0
            ORDER BY points DESC, id ASC
            LIMIT ? OFFSET ?
            """,
            (leaderboard, PAGE_SIZE, PAGE_SIZE * page),
        )
        rows = await cursor.fetchall()
        await cursor.close()

        items = []
        for user_id, points in rows:
            user = self.bot.get_user(user_id)
            formatted_points = f"{points:,.2f}".rstrip("0").rstrip(".")
            items.append(
                RankedGraphicItem(
                    label=(
                        user.display_name
                        if user is not None
                        else f"User {user_id}"
                    ),
                    value=formatted_points,
                    subtitle=(
                        f"@{user.name}"
                        if user is not None
                        else f"Discord ID {user_id}"
                    ),
                    avatar_url=(
                        str(user.display_avatar.replace(size=64).url)
                        if user is not None
                        else None
                    ),
                    score=float(points),
                )
            )
        png = await render_ranked_graphic(
            title="Leaderboard",
            subtitle=description or f"Page {page + 1} of {total_pages}",
            sections=[
                RankedGraphicSection(
                    "",
                    items,
                    rank_start=page * PAGE_SIZE + 1,
                )
            ],
            updated_at=datetime.now(timezone.utc),
            accent_color=accent_color,
            total_entries=total_entries,
            banner_bytes=banner_bytes,
            layout="leaderboard",
        )
        file = discord.File(io.BytesIO(png), filename="leaderboard.png")
        token = self._token(leaderboard)
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Previous",
                emoji="◀️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"leaderboard|prev|{page}|{token}",
                disabled=page == 0,
            )
        )
        view.add_item(
            discord.ui.Button(
                label=f"Page {page + 1} of {total_pages}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"leaderboard|page|{page}|{token}",
                disabled=True,
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Next",
                emoji="▶️",
                style=discord.ButtonStyle.primary,
                custom_id=f"leaderboard|next|{page}|{token}",
                disabled=page + 1 >= total_pages,
            )
        )
        return file, view

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        if not custom_id.startswith("leaderboard|"):
            return
        parts = custom_id.split("|")
        if (
            len(parts) != 4
            or parts[1] not in {"prev", "next"}
            or not parts[2].isdigit()
        ):
            return
        name = await self._name_from_token(parts[3])
        if name is None:
            await interaction.response.send_message(
                "This leaderboard is no longer available.",
                ephemeral=True,
            )
            return
        page = int(parts[2]) + (1 if parts[1] == "next" else -1)
        await interaction.response.defer()
        file, view = await self.get_leaderboard_banner(
            name,
            page,
            guild=interaction.guild,
        )
        await interaction.edit_original_response(attachments=[file], view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboards(bot))
