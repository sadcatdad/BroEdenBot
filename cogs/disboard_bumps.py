"""Verified DISBOARD bump rewards and the Bump Legends leaderboard."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

from utils.ranked_graphic import (
    RankedGraphicItem,
    RankedGraphicSection,
    render_ranked_graphic,
)
from utils.settings import get_int_setting, get_setting
from utils.embed_templates import (
    discord_embeds_from_payload,
    discord_view_from_payload,
    get_embed_template,
    render_feature_payload,
)


logger = logging.getLogger(__name__)
BUMP_LEADERBOARD_NAME = "👑 BUMP LEGENDS 👑"
BUMP_LEADERBOARD_TITLE = "👑 Bump Legends Leaderboard"
LEGACY_BUMP_LEADERBOARD_NAME = "Bump Champions"
PREVIOUS_BUMP_LEADERBOARD_NAME = "Bump Legends"
PREVIOUS_BRANDED_BUMP_LEADERBOARD_NAME = "👑 Bump Legends 👑"
LEGACY_BUMP_LEADERBOARD_NAMES = (
    PREVIOUS_BRANDED_BUMP_LEADERBOARD_NAME,
    PREVIOUS_BUMP_LEADERBOARD_NAME,
    LEGACY_BUMP_LEADERBOARD_NAME,
)
BUMP_SUBTITLE = (
    "Top members supporting the server through DISBOARD bumps.\n"
    "1 bump = 1,000 points"
)
BUMP_FOOTER = "!bumpscores = show leaderboard"
BUMP_ACCENT_COLOR = 0x25B8B8
BUMP_SUCCESS_MESSAGE_DEFAULT = (
    "Thanks for bumping our server, {member}! You gained:\n"
    "- 💥 + {points} Bump Points\n"
    "{reward_status}\n"
    "A bump reminder will be posted in 2 hours."
)
BUMP_REMINDER_DELAY = timedelta(hours=2)
BUMP_REMINDER_BATCH_SIZE = 25
BUMP_REMINDER_MAX_ATTEMPTS = 3
BUMP_BACKGROUND_PATHS = (
    Path(__file__).resolve().parent.parent / "assets" / "bump_leaderboard_background.png",
    Path(__file__).resolve().parent.parent / "assets" / "bump_leaderboard_background",
)
SUCCESS_RE = re.compile(
    r"(?:\bbump\s+done!|"
    r"\bthx\s+for\s+bumping\s+our\s+server!\s*"
    r"we\s+will\s+remind\s+you\s+in\s+2\s+hours!)",
    re.IGNORECASE,
)
PAGE_SIZE = 10


@lru_cache(maxsize=1)
def _bump_background_bytes() -> Optional[bytes]:
    for path in BUMP_BACKGROUND_PATHS:
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            logger.warning("Could not read bump leaderboard background at %s", path)
    return None


def _configured_id(key: str) -> int:
    raw = str(get_setting(key, "") or "").strip()
    return int(raw) if raw.isdigit() else 0


def _bump_subtitle() -> str:
    points = max(1, get_int_setting("BUMP_POINTS_PER_SUCCESS", 1000))
    return (
        "Top members supporting the server through DISBOARD bumps.\n"
        f"1 bump = {points:,} points"
    )


def _publisher_timezone() -> ZoneInfo:
    name = str(get_setting("REMINDER_TIMEZONE", "America/Chicago") or "").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid REMINDER_TIMEZONE %r; using America/Chicago", name)
        return ZoneInfo("America/Chicago")


def _response_text(message: discord.Message) -> str:
    parts = [str(message.content or "")]
    for embed in message.embeds:
        parts.extend((str(embed.title or ""), str(embed.description or "")))
        for field in embed.fields:
            parts.extend((str(field.name or ""), str(field.value or "")))
    return "\n".join(parts)


class DisboardBumps(commands.Cog):
    managed_leaderboard_name = BUMP_LEADERBOARD_NAME

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS leaderboards (
                name TEXT PRIMARY KEY,
                header TEXT,
                description TEXT,
                image_url TEXT,
                image_data BLOB,
                accent_color TEXT
            );
            CREATE TABLE IF NOT EXISTS points (
                id INTEGER,
                leaderboard TEXT,
                points REAL,
                PRIMARY KEY (id, leaderboard)
            );
            CREATE TABLE IF NOT EXISTS disboard_bump_events (
                response_message_id TEXT PRIMARY KEY,
                interaction_id TEXT,
                guild_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                bumped_at TEXT NOT NULL,
                points_awarded INTEGER NOT NULL,
                role_id TEXT,
                role_status TEXT NOT NULL,
                role_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_disboard_bump_member_time
                ON disboard_bump_events (guild_id, member_id, bumped_at DESC);
            CREATE TABLE IF NOT EXISTS disboard_bump_reminders (
                response_message_id TEXT PRIMARY KEY,
                prompt_message_id TEXT,
                guild_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_choice',
                choice_at TEXT,
                leaderboard_used INTEGER NOT NULL DEFAULT 0,
                leaderboard_used_at TEXT,
                leaderboard_used_by TEXT,
                claimed_at TEXT,
                sent_at TEXT,
                reminder_message_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_disboard_bump_reminders_due
                ON disboard_bump_reminders (status, due_at);
            CREATE TABLE IF NOT EXISTS bump_leaderboard_post_state (
                guild_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                last_posted_at TEXT NOT NULL
            );
            """
        )
        cursor = await self.bot.db.execute("PRAGMA table_info(leaderboards)")
        leaderboard_columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        for name, definition in (
            ("header", "TEXT"),
            ("description", "TEXT"),
            ("image_url", "TEXT"),
            ("image_data", "BLOB"),
            ("accent_color", "TEXT"),
        ):
            if name not in leaderboard_columns:
                await self.bot.db.execute(
                    f"ALTER TABLE leaderboards ADD COLUMN {name} {definition}"
                )
        # Bro Eden may already have custom leaderboards whose names match old
        # RiffBot defaults. Do not rename or delete them during installation.
        await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO leaderboards (
                name, header, description, accent_color
            ) VALUES (?, ?, ?, ?)
            """,
            (
                BUMP_LEADERBOARD_NAME,
                "Leaderboard",
                _bump_subtitle(),
                "auto",
            ),
        )
        await self.bot.db.execute(
            "UPDATE leaderboards SET description = ? WHERE name = ?",
            (_bump_subtitle(), BUMP_LEADERBOARD_NAME),
        )
        await self.bot.db.commit()
        if not self.weekly_publisher.is_running():
            self.weekly_publisher.start()
        if not self.reminder_worker.is_running():
            self.reminder_worker.start()

    async def _migrate_legacy_leaderboard_name(self) -> None:
        """Intentionally preserve same-named destination leaderboards.

        The transfer source used destructive rename/delete migrations for its own
        historical names. In Bro Eden those names may belong to unrelated custom
        boards, so installation creates the canonical board additively instead.
        """
        return None

    def cog_unload(self) -> None:
        self.weekly_publisher.cancel()
        self.reminder_worker.cancel()

    @staticmethod
    def _prompt_view(
        response_message_id: str,
        *,
        choice_made: bool = False,
        leaderboard_used: bool = False,
        template_payload=None,
    ) -> discord.ui.View:
        view = None
        if template_payload:
            view_payload = {
                "content": template_payload.get("content", ""),
                "embeds": list(template_payload.get("embeds") or []),
                "buttons": list(template_payload.get("buttons") or [])[:4],
            }
            view = discord_view_from_payload(view_payload)
        if view is None:
            view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Bump Leaderboard",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bumpleaderboard|show|{response_message_id}",
            disabled=leaderboard_used,
        ))
        return view

    async def _send_bump_prompt(
        self,
        message: discord.Message,
        member: discord.Member,
        role_status: str,
    ) -> None:
        response_id = str(message.id)
        points = max(1, get_int_setting("BUMP_POINTS_PER_SUCCESS", 1000))
        content = self._success_content(member, points, role_status)
        embeds = []
        reward_role = None
        template_payload = await self._configured_success_payload()
        if template_payload:
            try:
                reward_role_id = _configured_id("BUMP_REWARD_ROLE_ID")
                reward_role = member.guild.get_role(reward_role_id) if reward_role_id else None
                reward_status_text = (
                    "- Your configured bump reward role was awarded"
                    if role_status == "awarded"
                    else "- Your Bump Points were saved; staff can check the reward role setup"
                )
                template_payload = render_feature_payload(
                    template_payload,
                    user_mention=member.mention,
                    role_mentions=[reward_role.mention] if reward_role is not None else [],
                    placeholders={
                        "member": member.mention,
                        "role": reward_role.mention if reward_role is not None else "",
                        "points": f"{points:,}",
                        "reward_status": reward_status_text,
                    },
                )
                content = template_payload["content"]
                embeds = discord_embeds_from_payload(template_payload)
                view = self._prompt_view(
                    response_id,
                    template_payload=template_payload,
                )
            except ValueError:
                logger.warning("Configured successful bump response payload is invalid")
                template_payload = None
        if not template_payload:
            view = self._prompt_view(response_id)
        reply_kwargs = {
            "mention_author": False,
            "view": view,
            "allowed_mentions": discord.AllowedMentions(
                users=[member],
                roles=[reward_role] if template_payload and reward_role is not None else False,
                everyone=False,
            ),
        }
        if embeds:
            reply_kwargs["embeds"] = embeds
        try:
            prompt = await message.reply(
                content or None,
                **reply_kwargs,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "Could not publish bump response guild_id=%s member_id=%s "
                "response_id=%s error=%s",
                message.guild.id,
                member.id,
                message.id,
                type(exc).__name__,
            )
            return
        await self.bot.db.execute(
            """
            UPDATE disboard_bump_reminders
            SET prompt_message_id = ?
            WHERE response_message_id = ?
            """,
            (str(prompt.id), response_id),
        )
        await self.bot.db.commit()

    @staticmethod
    def _success_content(
        member: discord.Member,
        points: int,
        role_status: str,
    ) -> str:
        if role_status == "awarded":
            reward_status = "- Your configured bump reward role was awarded"
        else:
            reward_status = (
                "- Your Bump Points were saved; staff can check the reward role setup"
            )
        return (
            BUMP_SUCCESS_MESSAGE_DEFAULT.replace("{member}", member.mention)
            .replace("{points}", f"{points:,}")
            .replace("{reward_status}", reward_status)
            .strip()
        )

    @staticmethod
    def _invoking_user(message: discord.Message):
        legacy = getattr(message, "interaction", None)
        if legacy is not None:
            if str(getattr(legacy, "name", "")).casefold() != "bump":
                return None, None
            return getattr(legacy, "user", None), getattr(legacy, "id", None)
        metadata = getattr(message, "interaction_metadata", None)
        if metadata is None:
            return None, None
        command_name = str(getattr(metadata, "name", "") or "")
        if command_name and command_name.casefold() != "bump":
            return None, None
        return getattr(metadata, "user", None), getattr(metadata, "id", None)

    async def _verified_bump(self, message: discord.Message):
        if message.guild is None:
            return None, None
        trusted_bot_id = _configured_id("DISBOARD_BOT_USER_ID")
        if not trusted_bot_id or message.author.id != trusted_bot_id:
            return None, None
        if not SUCCESS_RE.search(_response_text(message)):
            return None, None
        user, interaction_id = self._invoking_user(message)
        if user is None:
            mentioned_members = [
                member
                for member in getattr(message, "mentions", [])
                if isinstance(member, discord.Member) and not member.bot
            ]
            if len(mentioned_members) == 1:
                user = mentioned_members[0]
        if user is None:
            logger.warning(
                "Verified DISBOARD success could not resolve invoking member "
                "guild_id=%s response_id=%s",
                message.guild.id,
                message.id,
            )
            return None, None
        member = message.guild.get_member(user.id)
        if member is None:
            try:
                member = await message.guild.fetch_member(user.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None, None
        if member.bot:
            return None, None
        return member, interaction_id

    async def _grant_reward_role(
        self,
        member: discord.Member,
    ) -> tuple[int, str, Optional[str]]:
        role_id = _configured_id("BUMP_REWARD_ROLE_ID")
        role = member.guild.get_role(role_id) if role_id else None
        if role is None:
            return role_id, "failed", "Configured reward role was not found."
        bot_member = member.guild.me
        if (
            bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
            or role.managed
        ):
            return role_id, "failed", "Bot cannot manage the configured reward role."
        try:
            await member.add_roles(role, reason="Verified DISBOARD bump reward pulse")
            return role_id, "awarded", None
        except (discord.Forbidden, discord.HTTPException) as exc:
            return role_id, "failed", type(exc).__name__

    async def _process_bump(self, message: discord.Message) -> bool:
        member, interaction_id = await self._verified_bump(message)
        if member is None:
            return False
        points = max(1, get_int_setting("BUMP_POINTS_PER_SUCCESS", 1000))
        configured_role_id = _configured_id("BUMP_REWARD_ROLE_ID")
        cursor = await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO disboard_bump_events (
                response_message_id, interaction_id, guild_id, member_id,
                channel_id, bumped_at, points_awarded, role_id,
                role_status, role_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
            """,
            (
                str(message.id),
                str(interaction_id) if interaction_id is not None else None,
                str(message.guild.id),
                str(member.id),
                str(message.channel.id),
                message.created_at.astimezone(timezone.utc).isoformat(),
                points,
                str(configured_role_id) if configured_role_id else None,
            ),
        )
        inserted = cursor.rowcount > 0
        await cursor.close()
        if not inserted:
            return False
        due_at = message.created_at.astimezone(timezone.utc) + BUMP_REMINDER_DELAY
        await self.bot.db.execute(
            """
            INSERT INTO disboard_bump_reminders (
                response_message_id, guild_id, member_id, channel_id, due_at,
                status
            ) VALUES (?, ?, ?, ?, ?, 'scheduled')
            """,
            (
                str(message.id),
                str(message.guild.id),
                str(member.id),
                str(message.channel.id),
                due_at.isoformat(),
            ),
        )
        await self.bot.db.execute(
            """
            INSERT INTO points (id, leaderboard, points)
            VALUES (?, ?, ?)
            ON CONFLICT (id, leaderboard)
            DO UPDATE SET points = points + excluded.points
            """,
            (member.id, BUMP_LEADERBOARD_NAME, points),
        )
        await self.bot.db.commit()
        leaderboard_cog = self.bot.get_cog("Leaderboards")
        if leaderboard_cog is not None:
            await leaderboard_cog._reconcile_member_milestones(
                message.guild,
                member,
                BUMP_LEADERBOARD_NAME,
            )
        role_id, role_status, role_error = await self._grant_reward_role(member)
        await self.bot.db.execute(
            """
            UPDATE disboard_bump_events
            SET role_id = ?, role_status = ?, role_error = ?
            WHERE response_message_id = ?
            """,
            (str(role_id) if role_id else None, role_status, role_error, str(message.id)),
        )
        await self.bot.db.commit()
        if role_error:
            logger.warning(
                "Bump reward role failed guild_id=%s member_id=%s response_id=%s error=%s",
                message.guild.id,
                member.id,
                message.id,
                role_error,
            )
        else:
            logger.info(
                "DISBOARD bump rewarded guild_id=%s member_id=%s response_id=%s "
                "points=%s role_status=%s",
                message.guild.id,
                member.id,
                message.id,
                points,
                role_status,
            )
        await self._send_bump_prompt(message, member, role_status)
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._process_bump(message)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.bot.db.execute(
            "DELETE FROM points WHERE id = ? AND leaderboard = ?",
            (member.id, BUMP_LEADERBOARD_NAME),
        )
        await self.bot.db.commit()
        leaderboard_cog = self.bot.get_cog("Leaderboards")
        if leaderboard_cog is not None:
            await leaderboard_cog._reconcile_member_milestones(
                member.guild,
                member,
                BUMP_LEADERBOARD_NAME,
            )

    async def _prune_departed_members(self, guild: discord.Guild) -> None:
        """Remove departed members from scores while retaining bump event history."""
        cursor = await self.bot.db.execute(
            "SELECT id FROM points WHERE leaderboard = ?",
            (BUMP_LEADERBOARD_NAME,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        departed = [(int(user_id), BUMP_LEADERBOARD_NAME) for (user_id,) in rows
                    if guild.get_member(int(user_id)) is None]
        if departed:
            await self.bot.db.executemany(
                "DELETE FROM points WHERE id = ? AND leaderboard = ?",
                departed,
            )
            await self.bot.db.commit()

    async def _graphic_page(
        self,
        guild: discord.Guild,
        page: int = 0,
    ) -> tuple[discord.File, discord.ui.View]:
        await self._prune_departed_members(guild)
        cursor = await self.bot.db.execute(
            "SELECT COUNT(*) FROM points WHERE leaderboard = ? AND points > 0",
            (BUMP_LEADERBOARD_NAME,),
        )
        total_entries = int((await cursor.fetchone())[0])
        await cursor.close()
        total_pages = max(1, (total_entries + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(0, page), total_pages - 1)
        cursor = await self.bot.db.execute(
            """
            SELECT id, points FROM points
            WHERE leaderboard = ? AND points > 0
            ORDER BY points DESC, id ASC
            LIMIT ? OFFSET ?
            """,
            (BUMP_LEADERBOARD_NAME, PAGE_SIZE, page * PAGE_SIZE),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        items: list[RankedGraphicItem] = []
        for user_id, points in rows:
            member = guild.get_member(int(user_id))
            if member is None:
                continue
            formatted = f"{float(points):,.2f}".rstrip("0").rstrip(".")
            items.append(
                RankedGraphicItem(
                    label=member.name,
                    subtitle="",
                    value=formatted,
                    avatar_url=str(member.display_avatar.replace(size=64).url),
                    score=float(points),
                )
            )
        png = await render_ranked_graphic(
            title=BUMP_LEADERBOARD_TITLE,
            subtitle=_bump_subtitle(),
            sections=[RankedGraphicSection("", items, rank_start=page * PAGE_SIZE + 1)],
            updated_at=datetime.now(timezone.utc),
            accent_color=BUMP_ACCENT_COLOR,
            total_entries=total_entries,
            background_bytes=await asyncio.to_thread(_bump_background_bytes),
            layout="leaderboard",
            footer_text=BUMP_FOOTER,
            force_columns=2,
            template_key="bump_leaderboard",
        )
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Previous", emoji="◀️", style=discord.ButtonStyle.secondary,
            custom_id=f"bumpscores|prev|{page}", disabled=page == 0,
        ))
        view.add_item(discord.ui.Button(
            label=f"Page {page + 1} of {total_pages}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bumpscores|page|{page}", disabled=True,
        ))
        view.add_item(discord.ui.Button(
            label="Next", emoji="▶️", style=discord.ButtonStyle.primary,
            custom_id=f"bumpscores|next|{page}", disabled=page + 1 >= total_pages,
        ))
        return discord.File(io.BytesIO(png), filename="bump-champions.png"), view

    async def render_managed_leaderboard_page(
        self,
        guild: discord.Guild,
        page: int = 0,
    ) -> tuple[discord.File, discord.ui.View]:
        """Render the canonical Bump Legends view for other bot surfaces."""
        return await self._graphic_page(guild, page)

    @commands.command(
        name="bumpscores",
        description="Show the current Bump Legends Leaderboard",
    )
    async def bump_scores(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            return
        file, view = await self._graphic_page(ctx.guild)
        await ctx.send(file=file, view=view)

    async def _edit_prompt_buttons(
        self,
        interaction: discord.Interaction,
        response_id: str,
    ) -> None:
        cursor = await self.bot.db.execute(
            """
            SELECT status, leaderboard_used
            FROM disboard_bump_reminders
            WHERE response_message_id = ?
            """,
            (response_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or interaction.message is None:
            return
        template_payload = await self._configured_success_payload()
        if template_payload and interaction.guild is not None:
            reward_role_id = _configured_id("BUMP_REWARD_ROLE_ID")
            reward_role = interaction.guild.get_role(reward_role_id) if reward_role_id else None
            user_mention = str(
                getattr(interaction.user, "mention", f"<@{interaction.user.id}>")
            )
            try:
                template_payload = render_feature_payload(
                    template_payload,
                    user_mention=user_mention,
                    role_mentions=[reward_role.mention] if reward_role is not None else [],
                    placeholders={
                        "member": user_mention,
                        "role": reward_role.mention if reward_role is not None else "",
                    },
                )
            except ValueError:
                template_payload = None
        try:
            view = self._prompt_view(
                response_id,
                choice_made=str(row[0]) != "pending_choice",
                leaderboard_used=bool(row[1]),
                template_payload=template_payload,
            )
        except ValueError:
            logger.warning("Configured successful bump response payload is invalid")
            view = self._prompt_view(
                response_id,
                choice_made=str(row[0]) != "pending_choice",
                leaderboard_used=bool(row[1]),
            )
        try:
            await interaction.message.edit(view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def _handle_reminder_choice(
        self,
        interaction: discord.Interaction,
        choice: str,
        response_id: str,
    ) -> None:
        cursor = await self.bot.db.execute(
            """
            SELECT guild_id, member_id, status
            FROM disboard_bump_reminders
            WHERE response_message_id = ?
            """,
            (response_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or interaction.guild is None or str(interaction.guild.id) != str(row[0]):
            await interaction.response.send_message(
                "This bump reminder is no longer available.", ephemeral=True,
            )
            return
        if str(interaction.user.id) != str(row[1]):
            await interaction.response.send_message(
                "Only the member who bumped can choose this reminder.", ephemeral=True,
            )
            return
        if str(row[2]) != "pending_choice":
            await interaction.response.send_message(
                "Your reminder choice has already been recorded.", ephemeral=True,
            )
            return
        now = datetime.now(timezone.utc)
        status = "scheduled" if choice == "yes" else "declined"
        due_at = now + BUMP_REMINDER_DELAY
        cursor = await self.bot.db.execute(
            """
            UPDATE disboard_bump_reminders
            SET status = ?, choice_at = ?, due_at = ?
            WHERE response_message_id = ?
              AND member_id = ?
              AND status = 'pending_choice'
            """,
            (status, now.isoformat(), due_at.isoformat(), response_id, str(interaction.user.id)),
        )
        updated = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        if not updated:
            await interaction.response.send_message(
                "Your reminder choice has already been recorded.", ephemeral=True,
            )
            return
        confirmation = (
            "✅ Great! We will remind you in 2 hours."
            if choice == "yes"
            else "❤️ No problem! You can also ignore voting to default to No."
        )
        await interaction.response.send_message(confirmation, ephemeral=True)
        await self._edit_prompt_buttons(interaction, response_id)

    async def _handle_prompt_leaderboard(
        self,
        interaction: discord.Interaction,
        response_id: str,
    ) -> None:
        if interaction.guild is None:
            return
        cursor = await self.bot.db.execute(
            """
            UPDATE disboard_bump_reminders
            SET leaderboard_used = 1,
                leaderboard_used_at = ?,
                leaderboard_used_by = ?
            WHERE response_message_id = ?
              AND guild_id = ?
              AND leaderboard_used = 0
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(interaction.user.id),
                response_id,
                str(interaction.guild.id),
            ),
        )
        claimed = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        if not claimed:
            await interaction.response.send_message(
                "The Bump Leaderboard button has already been used.", ephemeral=True,
            )
            await self._edit_prompt_buttons(interaction, response_id)
            return
        await interaction.response.defer()
        try:
            file, view = await self._graphic_page(interaction.guild)
            await interaction.followup.send(file=file, view=view)
        except Exception:
            logger.exception(
                "Bump prompt leaderboard failed guild_id=%s response_id=%s",
                interaction.guild.id,
                response_id,
            )
            await self.bot.db.execute(
                """
                UPDATE disboard_bump_reminders
                SET leaderboard_used = 0,
                    leaderboard_used_at = NULL,
                    leaderboard_used_by = NULL
                WHERE response_message_id = ?
                """,
                (response_id,),
            )
            await self.bot.db.commit()
            await interaction.followup.send(
                "The leaderboard could not be displayed right now. Please try again.",
                ephemeral=True,
            )
            return
        await self._edit_prompt_buttons(interaction, response_id)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        parts = custom_id.split("|")
        if (
            len(parts) == 3
            and parts[0] == "embedrole"
            and parts[1] in {"add", "remove"}
            and parts[2].isdigit()
        ):
            await self._handle_embed_role(interaction, parts[1], int(parts[2]))
            return
        if (
            len(parts) == 3
            and parts[0] == "bumpreminder"
            and parts[1] in {"yes", "no"}
        ):
            await self._handle_reminder_choice(interaction, parts[1], parts[2])
            return
        if len(parts) == 3 and parts[:2] == ["bumpleaderboard", "show"]:
            await self._handle_prompt_leaderboard(interaction, parts[2])
            return
        if len(parts) != 3 or parts[0] != "bumpscores" or parts[1] not in {"prev", "next"}:
            return
        if interaction.guild is None or not parts[2].isdigit():
            return
        current_page = int(parts[2])
        target_page = current_page - 1 if parts[1] == "prev" else current_page + 1
        await interaction.response.defer()
        file, view = await self._graphic_page(interaction.guild, target_page)
        await interaction.edit_original_response(attachments=[file], view=view)

    @staticmethod
    def _reminder_embed() -> discord.Embed:
        return discord.Embed(
            description=(
                "# 🔔 BUMP TIME 🔔\n"
                "# ➡️ If you see this, use `/bump`\n"
                "## 🎉 Help more friends find the community\n"
                "### 💥 Earn Bump Points and trigger the configured reward role."
            ),
            color=BUMP_ACCENT_COLOR,
        )

    async def _handle_embed_role(
        self,
        interaction: discord.Interaction,
        action: str,
        role_id: int,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "This role button can only be used in the server.", ephemeral=True,
            )
            return
        role = interaction.guild.get_role(role_id)
        bot_member = interaction.guild.me
        if (
            role is None
            or bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
            or role.managed
        ):
            await interaction.response.send_message(
                "That role is unavailable or cannot be managed by the bot.", ephemeral=True,
            )
            return
        try:
            if action == "add":
                if role in interaction.user.roles:
                    message = f"You already have {role.mention}."
                else:
                    await interaction.user.add_roles(
                        role, reason="Self-service embed role button",
                    )
                    message = f"Added {role.mention}."
            elif role not in interaction.user.roles:
                message = f"You do not have {role.mention}."
            else:
                await interaction.user.remove_roles(
                    role, reason="Self-service embed role button",
                )
                message = f"Removed {role.mention}."
        except discord.Forbidden:
            message = "I do not have permission to update that role."
        except discord.HTTPException:
            message = "Discord could not update that role right now. Please try again."
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _configured_asset_payload(
        self,
        setting_key: str,
        feature_label: str,
        *,
        legacy_setting_key: str = "",
        legacy_message_key: str = "",
        legacy_message_default: str = "",
        ignore_legacy_buttons: bool = False,
    ):
        template_id = _configured_id(setting_key)
        using_legacy = False
        if not template_id and legacy_setting_key:
            template_id = _configured_id(legacy_setting_key)
            using_legacy = bool(template_id)
        if not template_id:
            return None
        try:
            template = await asyncio.to_thread(get_embed_template, template_id)
        except (OSError, sqlite3.Error):
            logger.exception(
                "Could not load configured %s asset id=%s",
                feature_label,
                template_id,
            )
            return None
        if template is None:
            logger.warning(
                "Configured %s asset was not found id=%s",
                feature_label,
                template_id,
            )
            return None
        payload = template["payload"]
        if using_legacy and legacy_message_key:
            payload = {
                **payload,
                "content": str(
                    get_setting(legacy_message_key, legacy_message_default)
                    or legacy_message_default
                ).strip(),
                "buttons": [] if ignore_legacy_buttons else list(payload.get("buttons") or []),
            }
        return payload

    async def _configured_reminder_payload(self):
        return await self._configured_asset_payload(
            "BUMP_REMINDER_ASSET_ID",
            "bump reminder",
            legacy_setting_key="BUMP_REMINDER_EMBED_ID",
            legacy_message_key="BUMP_REMINDER_MESSAGE",
            legacy_message_default="{role}",
            ignore_legacy_buttons=True,
        )

    async def _configured_success_payload(self):
        return await self._configured_asset_payload(
            "BUMP_SUCCESS_ASSET_ID",
            "successful bump response",
            legacy_setting_key="BUMP_SUCCESS_EMBED_ID",
            legacy_message_key="BUMP_SUCCESS_MESSAGE",
            legacy_message_default=BUMP_SUCCESS_MESSAGE_DEFAULT,
        )

    @staticmethod
    def _reminder_content(member: discord.Member, role) -> str:
        return "{role}".replace("{member}", member.mention).replace(
            "{role}", role.mention if role is not None else "",
        ).strip()

    async def _retry_or_fail_reminder(
        self,
        response_id: str,
        attempt_count: int,
        reason: str,
        *,
        retryable: bool,
    ) -> None:
        if retryable and attempt_count < BUMP_REMINDER_MAX_ATTEMPTS:
            retry_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** attempt_count)
            await self.bot.db.execute(
                """
                UPDATE disboard_bump_reminders
                SET status = 'scheduled', due_at = ?, claimed_at = NULL,
                    last_error = ?
                WHERE response_message_id = ? AND status = 'processing'
                """,
                (retry_at.isoformat(), reason, response_id),
            )
        else:
            await self.bot.db.execute(
                """
                UPDATE disboard_bump_reminders
                SET status = 'failed', last_error = ?
                WHERE response_message_id = ? AND status = 'processing'
                """,
                (reason, response_id),
            )
        await self.bot.db.commit()

    async def _process_due_reminders(self) -> None:
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(minutes=10)
        await self.bot.db.execute(
            """
            UPDATE disboard_bump_reminders
            SET status = 'scheduled', claimed_at = NULL,
                last_error = 'Recovered after interrupted processing.'
            WHERE status = 'processing' AND claimed_at < ?
            """,
            (stale_before.isoformat(),),
        )
        await self.bot.db.commit()
        cursor = await self.bot.db.execute(
            """
            SELECT response_message_id, guild_id, member_id, channel_id,
                   attempt_count
            FROM disboard_bump_reminders
            WHERE status = 'scheduled' AND due_at <= ?
            ORDER BY due_at ASC
            LIMIT ?
            """,
            (now.isoformat(), BUMP_REMINDER_BATCH_SIZE),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        for response_id, guild_id, member_id, channel_id, attempts in rows:
            cursor = await self.bot.db.execute(
                """
                UPDATE disboard_bump_reminders
                SET status = 'processing', claimed_at = ?,
                    attempt_count = attempt_count + 1
                WHERE response_message_id = ? AND status = 'scheduled'
                """,
                (now.isoformat(), response_id),
            )
            claimed = cursor.rowcount > 0
            await cursor.close()
            await self.bot.db.commit()
            if not claimed:
                continue
            attempt_count = int(attempts) + 1
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                await self._retry_or_fail_reminder(
                    str(response_id), attempt_count, "Guild is unavailable.", retryable=True,
                )
                continue
            member = guild.get_member(int(member_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(member_id))
                except discord.NotFound:
                    await self._retry_or_fail_reminder(
                        str(response_id), attempt_count, "Member is no longer in the guild.",
                        retryable=False,
                    )
                    continue
                except (discord.Forbidden, discord.HTTPException) as exc:
                    await self._retry_or_fail_reminder(
                        str(response_id), attempt_count, type(exc).__name__, retryable=True,
                    )
                    continue
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except discord.NotFound:
                    await self._retry_or_fail_reminder(
                        str(response_id), attempt_count, "Source channel no longer exists.",
                        retryable=False,
                    )
                    continue
                except (discord.Forbidden, discord.HTTPException) as exc:
                    await self._retry_or_fail_reminder(
                        str(response_id), attempt_count, type(exc).__name__, retryable=True,
                    )
                    continue
            role_id = _configured_id("BUMP_PING_ROLE_ID")
            role = guild.get_role(role_id) if role_id else None
            if role is None:
                logger.warning(
                    "Bump reminder ping role unavailable guild_id=%s role_id=%s",
                    guild.id,
                    role_id or "not_configured",
                )
            payload = await self._configured_reminder_payload()
            content = self._reminder_content(member, role)
            embeds = []
            view = None
            if payload:
                try:
                    payload = render_feature_payload(
                        payload,
                        user_mention=member.mention,
                        role_mentions=[role.mention] if role is not None else [],
                        placeholders={
                            "member": member.mention,
                            "role": role.mention if role is not None else "",
                        },
                    )
                    content = payload["content"]
                    embeds = discord_embeds_from_payload(payload)
                    view = discord_view_from_payload(payload)
                except ValueError:
                    logger.warning("Configured bump reminder asset payload is invalid")
                    payload = None
            if not payload:
                content = self._reminder_content(member, role)
                embeds = [self._reminder_embed()]
            try:
                reminder_message = await channel.send(
                    content or None,
                    embeds=embeds,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(
                        users=[member],
                        roles=[role] if role is not None else False,
                        everyone=False,
                    ),
                )
            except discord.Forbidden as exc:
                await self._retry_or_fail_reminder(
                    str(response_id), attempt_count, type(exc).__name__, retryable=False,
                )
                continue
            except discord.HTTPException as exc:
                await self._retry_or_fail_reminder(
                    str(response_id), attempt_count, type(exc).__name__, retryable=True,
                )
                continue
            await self.bot.db.execute(
                """
                UPDATE disboard_bump_reminders
                SET status = 'sent', sent_at = ?, reminder_message_id = ?,
                    claimed_at = NULL, last_error = NULL
                WHERE response_message_id = ? AND status = 'processing'
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    str(reminder_message.id),
                    str(response_id),
                ),
            )
            await self.bot.db.commit()

    async def _publish_if_due(self, guild: discord.Guild) -> None:
        channel_id = _configured_id("BUMP_LEADERBOARD_CHANNEL_ID")
        if not channel_id:
            return
        cursor = await self.bot.db.execute(
            "SELECT last_posted_at FROM bump_leaderboard_post_state WHERE guild_id = ?",
            (str(guild.id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        now = datetime.now(timezone.utc)
        if row is None:
            # Establish the schedule without producing an unexpected deployment-day
            # post. The first automatic publication occurs the following Monday.
            await self.bot.db.execute(
                """
                INSERT INTO bump_leaderboard_post_state (
                    guild_id, channel_id, last_posted_at
                ) VALUES (?, ?, ?)
                """,
                (str(guild.id), str(channel_id), now.isoformat()),
            )
            await self.bot.db.commit()
            return
        local_now = now.astimezone(_publisher_timezone())
        week_start = datetime.combine(
            local_now.date() - timedelta(days=local_now.weekday()),
            time.min,
            tzinfo=local_now.tzinfo,
        )
        if row:
            try:
                last_posted = datetime.fromisoformat(str(row[0]))
            except ValueError:
                last_posted = datetime.min.replace(tzinfo=timezone.utc)
            if last_posted.astimezone(local_now.tzinfo) >= week_start:
                return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            return
        try:
            file, view = await self._graphic_page(guild)
            await channel.send(file=file, view=view)
        except (discord.Forbidden, discord.HTTPException):
            return
        await self.bot.db.execute(
            """
            INSERT INTO bump_leaderboard_post_state (guild_id, channel_id, last_posted_at)
            VALUES (?, ?, ?)
            ON CONFLICT (guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                last_posted_at = excluded.last_posted_at
            """,
            (str(guild.id), str(channel_id), now.isoformat()),
        )
        await self.bot.db.commit()

    @tasks.loop(minutes=5)
    async def weekly_publisher(self) -> None:
        for guild in self.bot.guilds:
            await self._publish_if_due(guild)

    @weekly_publisher.before_loop
    async def before_weekly_publisher(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def reminder_worker(self) -> None:
        await self._process_due_reminders()

    @reminder_worker.before_loop
    async def before_reminder_worker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DisboardBumps(bot))
