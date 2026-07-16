"""Lightweight daily member activity streaks without message-content storage."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import COLOR
from utils.access import configured_staff_role_ids
from utils.ranked_graphic import (
    RankedGraphicItem,
    RankedGraphicSection,
    render_ranked_graphic,
)
from utils.settings import (
    get_bool_setting,
    get_csv_ids_setting,
    get_int_setting,
    get_setting,
)
from utils.embed_templates import (
    discord_embed_from_payload,
    discord_embeds_from_payload,
    discord_view_from_payload,
    get_embed_template,
    render_feature_payload,
)
from utils.streaks import STREAK_SCHEMA, compute_streaks, is_streak_milestone


logger = logging.getLogger(__name__)
STREAK_FOOTER = "!streak = see your streak | /streak leaderboard = see all"
STREAK_PAGE_SIZE = 10
STREAK_MILESTONE_MESSAGE_DEFAULT = (
    "🎉 Congratulations {member}! You reached a **{days}-day** activity streak!"
)
STREAK_BACKGROUND_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "streak_leaderboard_background.png"
)
AUTOMATIC_EXCLUDED_CHANNEL_TERMS = {
    "bot-command",
    "bot-commands",
    "bot center",
    "bot-center",
    "commands",
    "counting",
    "spam",
}
WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
STAFF_PERMISSION_NAMES = (
    "administrator",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_messages",
    "manage_threads",
    "view_audit_log",
    "kick_members",
    "ban_members",
    "moderate_members",
)


@lru_cache(maxsize=1)
def _streak_background_bytes() -> Optional[bytes]:
    try:
        return STREAK_BACKGROUND_PATH.read_bytes()
    except OSError:
        return None


class Streaks(commands.Cog):
    streak = app_commands.Group(
        name="streak",
        description="Daily community activity streaks",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._weekly_refresh_started = False
        self._heartbeat_initialized: set[int] = set()
        self._staff_role_ids: Optional[set[int]] = None

    async def cog_load(self) -> None:
        await self.bot.db.executescript(STREAK_SCHEMA)
        # A process interruption may leave one request claimed but unfinished.
        # Returning it to the durable queue makes restoration restart-safe.
        await self.bot.db.execute(
            """
            UPDATE streak_restore_requests
            SET status = 'pending', started_at = NULL,
                error = 'Previous restore worker stopped before completion.'
            WHERE status = 'processing'
            """
        )
        await self.bot.db.commit()
        if not self.weekly_refresh.is_running():
            self.weekly_refresh.start()
        if not self.heartbeat_worker.is_running():
            self.heartbeat_worker.start()
        if not self.restore_worker.is_running():
            self.restore_worker.start()

    def cog_unload(self) -> None:
        self.weekly_refresh.cancel()
        self.heartbeat_worker.cancel()
        self.restore_worker.cancel()

    @staticmethod
    def _timezone() -> ZoneInfo:
        name = str(get_setting("STREAK_TIMEZONE", "America/Chicago") or "")
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("America/Chicago")

    def _today(self) -> date:
        return datetime.now(self._timezone()).date()

    def _message_activity_date(self, message: discord.Message) -> date:
        created_at = getattr(message, "created_at", None)
        if not isinstance(created_at, datetime):
            return self._today()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(self._timezone()).date()

    @staticmethod
    def _normalized_content(content: str) -> str:
        return " ".join(str(content or "").casefold().split())

    def _configured_staff_roles(self) -> set[int]:
        if self._staff_role_ids is None:
            self._staff_role_ids = set(configured_staff_role_ids())
        return self._staff_role_ids

    def _channel_is_excluded(self, message: discord.Message) -> bool:
        channel = message.channel
        channel_id = getattr(channel, "id", 0)
        if channel_id in set(get_csv_ids_setting("STREAK_EXCLUDED_CHANNEL_IDS")):
            return True
        category_id = getattr(channel, "category_id", None)
        if category_id is None:
            category_id = getattr(getattr(channel, "parent", None), "category_id", None)
        if category_id in set(get_csv_ids_setting("STREAK_EXCLUDED_CATEGORY_IDS")):
            return True
        names = {
            str(getattr(channel, "name", "") or "").casefold(),
            str(getattr(getattr(channel, "parent", None), "name", "") or "").casefold(),
        }
        if any(
            term in name
            for name in names
            for term in AUTOMATIC_EXCLUDED_CHANNEL_TERMS
        ):
            return True
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for is None:
            return True
        default_role = message.guild.default_role
        permissions = permissions_for(default_role)
        if getattr(permissions, "view_channel", False):
            return False

        # Bro Eden's community channels are gated behind a verified-member
        # role, so @everyone cannot see them even though they are public to the
        # membership. Accept access granted by an ordinary role held by the
        # author, but do not let staff/admin roles make a private staff channel
        # qualify.
        staff_role_ids = self._configured_staff_roles()
        for role in getattr(message.author, "roles", ()):
            if role == default_role or getattr(role, "managed", False):
                continue
            if getattr(role, "id", None) in staff_role_ids:
                continue
            role_permissions = getattr(role, "permissions", None)
            if role_permissions and any(
                getattr(role_permissions, name, False)
                for name in STAFF_PERMISSION_NAMES
            ):
                continue
            permissions = permissions_for(role)
            if getattr(permissions, "view_channel", False):
                return False
        return True

    async def _is_duplicate(
        self,
        guild_id: int,
        user_id: int,
        message_hash: str,
        today: date,
    ) -> bool:
        lookback = max(1, get_int_setting("STREAK_DUPLICATE_LOOKBACK_DAYS", 30))
        cutoff = (today - timedelta(days=lookback)).isoformat()
        cursor = await self.bot.db.execute(
            """
            SELECT 1 FROM streak_days
            WHERE guild_id = ? AND user_id = ? AND message_hash = ?
              AND activity_date >= ?
            LIMIT 1
            """,
            (str(guild_id), str(user_id), message_hash, cutoff),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row is not None

    async def _is_manually_excluded(
        self,
        guild_id: int,
        user_id: int,
        activity_date: date,
    ) -> bool:
        cursor = await self.bot.db.execute(
            """
            SELECT action FROM streak_adjustments
            WHERE guild_id = ? AND user_id = ? AND activity_date = ?
            ORDER BY id DESC LIMIT 1
            """,
            (str(guild_id), str(user_id), activity_date.isoformat()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return bool(row and str(row[0]) == "remove")

    async def _recompute(self, guild_id: int, user_id: int) -> tuple[int, int]:
        cursor = await self.bot.db.execute(
            """
            SELECT activity_date FROM streak_days
            WHERE guild_id = ? AND user_id = ?
            ORDER BY activity_date
            """,
            (str(guild_id), str(user_id)),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        days = [date.fromisoformat(str(row[0])) for row in rows]
        current, calculated_longest = compute_streaks(days, self._today())
        longest = calculated_longest
        last_date = max(days).isoformat() if days else None
        await self.bot.db.execute(
            """
            INSERT INTO member_streaks (
                guild_id, user_id, current_streak, longest_streak,
                last_qualified_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                current_streak = excluded.current_streak,
                longest_streak = excluded.longest_streak,
                last_qualified_date = excluded.last_qualified_date,
                updated_at = excluded.updated_at
            """,
            (
                str(guild_id),
                str(user_id),
                current,
                longest,
                last_date,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.bot.db.commit()
        return current, longest

    async def _qualify_message(
        self,
        message: discord.Message,
        *,
        activity_date: Optional[date] = None,
    ) -> Optional[tuple[int, bool]]:
        if (
            message.guild is None
            or message.author.bot
            or message.webhook_id is not None
            or not isinstance(message.author, discord.Member)
            or self._channel_is_excluded(message)
        ):
            return None
        normalized = self._normalized_content(message.content)
        if normalized.startswith("!"):
            return None
        minimum_words = max(4, get_int_setting("STREAK_MIN_WORDS", 4))
        if len(WORD_RE.findall(normalized)) < minimum_words:
            return None
        message_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        today = activity_date or self._message_activity_date(message)
        if await self._is_manually_excluded(
            message.guild.id,
            message.author.id,
            today,
        ):
            return None
        if await self._is_duplicate(
            message.guild.id,
            message.author.id,
            message_hash,
            today,
        ):
            return None
        cursor = await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO streak_days (
                guild_id, user_id, activity_date, message_id,
                channel_id, message_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(message.guild.id),
                str(message.author.id),
                today.isoformat(),
                str(message.id),
                str(message.channel.id),
                message_hash,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        inserted = cursor.rowcount > 0
        await cursor.close()
        await self.bot.db.commit()
        if not inserted:
            return None
        current, _ = await self._recompute(message.guild.id, message.author.id)
        milestone_earned = False
        if is_streak_milestone(current):
            cursor = await self.bot.db.execute(
                """
                INSERT OR IGNORE INTO streak_milestones (
                    guild_id, user_id, milestone_days, source_message_id, earned_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(message.guild.id),
                    str(message.author.id),
                    current,
                    str(message.id),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            milestone_earned = cursor.rowcount > 0
            await cursor.close()
            await self.bot.db.commit()
        return current, milestone_earned

    @staticmethod
    def _streak_view(guild_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="My Streak",
                emoji="🔥",
                style=discord.ButtonStyle.primary,
                custom_id=f"streakpanel|me|{guild_id}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="View Leaderboard",
                emoji="🏆",
                style=discord.ButtonStyle.secondary,
                custom_id=f"streakpanel|leaderboard|{guild_id}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Streak Rules",
                emoji="📖",
                style=discord.ButtonStyle.secondary,
                custom_id=f"streakpanel|rules|{guild_id}",
            )
        )
        return view

    @staticmethod
    def _streak_command_view(guild_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label="Streak Leaderboard",
                emoji="🏆",
                style=discord.ButtonStyle.secondary,
                custom_id=f"streakpanel|leaderboard|{guild_id}",
            )
        )
        view.add_item(
            discord.ui.Button(
                label="Rules",
                emoji="📖",
                style=discord.ButtonStyle.secondary,
                custom_id=f"streakpanel|rules|{guild_id}",
            )
        )
        return view

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        result = await self._qualify_message(message)
        if result is None:
            return
        current, milestone_earned = result
        if milestone_earned:
            try:
                await message.add_reaction("🎉")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self._send_milestone_notification(
                message.guild,
                message.author,
                current,
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self,
        payload: discord.RawMessageDeleteEvent,
    ) -> None:
        if payload.guild_id is None:
            return
        await self._remove_deleted_messages(payload.guild_id, [payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ) -> None:
        if payload.guild_id is None:
            return
        await self._remove_deleted_messages(payload.guild_id, payload.message_ids)

    async def _remove_deleted_messages(
        self,
        guild_id: int,
        message_ids: Iterable[int],
    ) -> None:
        ids = [str(message_id) for message_id in message_ids]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        cursor = await self.bot.db.execute(
            f"""
            SELECT DISTINCT user_id FROM streak_days
            WHERE guild_id = ? AND message_id IN ({placeholders})
            """,
            (str(guild_id), *ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if not rows:
            return
        await self.bot.db.execute(
            f"""
            DELETE FROM streak_milestones
            WHERE guild_id = ? AND source_message_id IN ({placeholders})
            """,
            (str(guild_id), *ids),
        )
        await self.bot.db.execute(
            f"""
            DELETE FROM streak_days
            WHERE guild_id = ? AND message_id IN ({placeholders})
            """,
            (str(guild_id), *ids),
        )
        await self.bot.db.commit()
        for row in rows:
            await self._recompute(guild_id, int(row[0]))

    async def _member_embed(self, guild_id: int, member: discord.Member) -> discord.Embed:
        await self._recompute(guild_id, member.id)
        cursor = await self.bot.db.execute(
            """
            SELECT current_streak, longest_streak, last_qualified_date
            FROM member_streaks WHERE guild_id = ? AND user_id = ?
            """,
            (str(guild_id), str(member.id)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        current, longest, last_date = row if row else (0, 0, None)
        completed = str(last_date or "") == self._today().isoformat()
        embed = discord.Embed(
            title="🔥 ACTIVITY STREAK",
            description=(
                f"**{discord.utils.escape_markdown(member.display_name)}** · "
                f"@{discord.utils.escape_markdown(member.name)}\n\n"
                f"Current streak: **{int(current)} days**\n"
                f"Longest streak: **{int(longest)} days**\n"
                f"Today: **{'Completed ✅' if completed else 'Not completed'}**"
            ),
            color=COLOR,
        )
        embed.set_footer(text=STREAK_FOOTER)
        return embed

    async def _unread_milestone(self, guild_id: int, user_id: int) -> Optional[int]:
        cursor = await self.bot.db.execute(
            """
            SELECT milestone_days FROM streak_milestones
            WHERE guild_id = ? AND user_id = ? AND seen_at IS NULL
            ORDER BY milestone_days DESC
            LIMIT 1
            """,
            (str(guild_id), str(user_id)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row else None

    async def _mark_milestones_seen(
        self,
        guild_id: int,
        user_id: int,
        through_days: int,
    ) -> None:
        await self.bot.db.execute(
            """
            UPDATE streak_milestones SET seen_at = ?
            WHERE guild_id = ? AND user_id = ? AND seen_at IS NULL
              AND milestone_days <= ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(guild_id),
                str(user_id),
                through_days,
            ),
        )
        await self.bot.db.commit()

    @staticmethod
    def _milestone_message(member: discord.Member, days: int) -> str:
        return (
            STREAK_MILESTONE_MESSAGE_DEFAULT.replace("{member}", member.mention)
            .replace("{days}", str(days))
            .strip()
        )

    async def _milestone_asset_payload(
        self,
        member: discord.Member,
        days: int,
    ) -> Optional[dict]:
        template_id = str(
            get_setting("STREAK_MILESTONE_ASSET_ID", "") or ""
        ).strip()
        if not template_id.isdigit():
            return None
        try:
            template = await asyncio.to_thread(get_embed_template, int(template_id))
        except (OSError, sqlite3.Error):
            logger.exception("Could not load configured streak milestone asset id=%s", template_id)
            return None
        if template is None:
            logger.warning("Configured streak milestone asset was not found id=%s", template_id)
            return None
        try:
            return render_feature_payload(
                template["payload"],
                user_mention=member.mention,
                role_mentions=[],
                placeholders={"member": member.mention, "days": str(days)},
            )
        except ValueError:
            logger.warning("Configured streak milestone asset payload is invalid id=%s", template_id)
            return None

    async def _send_milestone_notification(
        self,
        guild: discord.Guild,
        member: discord.Member,
        milestone_days: int,
    ) -> None:
        raw_channel_id = str(
            get_setting("STREAK_MILESTONE_CHANNEL_ID", "") or ""
        ).strip()
        if not raw_channel_id.isdigit():
            return
        channel_id = int(raw_channel_id)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning(
                    "Could not access streak milestone channel guild_id=%s channel_id=%s",
                    guild.id,
                    channel_id,
                )
                return
        if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            logger.warning(
                "Ignoring streak milestone channel outside guild_id=%s channel_id=%s",
                guild.id,
                channel_id,
            )
            return
        send = getattr(channel, "send", None)
        if send is None:
            logger.warning(
                "Configured streak milestone channel is not messageable guild_id=%s channel_id=%s",
                guild.id,
                channel_id,
            )
            return
        payload = await self._milestone_asset_payload(member, milestone_days)
        content = self._milestone_message(member, milestone_days)
        embeds = []
        view = None
        if payload:
            content = payload["content"]
            embeds = discord_embeds_from_payload(payload)
            view = discord_view_from_payload(payload)
        try:
            await send(
                content or None,
                embeds=embeds,
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=[member],
                    roles=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Could not publish streak milestone guild_id=%s member_id=%s channel_id=%s",
                guild.id,
                member.id,
                channel_id,
            )

    async def _milestone_embed(
        self,
        guild_id: int,
        member: discord.Member,
        milestone_days: int,
    ) -> discord.Embed:
        current, longest = await self._recompute(guild_id, member.id)
        payload = await self._milestone_asset_payload(member, milestone_days)
        embed = discord_embed_from_payload(payload) if payload else None
        if embed is None:
            description = payload["content"] if payload else self._milestone_message(member, milestone_days)
            embed = discord.Embed(
                title="🔥 STREAK MILESTONE!",
                description=description,
                color=COLOR,
            )
        if len(embed.fields) <= 23:
            embed.add_field(name="Current streak", value=f"**{current} days**")
            embed.add_field(name="Longest streak", value=f"**{longest} days**")
        if not embed.footer.text:
            embed.set_footer(text=STREAK_FOOTER)
        return embed

    @commands.command(name="streak", description="Show your activity streak")
    async def streak_prefix(self, ctx: commands.Context) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return
        member_embed = await self._member_embed(ctx.guild.id, ctx.author)
        milestone = await self._unread_milestone(ctx.guild.id, ctx.author.id)
        if milestone is None:
            await ctx.send(
                embed=member_embed,
                view=self._streak_command_view(ctx.guild.id),
            )
            return
        await ctx.send(
            embeds=[
                await self._milestone_embed(ctx.guild.id, ctx.author, milestone),
                member_embed,
            ],
            view=self._streak_command_view(ctx.guild.id),
        )
        await self._mark_milestones_seen(ctx.guild.id, ctx.author.id, milestone)

    @staticmethod
    def _rules_embed() -> discord.Embed:
        embed = discord.Embed(
            title="🔥 STREAK RULES",
            description=(
                "• Post at least once every day.\n"
                "• Your message must contain more than 3 words.\n"
                "• Staff-only channels and Bot Center do not count.\n"
                "• Bot commands, spam, counting, duplicate messages, bots, and "
                "webhooks do not count."
            ),
            color=COLOR,
        )
        embed.set_footer(text=STREAK_FOOTER)
        return embed

    async def _leaderboard_page(
        self,
        guild: discord.Guild,
        mode: str,
        *,
        page: int = 0,
        weekly: bool = False,
    ) -> tuple[discord.File, discord.ui.View]:
        yesterday = (self._today() - timedelta(days=1)).isoformat()
        await self.bot.db.execute(
            """
            UPDATE member_streaks SET current_streak = 0, updated_at = ?
            WHERE guild_id = ?
              AND (last_qualified_date IS NULL OR last_qualified_date < ?)
            """,
            (datetime.now(timezone.utc).isoformat(), str(guild.id), yesterday),
        )
        await self.bot.db.commit()
        column = "longest_streak" if mode == "longest" else "current_streak"
        cursor = await self.bot.db.execute(
            f"""
            SELECT COUNT(*) FROM member_streaks
            WHERE guild_id = ? AND {column} > 0
            """,
            (str(guild.id),),
        )
        total_entries = int((await cursor.fetchone())[0])
        await cursor.close()
        total_pages = max(1, math.ceil(total_entries / STREAK_PAGE_SIZE))
        page = min(max(0, page), total_pages - 1)
        cursor = await self.bot.db.execute(
            f"""
            SELECT user_id, {column} FROM member_streaks
            WHERE guild_id = ? AND {column} > 0
            ORDER BY {column} DESC, user_id ASC
            LIMIT ? OFFSET ?
            """,
            (str(guild.id), STREAK_PAGE_SIZE, page * STREAK_PAGE_SIZE),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        items: list[RankedGraphicItem] = []
        for user_id, streak in rows:
            member = guild.get_member(int(user_id))
            if member is None:
                continue
            avatar = getattr(member, "display_avatar", None)
            avatar_url = None
            if avatar is not None:
                avatar_url = str(avatar.replace(size=64).url)
            days = int(streak)
            items.append(
                RankedGraphicItem(
                    label=member.display_name,
                    subtitle=f"@{member.name}",
                    value=f"{days:,} day{'s' if days != 1 else ''}",
                    avatar_url=avatar_url,
                    score=float(days),
                )
            )
        title = (
            "🔥 Weekly Streak Leaderboard"
            if weekly
            else (
                "🔥 Longest Streak Leaderboard"
                if mode == "longest"
                else "🔥 Streak Leaderboard"
            )
        )
        png = await render_ranked_graphic(
            title=title,
            subtitle="Chat every day to keep your streak!",
            sections=[
                RankedGraphicSection(
                    "",
                    items,
                    rank_start=page * STREAK_PAGE_SIZE + 1,
                )
            ],
            updated_at=datetime.now(timezone.utc),
            accent_color=COLOR,
            total_entries=total_entries,
            background_bytes=await asyncio.to_thread(_streak_background_bytes),
            layout="leaderboard",
            footer_text=STREAK_FOOTER,
            force_columns=2,
            template_key="streak_leaderboard",
        )
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Previous",
            emoji="◀️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"streakboard|{mode}|prev|{page}",
            disabled=page == 0,
        ))
        view.add_item(discord.ui.Button(
            label=f"Page {page + 1} of {total_pages}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"streakboard|{mode}|page|{page}",
            disabled=True,
        ))
        view.add_item(discord.ui.Button(
            label="Next",
            emoji="▶️",
            style=discord.ButtonStyle.primary,
            custom_id=f"streakboard|{mode}|next|{page}",
            disabled=page + 1 >= total_pages,
        ))
        return discord.File(io.BytesIO(png), filename="streak-leaderboard.png"), view

    @streak.command(name="leaderboard", description="Show top activity streaks")
    @app_commands.choices(
        streak_type=[
            app_commands.Choice(name="Current streak", value="current"),
            app_commands.Choice(name="Longest streak", value="longest"),
        ]
    )
    @app_commands.guild_only()
    async def streak_leaderboard(
        self,
        interaction: discord.Interaction,
        streak_type: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        mode = streak_type.value if streak_type else "current"
        await interaction.response.defer(thinking=True)
        file, view = await self._leaderboard_page(interaction.guild, mode)
        await interaction.followup.send(file=file, view=view)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id", ""))
        parts = custom_id.split("|")
        if (
            len(parts) == 4
            and parts[0] == "streakboard"
            and parts[1] in {"current", "longest"}
            and parts[2] in {"prev", "next"}
            and parts[3].isdigit()
        ):
            if interaction.guild is None:
                return
            current_page = int(parts[3])
            target_page = current_page - 1 if parts[2] == "prev" else current_page + 1
            await interaction.response.defer()
            file, view = await self._leaderboard_page(
                interaction.guild,
                parts[1],
                page=target_page,
            )
            await interaction.edit_original_response(attachments=[file], view=view)
            return
        if (
            len(parts) != 3
            or parts[0] != "streakpanel"
            or parts[1] not in {"me", "leaderboard", "rules"}
            or not parts[2].isdigit()
        ):
            return
        guild = self.bot.get_guild(int(parts[2]))
        if guild is None:
            await interaction.response.send_message(
                "Your streak profile is not available in that server.",
                ephemeral=True,
            )
            return
        if parts[1] == "me":
            member = guild.get_member(interaction.user.id)
            if member is None and isinstance(interaction.user, discord.Member):
                member = interaction.user
            if member is None:
                await interaction.response.send_message(
                    "Your streak profile is not available in that server.",
                    ephemeral=True,
                )
                return
            member_embed = await self._member_embed(guild.id, member)
            milestone = await self._unread_milestone(guild.id, member.id)
            if milestone is not None:
                await interaction.response.send_message(
                    embeds=[
                        await self._milestone_embed(guild.id, member, milestone),
                        member_embed,
                    ],
                    ephemeral=True,
                )
                await self._mark_milestones_seen(guild.id, member.id, milestone)
                return
            embed = member_embed
        elif parts[1] == "leaderboard":
            await interaction.response.defer(ephemeral=True, thinking=True)
            file, view = await self._leaderboard_page(guild, "current")
            await interaction.followup.send(
                file=file,
                view=view,
                ephemeral=True,
            )
            return
        else:
            embed = self._rules_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _refresh_weekly_post(self, guild: discord.Guild) -> None:
        raw_channel = str(get_setting("STREAK_LEADERBOARD_CHANNEL_ID", "") or "")
        if not raw_channel.isdigit():
            return
        channel_id = int(raw_channel)
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            return
        now = datetime.now(self._timezone())
        week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02}"
        cursor = await self.bot.db.execute(
            """
            SELECT channel_id, message_id, last_week_key
            FROM streak_weekly_posts WHERE guild_id = ?
            """,
            (str(guild.id),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        file, _page_view = await self._leaderboard_page(
            guild,
            "current",
            weekly=True,
        )
        message = None
        edit_attempted = False
        if row and int(row[0]) == channel_id:
            edit_attempted = True
            try:
                message = await channel.fetch_message(int(row[1]))
                await message.edit(
                    embed=None,
                    attachments=[file],
                    view=self._streak_view(guild.id),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            if edit_attempted:
                file, _page_view = await self._leaderboard_page(
                    guild,
                    "current",
                    weekly=True,
                )
            try:
                message = await channel.send(
                    file=file,
                    view=self._streak_view(guild.id),
                )
            except (discord.Forbidden, discord.HTTPException):
                return
        await self.bot.db.execute(
            """
            INSERT INTO streak_weekly_posts (
                guild_id, channel_id, message_id, last_week_key, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                last_week_key = excluded.last_week_key,
                updated_at = excluded.updated_at
            """,
            (
                str(guild.id),
                str(channel_id),
                str(message.id),
                week_key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.bot.db.commit()

    async def _queue_automatic_restore(
        self,
        guild_id: int,
        previous_heartbeat: datetime,
        now: datetime,
    ) -> Optional[int]:
        if not get_bool_setting("STREAK_RESTORE_ENABLED", True):
            return None
        gap_minutes = max(
            2,
            get_int_setting("STREAK_RESTORE_GAP_MINUTES", 10),
        )
        if now - previous_heartbeat < timedelta(minutes=gap_minutes):
            return None
        max_days = max(1, get_int_setting("STREAK_RESTORE_MAX_DAYS", 14))
        start = max(previous_heartbeat, now - timedelta(days=max_days))
        cursor = await self.bot.db.execute(
            """
            SELECT id FROM streak_restore_requests
            WHERE guild_id = ? AND status IN ('pending', 'processing')
              AND start_at_utc <= ? AND end_at_utc >= ?
            ORDER BY id DESC LIMIT 1
            """,
            (str(guild_id), now.isoformat(), start.isoformat()),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        if existing:
            return int(existing[0])
        cursor = await self.bot.db.execute(
            """
            INSERT INTO streak_restore_requests (
                guild_id, start_at_utc, end_at_utc, requested_by,
                request_source, status, created_at
            ) VALUES (?, ?, ?, 'automatic-heartbeat', 'automatic', 'pending', ?)
            """,
            (
                str(guild_id),
                start.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        request_id = int(cursor.lastrowid)
        await cursor.close()
        logger.info(
            "Queued automatic streak restore request_id=%s guild_id=%s gap_minutes=%.1f",
            request_id,
            guild_id,
            (now - previous_heartbeat).total_seconds() / 60,
        )
        return request_id

    async def _record_heartbeats(self) -> None:
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            cursor = await self.bot.db.execute(
                """
                SELECT last_heartbeat_at FROM streak_runtime_state
                WHERE guild_id = ?
                """,
                (str(guild.id),),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                try:
                    previous = datetime.fromisoformat(str(row[0]))
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=timezone.utc)
                    await self._queue_automatic_restore(guild.id, previous, now)
                except ValueError:
                    logger.warning(
                        "Invalid streak heartbeat timestamp guild_id=%s value=%r",
                        guild.id,
                        row[0],
                    )
            started_at = (
                now.isoformat()
                if guild.id not in self._heartbeat_initialized
                else None
            )
            await self.bot.db.execute(
                """
                INSERT INTO streak_runtime_state (
                    guild_id, last_heartbeat_at, last_started_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    last_started_at = COALESCE(
                        excluded.last_started_at,
                        streak_runtime_state.last_started_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    str(guild.id),
                    now.isoformat(),
                    started_at,
                    now.isoformat(),
                ),
            )
            self._heartbeat_initialized.add(guild.id)
        await self.bot.db.commit()

    async def _restore_history(
        self,
        guild: discord.Guild,
        start: datetime,
        end: datetime,
    ) -> dict[str, object]:
        channels = list(getattr(guild, "text_channels", ()))
        channels.extend(
            thread
            for thread in getattr(guild, "threads", ())
            if not getattr(thread, "archived", False)
        )
        unique_channels = {
            int(channel.id): channel
            for channel in channels
            if hasattr(channel, "history") and getattr(channel, "id", None)
        }
        max_messages = max(
            100,
            get_int_setting("STREAK_RESTORE_MAX_MESSAGES", 50000),
        )
        scanned = 0
        restored = 0
        failed_channels = 0
        accessible_channels = 0
        restored_members: set[int] = set()
        truncated = False
        for channel in unique_channels.values():
            if scanned >= max_messages:
                truncated = True
                break
            try:
                history = channel.history(
                    after=start,
                    before=end,
                    oldest_first=True,
                    limit=None,
                )
                accessible_channels += 1
                async for message in history:
                    scanned += 1
                    result = await self._qualify_message(
                        message,
                        activity_date=self._message_activity_date(message),
                    )
                    if result is not None:
                        restored += 1
                        restored_members.add(int(message.author.id))
                    if scanned >= max_messages:
                        truncated = True
                        break
            except (discord.Forbidden, discord.HTTPException):
                failed_channels += 1
                logger.warning(
                    "Could not scan streak restore channel guild_id=%s channel_id=%s",
                    guild.id,
                    channel.id,
                )
        return {
            "messages_scanned": scanned,
            "days_restored": restored,
            "members_restored": len(restored_members),
            "channels_failed": failed_channels,
            "accessible_channels": accessible_channels,
            "truncated": truncated,
        }

    async def _process_restore_request(self) -> bool:
        cursor = await self.bot.db.execute(
            """
            SELECT id, guild_id, start_at_utc, end_at_utc
            FROM streak_restore_requests
            WHERE status = 'pending'
            ORDER BY id LIMIT 1
            """
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return False
        request_id, guild_id, start_text, end_text = row
        now = datetime.now(timezone.utc)
        await self.bot.db.execute(
            """
            UPDATE streak_restore_requests
            SET status = 'processing', started_at = ?, error = NULL
            WHERE id = ? AND status = 'pending'
            """,
            (now.isoformat(), request_id),
        )
        await self.bot.db.commit()
        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            await self._finish_restore_request(
                int(request_id),
                str(guild_id),
                status="failed",
                error="Configured guild is unavailable to the bot.",
            )
            return True
        try:
            start = datetime.fromisoformat(str(start_text))
            end = datetime.fromisoformat(str(end_text))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            result = await self._restore_history(guild, start, end)
        except Exception as exc:
            logger.exception(
                "Streak restore failed request_id=%s guild_id=%s",
                request_id,
                guild_id,
            )
            await self._finish_restore_request(
                int(request_id),
                str(guild_id),
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            return True
        if (
            int(result["accessible_channels"]) == 0
            and int(result["channels_failed"]) > 0
        ):
            status = "failed"
            error = "No configured public channel history could be read."
        else:
            status = "completed"
            error = (
                "Message safety limit reached; queue a smaller date range."
                if result["truncated"]
                else None
            )
        await self._finish_restore_request(
            int(request_id),
            str(guild_id),
            status=status,
            error=error,
            result=result,
        )
        return True

    async def _finish_restore_request(
        self,
        request_id: int,
        guild_id: str,
        *,
        status: str,
        error: Optional[str],
        result: Optional[dict[str, object]] = None,
    ) -> None:
        result = result or {}
        now = datetime.now(timezone.utc).isoformat()
        scanned = int(result.get("messages_scanned", 0))
        restored = int(result.get("days_restored", 0))
        members = int(result.get("members_restored", 0))
        failed_channels = int(result.get("channels_failed", 0))
        detail = (
            f"Scanned {scanned:,} messages; restored {restored:,} days "
            f"for {members:,} members; {failed_channels:,} channels failed."
        )
        await self.bot.db.execute(
            """
            UPDATE streak_restore_requests
            SET status = ?, messages_scanned = ?, days_restored = ?,
                members_restored = ?, channels_failed = ?, error = ?,
                completed_at = ?
            WHERE id = ?
            """,
            (
                status,
                scanned,
                restored,
                members,
                failed_channels,
                error,
                now,
                request_id,
            ),
        )
        await self.bot.db.execute(
            """
            INSERT INTO streak_runtime_state (
                guild_id, last_heartbeat_at, last_restore_at,
                last_restore_status, last_restore_detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id) DO UPDATE SET
                last_restore_at = excluded.last_restore_at,
                last_restore_status = excluded.last_restore_status,
                last_restore_detail = excluded.last_restore_detail,
                updated_at = excluded.updated_at
            """,
            (guild_id, now, now, status, detail if not error else error, now),
        )
        await self.bot.db.commit()

    @tasks.loop(minutes=1)
    async def heartbeat_worker(self) -> None:
        await self._record_heartbeats()

    @heartbeat_worker.before_loop
    async def before_heartbeat_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=20)
    async def restore_worker(self) -> None:
        await self._process_restore_request()

    @restore_worker.before_loop
    async def before_restore_worker(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=30)
    async def weekly_refresh(self) -> None:
        for guild in self.bot.guilds:
            await self._refresh_weekly_post(guild)

    @weekly_refresh.before_loop
    async def before_weekly_refresh(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Streaks(bot))
