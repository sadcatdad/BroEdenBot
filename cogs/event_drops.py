"""Discord adapter for the shared, database-backed Event Drops service."""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
from typing import Optional
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.access import configured_admin_role_ids, is_configured_owner
from utils.audit_log import publish_audit
from utils.display_names import normalize_display_name
from utils.event_drops import EventDrops

log = logging.getLogger(__name__)


def channel_order(channels, recent, avoid):
    """Prefer unused channels; relax oldest exclusions first in a finite pass."""
    pool = list(channels)
    random.shuffle(pool)
    blocked = list(dict.fromkeys(recent[:avoid]))
    return sorted(
        pool,
        key=lambda c: (
            str(c.id) in blocked,
            -blocked.index(str(c.id)) if str(c.id) in blocked else 0,
        ),
    )


class ClaimButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"eventdrop:claim:(?P<drop_id>[0-9]+)",
):
    def __init__(self, drop_id, campaign=None):
        self.drop_id = int(drop_id)
        c = campaign or {}
        super().__init__(
            discord.ui.Button(
                label=c.get("button_label", "Collect"),
                emoji=(
                    discord.PartialEmoji.from_str(c["button_emoji"])
                    if c.get("button_emoji")
                    else None
                ),
                style=getattr(discord.ButtonStyle, c.get("button_style", "primary")),
                custom_id=f"eventdrop:claim:{drop_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["drop_id"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog("EventDropsCog")
        if not cog:
            await interaction.followup.send(
                "Event Drops is temporarily unavailable.", ephemeral=True
            )
            return
        try:
            result = await cog.call(
                cog.service.claim,
                self.drop_id,
                interaction.user.id,
                interaction.guild_id,
                bot=interaction.user.bot,
                role_ids=[r.id for r in getattr(interaction.user, "roles", ())],
                display_name=normalize_display_name(interaction.user.display_name),
            )
            await interaction.followup.send(
                result["message"],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Event Drop claim failed drop_id=%s", self.drop_id)
            await interaction.followup.send(
                "Could not confirm this claim. Try again; retries cannot award twice.",
                ephemeral=True,
            )


def drop_view(drop_id, campaign):
    view = discord.ui.View(timeout=None)
    view.add_item(ClaimButton(drop_id, campaign))
    return view


def drop_embed(campaign, drop_id, expires_at, has_image=False):
    description = campaign["description"]
    if campaign.get("show_reward"):
        noun = campaign["singular"] if campaign["points"] == 1 else campaign["plural"]
        description += f"\n\nWorth: {campaign['points']} {noun}"
    if campaign.get("show_rarity") and campaign.get("rarity"):
        description += f"\nRarity: {campaign['rarity']}"
    embed = discord.Embed(
        title=campaign["title"],
        color=int(campaign["color"][1:], 16),
        description=description
        + f'\n\n{campaign["emoji"]} Expires <t:{int(expires_at)}:R>.',
    )
    embed.set_footer(text=f"Event Drop #{drop_id}")
    if campaign["thumbnail"]:
        embed.set_thumbnail(url=campaign["thumbnail"])
    if has_image:
        embed.set_image(url="attachment://event-drop.webp")
    return embed


class EventDropsCog(commands.Cog):
    event = app_commands.Group(
        name="event", description="Event Drop scores and leaderboards", guild_only=True
    )
    eventdrop = app_commands.Group(
        name="eventdrop", description="Manage Event Drops", guild_only=True
    )

    def __init__(self, bot):
        self.bot = bot
        self.service = EventDrops()
        self._recovered = False
        self._names_refreshed = 0.0

    @staticmethod
    async def call(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def cog_load(self):
        await self.call(self.service.initialize)
        self.bot.add_dynamic_items(ClaimButton)
        self.worker.start()

    def cog_unload(self):
        self.worker.cancel()
        self.bot.remove_dynamic_items(ClaimButton)

    async def eligible_channels(self, campaign, specific=None, image=False):
        guild = self.bot.get_guild(int(campaign["guild_id"]))
        if guild is None or guild.me is None:
            return []
        result = []
        for channel_id in campaign["channels"]:
            if specific and str(specific) != channel_id:
                continue
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                p = channel.permissions_for(guild.me)
                # Bots can delete their own messages without Manage Messages.
                # Read history is needed to reconcile an ambiguous send.
                if (
                    p.view_channel
                    and p.send_messages
                    and p.embed_links
                    and p.read_message_history
                    and (not image or p.attach_files)
                ):
                    result.append(channel)
                    continue
            log.warning(
                "Event Drops unavailable allowlisted channel campaign=%s channel=%s",
                campaign["id"],
                channel_id,
            )
        recent = await self.call(
            self.service.rows,
            "SELECT channel_id FROM event_drops WHERE campaign_id=? AND posted_at IS NOT NULL ORDER BY posted_at DESC LIMIT ?",
            (campaign["id"], campaign["avoid_recent"]),
        )
        return channel_order(
            result, [r["channel_id"] for r in recent], campaign["avoid_recent"]
        )

    async def failure(self, drop_id, campaign_id, error, *, ambiguous=False):
        log.warning("Event Drop delivery failed drop=%s: %s", drop_id, error)
        await self.call(
            self.service.execute,
            "UPDATE event_drops SET status=?,error=?,cleanup_after=? WHERE id=? AND status='sending'",
            (
                "sending" if ambiguous else "failed",
                str(error)[:500],
                time.time() + 120,
                drop_id,
            ),
        )
        await self.call(
            self.service.execute,
            "UPDATE event_drop_campaigns SET warning=? WHERE id=?",
            (str(error)[:500], campaign_id),
        )

    async def send_drop(self, drop_id):
        taken = await self.call(self.service.take_pending, drop_id)
        if not taken:
            return
        drop, campaign = taken
        attempted = False
        try:
            campaign.update(await self.call(self.service.drop_appearance, drop_id))
            channels = await self.eligible_channels(
                campaign, drop["channel_id"], image=bool(drop["asset_id"])
            )
            if not channels:
                raise ValueError(
                    "No eligible allowlisted channels. Check channel permissions and the selected channel pool."
                )
            assets = (
                await self.call(
                    self.service.rows,
                    "SELECT image_bytes FROM event_drop_assets WHERE id=?",
                    (drop["asset_id"],),
                )
                if drop["asset_id"]
                else []
            )
            for channel in channels:
                expires = await self.call(
                    self.service.prepare_send, drop_id, channel.id
                )
                kwargs = dict(
                    embed=drop_embed(campaign, drop_id, expires, bool(assets)),
                    view=drop_view(drop_id, campaign),
                    allowed_mentions=discord.AllowedMentions.none(),
                    nonce=f"eventdrop:{drop_id}",
                )
                if assets:
                    kwargs["file"] = discord.File(
                        io.BytesIO(assets[0]["image_bytes"]), filename="event-drop.webp"
                    )
                try:
                    attempted = True
                    message = await channel.send(**kwargs)
                except (discord.Forbidden, discord.NotFound):
                    # Definitive rejection, safe to try another explicitly selected channel.
                    attempted = False
                    log.warning(
                        "Event Drop channel rejected send channel=%s", channel.id
                    )
                    continue
                await self.call(
                    self.service.sent,
                    drop_id,
                    message.id,
                    message.created_at.timestamp(),
                )
                if drop["kind"] == "manual":
                    await publish_audit(
                        self.bot,
                        channel.guild,
                        "Event Drops: manual drop",
                        f'Campaign {campaign["name"]} · drop #{drop_id} · channel {channel.id}',
                    )
                return
            raise ValueError(
                "All selected channels rejected the drop. Check permissions."
            )
        except Exception as exc:
            # A client-side HTTP rejection is definitive, including invalid
            # artwork/emoji. Only timeouts, network errors and server failures
            # need receipt reconciliation rather than a failed history row.
            definitive = (
                isinstance(exc, discord.HTTPException)
                and 400 <= exc.status < 500
                and exc.status != 429
            )
            await self.failure(
                drop_id,
                campaign["id"],
                str(exc) or type(exc).__name__,
                ambiguous=attempted and not definitive,
            )

    async def reconcile(self, drop):
        """Recover Discord's accepted message after a crash between send and receipt.

        Never resend this occurrence. A bounded scan finds the unique bot marker;
        unresolved receipts remain visible and are retried slowly for cleanup.
        """
        now = time.time()
        owned = await self.call(
            self.service.execute,
            "UPDATE event_drops SET cleanup_after=? WHERE id=? AND status='sending' AND cleanup_after<=?",
            (now + 300, drop["id"], now),
        )
        if not owned:
            return
        if not drop["channel_id"] or not drop["expires_at"]:
            await self.failure(
                drop["id"],
                drop["campaign_id"],
                "Interrupted before sending; occurrence skipped.",
            )
            return
        channel = self.bot.get_channel(int(drop["channel_id"]))
        if channel is None:
            await self.failure(
                drop["id"],
                drop["campaign_id"],
                "Cannot reconcile missing channel; receipt remains uncertain.",
                ambiguous=True,
            )
            return
        try:
            after = (
                discord.Object(id=int(drop["reconcile_after_id"]))
                if drop.get("reconcile_after_id")
                else datetime.fromtimestamp(drop["created_at"] - 5, timezone.utc)
            )
            last_id = drop.get("reconcile_after_id")
            async for message in channel.history(
                limit=500, after=after, oldest_first=True
            ):
                last_id = str(message.id)
                if message.author.id == self.bot.user.id and any(
                    e.footer.text == f'Event Drop #{drop["id"]}' for e in message.embeds
                ):
                    await self.call(
                        self.service.sent,
                        drop["id"],
                        message.id,
                        message.created_at.timestamp(),
                    )
                    return
            await self.call(
                self.service.execute,
                "UPDATE event_drops SET reconcile_after_id=?,error=? WHERE id=?",
                (
                    last_id,
                    "Send receipt uncertain; history reconciliation continues in 5 minutes. Delivery will not be retried.",
                    drop["id"],
                ),
            )
        except discord.HTTPException as exc:
            log.warning(
                "Event Drop receipt reconciliation failed id=%s: %s", drop["id"], exc
            )

    async def cleanup(self, drop):
        now = time.time()
        owned = await self.call(
            self.service.execute,
            "UPDATE event_drops SET cleanup_after=? WHERE id=? AND status='expired' AND cleanup_after<=?",
            (now + 300, drop["id"], now),
        )
        if not owned:
            return
        try:
            channel = self.bot.get_channel(int(drop["channel_id"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(drop["channel_id"]))
            await channel.get_partial_message(int(drop["message_id"])).delete()
        except discord.NotFound:
            pass
        except Exception as exc:
            log.warning("Event Drop cleanup failed id=%s: %s", drop["id"], exc)
            await self.call(
                self.service.execute,
                "UPDATE event_drops SET error=? WHERE id=?",
                (
                    f"Message cleanup failed; retry in 5 minutes: {str(exc)[:350]}",
                    drop["id"],
                ),
            )
            return
        await self.call(
            self.service.execute,
            "UPDATE event_drops SET status='deleted',error=NULL WHERE id=?",
            (drop["id"],),
        )

    async def refresh_member_names(self):
        rows = await self.call(
            self.service.rows, "SELECT guild_id,user_id FROM event_drop_members"
        )
        updates = []
        for row in rows:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if guild is None or not guild.chunked:
                continue
            member = guild.get_member(int(row["user_id"]))
            name = normalize_display_name(member.display_name) if member else ""
            updates.append((name, time.time(), row["guild_id"], row["user_id"]))

        def save_names():
            with self.service.connect(True) as db:
                db.executemany(
                    "UPDATE event_drop_members SET display_name=?,updated_at=? WHERE guild_id=? AND user_id=?",
                    updates,
                )

        if updates:
            await self.call(save_names)

    @tasks.loop(seconds=15)
    async def worker(self):
        try:
            await self.call(self.service.tick, recover=not self._recovered)
            self._recovered = True
            now = time.time()
            if now - self._names_refreshed > 300:
                await self.refresh_member_names()
                self._names_refreshed = now
            for d in await self.call(
                self.service.rows,
                "SELECT * FROM event_drops WHERE status='expired' AND message_id IS NOT NULL AND cleanup_after<=? ORDER BY id LIMIT 20",
                (now,),
            ):
                try:
                    await self.cleanup(d)
                except Exception:
                    log.exception("Event Drop cleanup worker error id=%s", d["id"])
            for d in await self.call(
                self.service.rows,
                "SELECT * FROM event_drops WHERE status='sending' AND created_at<? AND cleanup_after<=? ORDER BY id LIMIT 5",
                (now - 120, now),
            ):
                try:
                    await self.reconcile(d)
                except Exception:
                    log.exception("Event Drop reconciliation error id=%s", d["id"])
            for d in await self.call(
                self.service.rows,
                "SELECT id FROM event_drops WHERE status='pending' ORDER BY id LIMIT 10",
            ):
                try:
                    await self.send_drop(d["id"])
                except Exception:
                    log.exception("Event Drop send worker error id=%s", d["id"])
        except Exception:
            log.exception("Event Drops worker tick failed; retrying next tick")

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()

    async def selected_campaign(self, guild_id, campaign_id=None):
        if campaign_id is not None:
            return await self.call(self.service.campaign, campaign_id, guild_id)
        campaigns = await self.call(self.service.campaigns, guild_id)
        if not campaigns:
            raise ValueError("No Event Drops campaigns are available.")
        return next(
            (c for c in campaigns if c["status"] in ("active", "paused")), campaigns[0]
        )

    @event.command(name="score", description="Privately see your Event Drops score")
    async def score(
        self, interaction: discord.Interaction, campaign_id: Optional[int] = None
    ):
        await self.show_scores(interaction, campaign_id, personal=True)

    @event.command(
        name="leaderboard", description="Show the top 10 Event Drops participants"
    )
    async def leaderboard(
        self, interaction: discord.Interaction, campaign_id: Optional[int] = None
    ):
        await self.show_scores(interaction, campaign_id, personal=False)

    async def show_scores(self, interaction, campaign_id, personal):
        await interaction.response.defer(ephemeral=personal)
        try:
            c = await self.selected_campaign(interaction.guild_id, campaign_id)
            scores = await self.call(self.service.leaderboard, c["id"])
            if personal:
                row = next(
                    (r for r in scores if r["user_id"] == str(interaction.user.id)),
                    None,
                )
                body = (
                    f'{row["total_points"] if row else 0} {c["plural"]}\nRank: #{row["rank"]}'
                    if row
                    else f'0 {c["plural"]}\nCollect a drop to join the leaderboard.'
                )
            else:
                lines = []
                for row in scores[:10]:
                    member = interaction.guild.get_member(int(row["user_id"]))
                    name = normalize_display_name(
                        member.display_name if member else row["display_name"],
                        f'Unknown/Former Member ({row["user_id"]})',
                    )
                    lines.append(
                        f'{row["rank"]}. {discord.utils.escape_markdown(name)} — {row["total_points"]} {c["plural"]}'
                    )
                body = (
                    "\n".join(lines)
                    or "No claims yet. The first drop is waiting for its collectors."
                )
            await interaction.followup.send(
                embed=discord.Embed(
                    title=f'{c["emoji"]} {c["name"]}'[:256],
                    description=body,
                    color=int(c["color"][1:], 16),
                ),
                ephemeral=personal,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    async def admin_action(self, interaction, action, campaign_id):
        user = interaction.user
        admin = (
            is_configured_owner(user)
            or user.guild_permissions.administrator
            or any(r.id in configured_admin_role_ids() for r in user.roles)
        )
        if not admin:
            await interaction.response.send_message(
                "A configured bot administrator is required.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            c = await self.selected_campaign(interaction.guild_id, campaign_id)
            if action == "status":
                next_label = (
                    f'<t:{int(c["next_drop_at"])}:R>' if c["next_drop_at"] else "none"
                )
                text = f'{c["name"]}: {c["status"]}. Next drop: {next_label}. {c["warning"] or ""}'
            elif action == "drop":
                drop_id = await self.call(
                    self.service.queue_manual,
                    c["id"],
                    interaction.guild_id,
                    f"discord:{interaction.id}",
                )
                text = f"Manual drop #{drop_id} queued."
            else:
                await self.call(
                    self.service.transition, c["id"], interaction.guild_id, action
                )
                text = f'{c["name"]}: {action} applied.'
            if action != "status":
                await publish_audit(
                    self.bot,
                    interaction.guild,
                    "Event Drops: " + action,
                    f"{text} Requested by {user.id}.",
                )
            await interaction.followup.send(
                text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @eventdrop.command(name="status", description="Inspect campaign scheduling")
    async def admin_status(
        self, interaction: discord.Interaction, campaign_id: Optional[int] = None
    ):
        await self.admin_action(interaction, "status", campaign_id)

    @eventdrop.command(name="drop", description="Queue one manual Event Drop")
    async def admin_drop(
        self, interaction: discord.Interaction, campaign_id: Optional[int] = None
    ):
        await self.admin_action(interaction, "drop", campaign_id)

    @eventdrop.command(name="pause", description="Pause automatic Event Drops")
    async def admin_pause(
        self, interaction: discord.Interaction, campaign_id: Optional[int] = None
    ):
        await self.admin_action(interaction, "pause", campaign_id)

    @eventdrop.command(name="resume", description="Resume automatic Event Drops")
    async def admin_resume(
        self, interaction: discord.Interaction, campaign_id: Optional[int] = None
    ):
        await self.admin_action(interaction, "resume", campaign_id)


async def setup(bot):
    await bot.add_cog(EventDropsCog(bot))
