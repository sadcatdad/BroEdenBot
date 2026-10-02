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
from dashboard.rbac import permissions_for_discord_member

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
                content = drop["message_text"]
                ping_role_id = drop["ping_role_id"]
                if ping_role_id and ping_role_id != str(campaign["guild_id"]):
                    role = channel.guild.get_role(int(ping_role_id))
                    if role is not None:
                        content = f"<@&{ping_role_id}>" + (
                            f"\n{content}" if content else ""
                        )
                        kwargs["allowed_mentions"] = discord.AllowedMentions(
                            everyone=False,
                            users=False,
                            roles=[role],
                            replied_user=False,
                        )
                    else:
                        log.warning(
                            "Event Drop %s ping role %s is unavailable; sending without ping",
                            drop_id,
                            ping_role_id,
                        )
                if content:
                    kwargs["content"] = content
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
                log.info(
                    "Event Drop delivered campaign=%s drop=%s variant=%s selection=%s points=%s channel=%s",
                    campaign["id"],
                    drop_id,
                    drop["variant_id"],
                    drop["variant_selection"],
                    drop["points"],
                    channel.id,
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

    async def command_allowed(self, interaction, action):
        user = interaction.user
        if not interaction.guild_id or user.bot:
            return False
        if (
            is_configured_owner(user)
            or user.guild_permissions.administrator
            or any(r.id in configured_admin_role_ids() for r in user.roles)
        ):
            return True
        permissions = await self.call(
            permissions_for_discord_member, user.id, [r.id for r in user.roles]
        )
        return "event_drops." + action in permissions

    async def require_command(self, interaction, action):
        # Permission reads can wait behind schema initialization. Acknowledge
        # first so the interaction remains usable beyond Discord's three seconds.
        await interaction.response.defer(ephemeral=True)
        if await self.command_allowed(interaction, action):
            return True
        await interaction.followup.send(
            f"You need the Event Drops {action} permission. An administrator can grant it through The Garden’s Access role mappings.",
            ephemeral=True,
        )
        return False

    @app_commands.command(
        name="drop",
        description="List running drop campaigns, their time remaining, and next drops",
    )
    @app_commands.guild_only()
    async def drop_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        campaigns = await self.call(self.service.campaigns, interaction.guild_id)
        campaigns = [
            c for c in campaigns if c["status"] in ("active", "paused", "scheduled")
        ]
        if not campaigns:
            await interaction.followup.send(
                "No running Event Drops campaigns.", ephemeral=True
            )
            return
        # One compact embed per page keeps every campaign within Discord's limits.
        for start in range(0, len(campaigns), 10):
            embed = discord.Embed(title="Event Drops campaigns", color=0x57F287)
            for c in campaigns[start : start + 10]:
                end = (
                    f"Ends <t:{int(c['end_at'])}:F> (<t:{int(c['end_at'])}:R>)"
                    if c["end_at"]
                    else "No scheduled end"
                )
                next_at = c["next_drop_at"] if c["status"] != "paused" else None
                next_label = (
                    f"<t:{int(next_at)}:F> (<t:{int(next_at)}:R>)"
                    if next_at
                    else (
                        "Paused" if c["status"] == "paused" else "Waiting for scheduler"
                    )
                )
                embed.add_field(
                    name=f"{c['name']} · #{c['id']}"[:256],
                    value=f"{c['status'].title()} · {end}\nNext drop: {next_label}",
                    inline=False,
                )
            embed.set_footer(
                text="Times use your Discord timezone. Paused campaigns allow staff awards and manual drops."
            )
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @app_commands.command(
        name="drop-now",
        description="Send a weighted random drop in a campaign's allowed channels",
    )
    @app_commands.guild_only()
    @app_commands.describe(campaign="Select a running campaign")
    async def drop_now(self, interaction: discord.Interaction, campaign: int):
        if not await self.require_command(interaction, "send"):
            return
        try:
            drop_id = await self.call(
                self.service.queue_manual,
                campaign,
                interaction.guild_id,
                f"discord:{interaction.id}",
                actor_id=interaction.user.id,
                source="discord",
            )
            await self.send_drop(drop_id)
            row = (
                await self.call(
                    self.service.rows,
                    "SELECT status,error FROM event_drops WHERE id=?",
                    (drop_id,),
                )
            )[0]
            text = f"Drop #{drop_id}: {row['status']}." + (
                f" {row['error']}" if row["error"] else ""
            )
            await interaction.followup.send(
                text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @app_commands.command(
        name="drop-give",
        description="Award a member exact points OR one drop variant's reward",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        campaign="Select a running campaign",
        user="Member receiving the award",
        points="Exact points (choose points OR drop)",
        drop="Variant reward (choose drop OR points)",
        reason="Optional reason saved to campaign history",
    )
    async def drop_give(
        self,
        interaction: discord.Interaction,
        campaign: int,
        user: discord.Member,
        points: Optional[app_commands.Range[int, 1, 1000000]] = None,
        drop: Optional[int] = None,
        reason: Optional[app_commands.Range[str, 1, 500]] = None,
    ):
        await self.adjust_command(
            interaction, campaign, user, "give", points, drop, reason or ""
        )

    @app_commands.command(
        name="drop-remove",
        description="Remove an exact number of a member's campaign points",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        campaign="Select a running campaign",
        user="Member whose points will be removed",
        points="Positive number to remove; cannot exceed their balance",
        reason="Optional reason saved to campaign history",
    )
    async def drop_remove(
        self,
        interaction: discord.Interaction,
        campaign: int,
        user: discord.Member,
        points: app_commands.Range[int, 1, 1000000],
        reason: Optional[app_commands.Range[str, 1, 500]] = None,
    ):
        await self.adjust_command(
            interaction, campaign, user, "remove", points, None, reason or ""
        )

    async def adjust_command(
        self, interaction, campaign, user, action, points, variant_id, reason
    ):
        if not await self.require_command(interaction, action):
            return
        try:
            if user.bot or user.guild.id != interaction.guild_id:
                raise ValueError("Choose a human member of this server.")
            c = await self.call(self.service.campaign, campaign, interaction.guild_id)
            result = await self.call(
                self.service.adjust_points,
                campaign,
                interaction.guild_id,
                f"discord:{interaction.id}",
                interaction.user.id,
                user.id,
                action,
                points,
                variant_id,
                reason,
                user.display_name,
            )
        except ValueError as exc:
            await interaction.followup.send(
                str(exc),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if result["duplicate"]:
            await interaction.followup.send(
                f"Operation #{result['id']} was already recorded; no points changed again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Recorded {action} operation #{result['id']}. New total: {result['after_total']} {c['plural']}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if action == "give":
            variant = (
                f"{discord.utils.escape_markdown(result['variant_name'])} with "
                if result["variant_name"]
                else ""
            )
            text = f"<@{user.id}> received {variant}{result['points']} {discord.utils.escape_markdown(c['plural'])} in **{discord.utils.escape_markdown(c['name'])}**!"
            try:
                await interaction.followup.send(
                    text,
                    ephemeral=False,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        roles=False,
                        users=[discord.Object(id=user.id)],
                        replied_user=False,
                    ),
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    "The award is saved, but Discord could not post the recipient notification. The score was not rolled back.",
                    ephemeral=True,
                )
        await publish_audit(
            self.bot,
            interaction.guild,
            "Event Drops: " + action,
            f"Campaign #{campaign} · operation #{result['id']} · actor {interaction.user.id} · recipient {user.id} · {result['points']:+} points · total {result['after_total']}",
        )

    @drop_now.autocomplete("campaign")
    @drop_give.autocomplete("campaign")
    @drop_remove.autocomplete("campaign")
    async def active_campaign_choices(
        self, interaction: discord.Interaction, current: str
    ):
        action = {"drop-now": "send", "drop-give": "give", "drop-remove": "remove"}.get(
            interaction.command.name
        )
        if not action or not await self.command_allowed(interaction, action):
            return []
        campaigns = await self.call(self.service.campaigns, interaction.guild_id)
        now = time.time()
        return [
            app_commands.Choice(
                name=f"{c['name']} · {c['status']} · #{c['id']}"[:100], value=c["id"]
            )
            for c in campaigns
            if c["status"] in ("active", "paused")
            and (not c["start_at"] or c["start_at"] <= now)
            and (not c["end_at"] or c["end_at"] > now)
            and (current.casefold() in c["name"].casefold() or current == str(c["id"]))
        ][:25]

    @drop_give.autocomplete("drop")
    async def award_variant_choices(
        self, interaction: discord.Interaction, current: str
    ):
        if not await self.command_allowed(interaction, "give"):
            return []
        campaign_id = getattr(interaction.namespace, "campaign", None)
        if not campaign_id:
            return []
        try:
            c = await self.call(
                self.service.campaign, int(campaign_id), interaction.guild_id
            )
            if not c["variants_enabled"] or c["status"] not in ("active", "paused"):
                return []
            variants = await self.call(self.service.variants, c["id"])
        except (ValueError, TypeError):
            return []
        return [
            app_commands.Choice(
                name=f"{v['name']} · {v['reward_label']} · #{v['id']}"[:100],
                value=v["id"],
            )
            for v in variants
            if v["enabled"]
            and (current.casefold() in v["name"].casefold() or current == str(v["id"]))
        ][:25]

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
                    actor_id=user.id,
                    source="discord",
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
