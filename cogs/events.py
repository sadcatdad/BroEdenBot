"""Discord Scheduled Event synchronization and dashboard action worker."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord.ext import commands, tasks

from config import COLOR
from utils.events import EVENTS_SCHEMA_SQL, initialize_events_schema_async, utc_text
from utils.reminder_service import DEFAULT_EVENT_OFFSETS, ReminderService, parse_utc
from utils.settings import get_setting


logger = logging.getLogger(__name__)


def _event_type_name(entity_type: Any) -> str:
    name = str(getattr(entity_type, "name", entity_type) or "external").casefold()
    return "stage" if name in {"stage", "stage_instance"} else name


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class EventsSync(commands.Cog):
    """Keep the dashboard, Discord events, and canonical reminders aligned."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = ReminderService(bot.db)
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        await self.service.initialize()
        await initialize_events_schema_async(self.bot.db)
        await self.bot.db.execute(
            """
            UPDATE event_dashboard_actions
            SET status = 'pending', processed_at_utc = NULL,
                failure_reason = 'Recovered after the bot restarted during this action.'
            WHERE status = 'processing' AND attempt_count < 3
            """
        )
        await self.bot.db.execute(
            """
            UPDATE event_dashboard_actions
            SET status = 'failed', processed_at_utc = ?,
                failure_reason = 'The bot restarted after this action exhausted its retry limit.',
                payload_json = '{}', image_bytes = NULL, image_content_type = NULL
            WHERE status = 'processing' AND attempt_count >= 3
            """,
            (utc_text(),),
        )
        await self.bot.db.commit()

    def cog_unload(self) -> None:
        self.reconciliation.cancel()
        self.action_worker.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.reconciliation.is_running():
            self.reconciliation.start()
        if not self.action_worker.is_running():
            self.action_worker.start()
        for guild in self.bot.guilds:
            await self.refresh_guild(guild)

    async def _upsert_reminder(
        self,
        guild: discord.Guild,
        payload: dict[str, Any],
    ) -> Optional[int]:
        event_id = str(payload["scheduled_event_id"])
        existing = await self.service.fetch_one(
            "SELECT * FROM reminder_items WHERE legacy_source = 'discord_scheduled_event' AND legacy_id = ?",
            (event_id,),
        )
        start = _parse_datetime(payload["scheduled_at_utc"])
        current = payload["status"] in {"scheduled", "active"} and start > datetime.now(timezone.utc)
        actor_id = int(payload.get("discord_creator_id") or getattr(self.bot.user, "id", 0) or 0)
        if not current:
            if existing is not None and existing["status"] == "upcoming":
                await self.service.cancel_reminder(
                    int(existing["id"]), actor_id, reason="Discord Scheduled Event ended or was cancelled", staff=True
                )
            return int(existing["id"]) if existing is not None else None
        if existing is None:
            created = await self.service.create_reminder(
                reminder_type="event",
                guild_id=guild.id,
                creator_user_id=actor_id,
                host_user_id=actor_id,
                title=payload["name"],
                description=payload.get("description", ""),
                scheduled_at_utc=start,
                interpretation_timezone=str(get_setting("SERVER_TIMEZONE", "America/Chicago") or "America/Chicago"),
                destination_channel_id=int(payload["channel_id"]) if str(payload.get("channel_id") or "").isdigit() else None,
                destination_channel_name=payload.get("location", ""),
                default_offsets=DEFAULT_EVENT_OFFSETS,
                allow_custom_timing=True,
                close_subscriptions_at_start=True,
                keep_public_card=False,
                auto_subscribe_creator=False,
            )
            reminder_id = int(created["id"])
            await self.bot.db.execute(
                """
                UPDATE reminder_items
                SET legacy_source = 'discord_scheduled_event', legacy_id = ?,
                    keep_public_card = 0, public_channel_id = NULL, public_message_id = NULL
                WHERE id = ?
                """,
                (event_id, reminder_id),
            )
            await self.bot.db.commit()
            return reminder_id
        reminder_id = int(existing["id"])
        if existing["status"] != "upcoming":
            return reminder_id
        await self.service.update_reminder(
            reminder_id,
            actor_id,
            staff=True,
            title=payload["name"],
            description=payload.get("description", ""),
            scheduled_at_utc=start,
            destination_channel_id=int(payload["channel_id"]) if str(payload.get("channel_id") or "").isdigit() else None,
            destination_channel_name=payload.get("location", ""),
            clear_destination=not bool(payload.get("channel_id")),
            default_offsets=DEFAULT_EVENT_OFFSETS,
        )
        return reminder_id

    @staticmethod
    def _snapshot_payload(event: discord.ScheduledEvent, raw: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        creator = getattr(event, "creator", None)
        channel = getattr(event, "channel", None)
        cover = getattr(event, "cover_image", None)
        recurrence = (raw or {}).get("recurrence_rule") or getattr(event, "recurrence_rule", None)
        status = str(getattr(getattr(event, "status", None), "name", "scheduled")).casefold()
        return {
            "scheduled_event_id": str(event.id),
            "name": str(event.name),
            "description": str(getattr(event, "description", "") or ""),
            "entity_type": _event_type_name(getattr(event, "entity_type", None)),
            "channel_id": str(channel.id) if channel is not None else None,
            "location": str(getattr(channel, "name", "") or getattr(event, "location", "") or "Discord Event"),
            "scheduled_at_utc": event.start_time.astimezone(timezone.utc).isoformat(),
            "end_at_utc": event.end_time.astimezone(timezone.utc).isoformat() if event.end_time else None,
            "event_url": str(event.url),
            "image_url": str(cover.url) if cover is not None else None,
            "discord_creator_id": str(getattr(event, "creator_id", "") or getattr(creator, "id", "") or "") or None,
            "discord_creator_name": str(getattr(creator, "display_name", "") or getattr(creator, "name", "") or "") or None,
            "interested_count": int(getattr(event, "user_count", 0) or 0),
            "recurrence_json": json.dumps(recurrence, default=str, separators=(",", ":")) if recurrence is not None else None,
            "status": status,
        }

    async def refresh_guild(self, guild: discord.Guild) -> None:
        lock = self._guild_locks.setdefault(guild.id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            attempted = utc_text()
            await self.bot.db.execute(
                """
                INSERT INTO dashboard_event_sync_status (guild_id, last_attempt_at_utc)
                VALUES (?, ?) ON CONFLICT (guild_id) DO UPDATE SET last_attempt_at_utc = excluded.last_attempt_at_utc
                """,
                (str(guild.id), attempted),
            )
            await self.bot.db.commit()
            try:
                # discord.py 2.7 omits Discord's recurrence_rule field from
                # ScheduledEvent, so retain it from the raw response.
                raw_events = await guild._state.http.get_scheduled_events(guild.id, True)
                events = [discord.ScheduledEvent(state=guild._state, data=item) for item in raw_events]
                raw_by_id = {str(item.get("id")): item for item in raw_events}
                existing = await self.service.fetch_all(
                    "SELECT scheduled_event_id, reminder_id FROM dashboard_scheduled_events WHERE guild_id = ? AND status IN ('scheduled', 'active')",
                    (str(guild.id),),
                )
                existing_ids = {str(row["scheduled_event_id"]): row for row in existing}
                seen: set[str] = set()
                now = utc_text()
                for event in events:
                    payload = self._snapshot_payload(event, raw_by_id.get(str(event.id)))
                    event_id = payload["scheduled_event_id"]
                    seen.add(event_id)
                    reminder_id = await self._upsert_reminder(guild, payload)
                    await self.bot.db.execute(
                        """
                        INSERT INTO dashboard_scheduled_events (
                            scheduled_event_id, guild_id, name, description, entity_type,
                            channel_id, location, scheduled_at_utc, end_at_utc, event_url,
                            image_url, discord_creator_id, discord_creator_name,
                            interested_count, recurrence_json, status, reminder_id,
                            last_sync_status, last_sync_error, updated_at_utc
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (scheduled_event_id) DO UPDATE SET
                            guild_id = excluded.guild_id, name = excluded.name,
                            description = excluded.description, entity_type = excluded.entity_type,
                            channel_id = excluded.channel_id, location = excluded.location,
                            scheduled_at_utc = excluded.scheduled_at_utc, end_at_utc = excluded.end_at_utc,
                            event_url = excluded.event_url, image_url = excluded.image_url,
                            discord_creator_id = excluded.discord_creator_id,
                            discord_creator_name = excluded.discord_creator_name,
                            interested_count = excluded.interested_count,
                            recurrence_json = excluded.recurrence_json, status = excluded.status,
                            reminder_id = excluded.reminder_id,
                            last_sync_status = excluded.last_sync_status,
                            last_sync_error = excluded.last_sync_error,
                            updated_at_utc = excluded.updated_at_utc
                        """,
                        (
                            event_id, str(guild.id), payload["name"], payload["description"],
                            payload["entity_type"], payload["channel_id"], payload["location"],
                            payload["scheduled_at_utc"], payload["end_at_utc"], payload["event_url"],
                            payload["image_url"], payload["discord_creator_id"],
                            payload["discord_creator_name"], payload["interested_count"],
                            payload["recurrence_json"], payload["status"], reminder_id,
                            "synchronized", None, now,
                        ),
                    )
                    if payload["discord_creator_id"] and str(payload["discord_creator_id"]) != str(getattr(self.bot.user, "id", "")):
                        await self.bot.db.execute(
                            """
                            INSERT OR IGNORE INTO dashboard_event_ownership (
                                scheduled_event_id, discord_user_id, organizer_name,
                                created_at_utc, updated_at_utc
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (event_id, payload["discord_creator_id"], payload["discord_creator_name"] or "", now, now),
                        )
                missing = set(existing_ids) - seen
                for event_id in missing:
                    reminder_id = existing_ids[event_id].get("reminder_id")
                    if reminder_id:
                        await self.service.cancel_reminder(
                            int(reminder_id), int(getattr(self.bot.user, "id", 0) or 0),
                            reason="Discord Scheduled Event was removed", staff=True,
                        )
                    await self.bot.db.execute(
                        "UPDATE dashboard_scheduled_events SET status = 'cancelled', last_sync_status = 'removed', last_sync_error = NULL, updated_at_utc = ? WHERE scheduled_event_id = ?",
                        (now, event_id),
                    )
                await self._refresh_artwork_links(guild)
                storage_ready = self._storage_channel_ready(guild)
                await self.bot.db.execute(
                    """
                    INSERT INTO dashboard_event_sync_status (
                        guild_id, last_attempt_at_utc, last_success_at_utc, event_count,
                        can_create_events, can_manage_events, eligible_channel_count,
                        storage_channel_ready, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT (guild_id) DO UPDATE SET
                        last_attempt_at_utc = excluded.last_attempt_at_utc,
                        last_success_at_utc = excluded.last_success_at_utc,
                        event_count = excluded.event_count,
                        can_create_events = excluded.can_create_events,
                        can_manage_events = excluded.can_manage_events,
                        eligible_channel_count = excluded.eligible_channel_count,
                        storage_channel_ready = excluded.storage_channel_ready,
                        last_error = NULL
                    """,
                    (
                        str(guild.id), attempted, now, len(events),
                        int(bool(getattr(getattr(guild.me, "guild_permissions", None), "create_events", False))),
                        int(bool(getattr(getattr(guild.me, "guild_permissions", None), "manage_events", False))),
                        sum(isinstance(channel, (discord.StageChannel, discord.VoiceChannel)) for channel in guild.channels),
                        int(storage_ready),
                    ),
                )
                await self.bot.db.commit()
            except (discord.Forbidden, discord.HTTPException, OSError, ValueError):
                logger.exception("Could not synchronize Discord Scheduled Events guild_id=%s", guild.id)
                await self.bot.db.execute(
                    "UPDATE dashboard_scheduled_events SET last_sync_status = 'failed', last_sync_error = ? WHERE guild_id = ?",
                    ("Discord event synchronization failed.", str(guild.id)),
                )
                await self.bot.db.execute(
                    "UPDATE dashboard_event_sync_status SET last_error = ? WHERE guild_id = ?",
                    ("Discord event synchronization failed.", str(guild.id)),
                )
                await self.bot.db.commit()

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        if event.guild is not None:
            await self.refresh_guild(event.guild)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, _before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
        if after.guild is not None:
            await self.refresh_guild(after.guild)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        if event.guild is not None:
            await self.refresh_guild(event.guild)

    @tasks.loop(minutes=15)
    async def reconciliation(self) -> None:
        for guild in self.bot.guilds:
            await self.refresh_guild(guild)

    @reconciliation.before_loop
    async def before_reconciliation(self) -> None:
        await self.bot.wait_until_ready()

    async def _event_channel(self, guild: discord.Guild, payload: dict[str, Any]) -> Optional[discord.abc.GuildChannel]:
        entity_type = str(payload.get("entity_type", ""))
        if entity_type == "external":
            return None
        channel_id = str(payload.get("channel_id") or "")
        if not channel_id.isdigit():
            raise ValueError("Choose a Discord Stage or voice channel.")
        channel = guild.get_channel(int(channel_id))
        if entity_type == "stage" and not isinstance(channel, discord.StageChannel):
            raise ValueError("The selected channel is not a Stage channel.")
        if entity_type == "voice" and not isinstance(channel, discord.VoiceChannel):
            raise ValueError("The selected channel is not a voice channel.")
        return channel

    @staticmethod
    def _storage_setting_id() -> int:
        value = str(get_setting("EVENTS_ARTWORK_STORAGE_CHANNEL_ID", "") or "").strip()
        return int(value) if value.isdigit() else 0

    def _storage_channel_ready(self, guild: discord.Guild) -> bool:
        channel_id = self._storage_setting_id()
        destination = self.bot.get_channel(channel_id) if channel_id else None
        if not isinstance(destination, (discord.ForumChannel, discord.TextChannel, discord.Thread)):
            return False
        permissions = destination.permissions_for(guild.me)
        can_send = bool(
            getattr(permissions, "send_messages", False)
            or getattr(permissions, "send_messages_in_threads", False)
        )
        return bool(
            getattr(permissions, "view_channel", False)
            and getattr(permissions, "attach_files", False)
            and can_send
        )

    async def _refresh_artwork_links(self, guild: discord.Guild) -> None:
        rows = await self.service.fetch_all(
            "SELECT * FROM dashboard_event_artwork WHERE guild_id = ?",
            (str(guild.id),),
        )
        for row in rows:
            try:
                destination = await self._resolve_storage_destination(
                    int(row["storage_thread_id"] or row["storage_channel_id"])
                )
                if destination is None or not hasattr(destination, "fetch_message"):
                    continue
                message = await destination.fetch_message(int(row["storage_message_id"]))
                if not message.attachments:
                    continue
                current_url = str(message.attachments[0].url)
                if current_url != str(row["attachment_url"]):
                    await self.bot.db.execute(
                        "UPDATE dashboard_event_artwork SET attachment_url = ?, updated_at_utc = ? WHERE scheduled_event_id = ?",
                        (current_url, utc_text(), str(row["scheduled_event_id"])),
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
                logger.warning(
                    "Could not refresh stored event artwork link event_id=%s",
                    row.get("scheduled_event_id"),
                )
        await self.bot.db.commit()

    async def _resolve_storage_destination(self, channel_id: int) -> Any:
        destination = self.bot.get_channel(channel_id)
        if destination is not None:
            return destination
        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _store_event_artwork(
        self,
        action: dict[str, Any],
        guild: discord.Guild,
        payload: dict[str, Any],
    ) -> dict[str, str | None]:
        if action.get("storage_attachment_url"):
            return {
                "channel_id": str(action.get("storage_channel_id") or ""),
                "thread_id": str(action.get("storage_thread_id") or "") or None,
                "message_id": str(action.get("storage_message_id") or ""),
                "attachment_url": str(action["storage_attachment_url"]),
            }
        channel_id = self._storage_setting_id()
        if not channel_id:
            raise ValueError("Event Artwork Storage is not configured in the Events dashboard settings.")
        destination = await self._resolve_storage_destination(channel_id)
        if not isinstance(destination, (discord.ForumChannel, discord.TextChannel, discord.Thread)):
            raise ValueError("The configured Event Artwork Storage destination is unavailable or unsupported.")
        filename = f"bro-eden-event-{action.get('scheduled_event_id') or action['id']}.webp"
        event_name = str(payload.get("name") or "Untitled event")[:100]
        content = f"Bro Eden Event Artwork · {event_name} · Action {action['id']}"
        allowed = discord.AllowedMentions.none()
        thread_id: Optional[int] = None
        message = None
        if action.get("scheduled_event_id"):
            cursor = await self.bot.db.execute(
                "SELECT * FROM dashboard_event_artwork WHERE scheduled_event_id = ?",
                (str(action["scheduled_event_id"]),),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing is not None and str(existing["storage_channel_id"]) == str(channel_id):
                stored_destination = await self._resolve_storage_destination(
                    int(existing["storage_thread_id"] or existing["storage_channel_id"])
                )
                if isinstance(stored_destination, discord.Thread) and stored_destination.archived:
                    try:
                        await stored_destination.edit(archived=False, reason="Bro Eden event artwork storage")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                if stored_destination is not None and hasattr(stored_destination, "fetch_message"):
                    try:
                        prior = await stored_destination.fetch_message(int(existing["storage_message_id"]))
                        message = await prior.edit(
                            content=content,
                            attachments=[discord.File(io.BytesIO(bytes(action["image_bytes"])), filename=filename)],
                            allowed_mentions=allowed,
                        )
                        thread_id = int(existing["storage_thread_id"]) if existing["storage_thread_id"] else None
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        message = None
        if message is not None:
            pass
        elif isinstance(destination, discord.Thread):
            if destination.archived:
                try:
                    await destination.edit(archived=False, reason="Bro Eden event artwork storage")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            message = await destination.send(
                content=content,
                file=discord.File(io.BytesIO(bytes(action["image_bytes"])), filename=filename),
                allowed_mentions=allowed,
            )
            thread_id = destination.id
        elif isinstance(destination, discord.ForumChannel):
            tags = []
            if getattr(destination.flags, "require_tag", False) and destination.available_tags:
                tags = [destination.available_tags[0]]
            created = await destination.create_thread(
                name=f"Event Artwork — {event_name}"[:100],
                content=content,
                file=discord.File(io.BytesIO(bytes(action["image_bytes"])), filename=filename),
                applied_tags=tags,
                allowed_mentions=allowed,
                reason="Bro Eden event artwork storage",
            )
            thread_id = created.thread.id
            message = created.message
        else:
            message = await destination.send(
                content=content,
                file=discord.File(io.BytesIO(bytes(action["image_bytes"])), filename=filename),
                allowed_mentions=allowed,
            )
        if not message.attachments:
            raise RuntimeError("Discord did not preserve the uploaded event artwork.")
        receipt = {
            "channel_id": str(channel_id),
            "thread_id": str(thread_id) if thread_id else None,
            "message_id": str(message.id),
            "attachment_url": str(message.attachments[0].url),
        }
        await self.bot.db.execute(
            """
            UPDATE event_dashboard_actions
            SET storage_channel_id = ?, storage_thread_id = ?,
                storage_message_id = ?, storage_attachment_url = ?
            WHERE id = ?
            """,
            (
                receipt["channel_id"], receipt["thread_id"], receipt["message_id"],
                receipt["attachment_url"], action["id"],
            ),
        )
        await self.bot.db.commit()
        return receipt

    async def _save_event_artwork_reference(
        self,
        *,
        event_id: int | str,
        guild_id: int | str,
        receipt: dict[str, str | None],
        content_type: str,
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO dashboard_event_artwork (
                scheduled_event_id, guild_id, storage_channel_id,
                storage_thread_id, storage_message_id, attachment_url,
                content_type, updated_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (scheduled_event_id) DO UPDATE SET
                guild_id = excluded.guild_id,
                storage_channel_id = excluded.storage_channel_id,
                storage_thread_id = excluded.storage_thread_id,
                storage_message_id = excluded.storage_message_id,
                attachment_url = excluded.attachment_url,
                content_type = excluded.content_type,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                str(event_id), str(guild_id), receipt["channel_id"], receipt["thread_id"],
                receipt["message_id"], receipt["attachment_url"], content_type, utc_text(),
            ),
        )
        await self.bot.db.commit()

    @staticmethod
    def _event_description(payload: dict[str, Any], organizer: str) -> str:
        description = str(payload.get("description") or "").strip()
        organizer_line = f"Organized by {organizer}" if organizer else ""
        if organizer_line and organizer_line.casefold() not in description.casefold():
            description = f"{description}\n\n{organizer_line}".strip()
        return description[:1000]

    async def _process_confirmation(self, action: dict[str, Any], payload: dict[str, Any]) -> str:
        subscription_id = int(payload.get("subscription_id") or 0)
        row = await self.service.fetch_one(
            """
            SELECT s.user_id, r.title, r.scheduled_at_utc, r.destination_channel_name
            FROM reminder_subscriptions s JOIN reminder_items r ON r.id = s.reminder_id
            WHERE s.id = ? AND s.status = 'active'
            """,
            (subscription_id,),
        )
        if row is None:
            raise ValueError("The event subscription no longer exists.")
        user = self.bot.get_user(int(row["user_id"]))
        if user is None:
            user = await self.bot.fetch_user(int(row["user_id"]))
        start = parse_utc(row["scheduled_at_utc"])
        embed = discord.Embed(
            title="Event reminders enabled",
            description=f"You are subscribed to **{row['title']}**.",
            color=COLOR,
        )
        embed.add_field(name="When", value=discord.utils.format_dt(start, "F"), inline=False)
        if row.get("destination_channel_name"):
            embed.add_field(name="Where", value=row["destination_channel_name"], inline=False)
        embed.set_footer(text="Use the Bro Eden Events page or /remind subscriptions to change timing.")
        await user.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        return "Subscription confirmation DM sent."

    async def _process_event_action(self, action: dict[str, Any]) -> tuple[Optional[str], str]:
        payload = json.loads(action.get("payload_json") or "{}")
        if action["action"] == "confirm_subscription":
            return action.get("scheduled_event_id"), await self._process_confirmation(action, payload)
        guild = self.bot.get_guild(int(action["guild_id"]))
        if guild is None:
            raise ValueError("The configured Discord server is unavailable.")
        reason = f"Bro Eden dashboard request by {action.get('requested_by_name') or 'authorized organizer'}"[:512]
        if action["action"] == "cancel":
            event = await guild.fetch_scheduled_event(int(action["scheduled_event_id"]), with_counts=True)
            await event.cancel(reason=reason)
            return str(event.id), "Discord event cancelled."
        entity_type_name = str(payload.get("entity_type") or "")
        if entity_type_name not in {"stage", "voice", "external"}:
            raise ValueError("Choose Stage, Voice, or External event type.")
        channel = await self._event_channel(guild, payload)
        start = _parse_datetime(payload["scheduled_at_utc"])
        end = _parse_datetime(payload["end_at_utc"]) if payload.get("end_at_utc") else None
        if start <= datetime.now(timezone.utc):
            raise ValueError("Event start time must be in the future.")
        if entity_type_name == "external" and (not str(payload.get("location") or "").strip() or end is None):
            raise ValueError("External events require a location and end time.")
        if end is not None and end <= start:
            raise ValueError("Event end time must be after its start time.")
        entity_type = {
            "stage": discord.EntityType.stage_instance,
            "voice": discord.EntityType.voice,
            "external": discord.EntityType.external,
        }[entity_type_name]
        kwargs = {
            "name": str(payload.get("name") or "").strip()[:100],
            "description": self._event_description(payload, action.get("requested_by_name") or ""),
            "start_time": start,
            "entity_type": entity_type,
            "reason": reason,
        }
        artwork_receipt = None
        if action.get("image_bytes"):
            artwork_receipt = await self._store_event_artwork(action, guild, payload)
        if end is not None or action["action"] == "edit":
            kwargs["end_time"] = end
        if entity_type_name == "external":
            kwargs["location"] = str(payload.get("location") or "").strip()[:100]
            if action["action"] == "edit":
                kwargs["channel"] = None
        else:
            kwargs["channel"] = channel
        if action.get("image_bytes"):
            kwargs["image"] = bytes(action["image_bytes"])
        if action["action"] == "create":
            kwargs["privacy_level"] = discord.PrivacyLevel.guild_only
            event = await guild.create_scheduled_event(**kwargs)
            # Ownership references the mirrored snapshot, so make that row
            # durable before attaching the human dashboard organizer.
            await self.refresh_guild(guild)
            now = utc_text()
            await self.bot.db.execute(
                """
                INSERT INTO dashboard_event_ownership (
                    scheduled_event_id, dashboard_user_id, discord_user_id,
                    organizer_name, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (scheduled_event_id) DO UPDATE SET
                    dashboard_user_id = excluded.dashboard_user_id,
                    discord_user_id = excluded.discord_user_id,
                    organizer_name = excluded.organizer_name,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    str(event.id), action.get("requested_by_dashboard_user_id"),
                    action.get("requested_by_discord_user_id"),
                    action.get("requested_by_name") or "", now, now,
                ),
            )
            await self.bot.db.commit()
            if artwork_receipt is not None:
                await self._save_event_artwork_reference(
                    event_id=event.id,
                    guild_id=guild.id,
                    receipt=artwork_receipt,
                    content_type=str(action.get("image_content_type") or "image/webp"),
                )
            return str(event.id), "Discord event published."
        event = await guild.fetch_scheduled_event(int(action["scheduled_event_id"]), with_counts=True)
        await event.edit(**kwargs)
        if artwork_receipt is not None:
            await self._save_event_artwork_reference(
                event_id=event.id,
                guild_id=guild.id,
                receipt=artwork_receipt,
                content_type=str(action.get("image_content_type") or "image/webp"),
            )
        return str(event.id), "Discord event updated."

    @tasks.loop(seconds=5)
    async def action_worker(self) -> None:
        rows = await self.service.fetch_all(
            "SELECT * FROM event_dashboard_actions WHERE status = 'pending' ORDER BY id LIMIT 10"
        )
        for action in rows:
            cursor = await self.bot.db.execute(
                "UPDATE event_dashboard_actions SET status = 'processing', attempt_count = attempt_count + 1 WHERE id = ? AND status = 'pending'",
                (action["id"],),
            )
            claimed = bool(cursor.rowcount)
            await cursor.close()
            await self.bot.db.commit()
            if not claimed:
                continue
            try:
                event_id, message = await self._process_event_action(action)
            except (discord.HTTPException, OSError) as exc:
                logger.exception("Temporary event dashboard action failure action_id=%s", action["id"])
                if int(action.get("attempt_count") or 0) + 1 < 3:
                    await self.bot.db.execute(
                        "UPDATE event_dashboard_actions SET status = 'pending', failure_reason = ? WHERE id = ?",
                        (f"Temporary Discord error; retrying: {str(exc)[:400] or type(exc).__name__}", action["id"]),
                    )
                else:
                    await self.bot.db.execute(
                        """
                        UPDATE event_dashboard_actions
                        SET status = 'failed', processed_at_utc = ?, failure_reason = ?,
                            payload_json = '{}', image_bytes = NULL, image_content_type = NULL
                        WHERE id = ?
                        """,
                        (utc_text(), str(exc)[:500] or type(exc).__name__, action["id"]),
                    )
            except Exception as exc:
                logger.exception("Event dashboard action failed action_id=%s", action["id"])
                await self.bot.db.execute(
                    """
                    UPDATE event_dashboard_actions
                    SET status = 'failed', processed_at_utc = ?, failure_reason = ?,
                        payload_json = '{}', image_bytes = NULL, image_content_type = NULL
                    WHERE id = ?
                    """,
                    (utc_text(), str(exc)[:500] or type(exc).__name__, action["id"]),
                )
            else:
                await self.bot.db.execute(
                    """
                    UPDATE event_dashboard_actions
                    SET status = 'completed', processed_at_utc = ?, result_event_id = ?,
                        result_message = ?, payload_json = '{}', image_bytes = NULL,
                        image_content_type = NULL, failure_reason = NULL
                    WHERE id = ?
                    """,
                    (utc_text(), event_id, message, action["id"]),
                )
                if action["action"] != "confirm_subscription":
                    guild = self.bot.get_guild(int(action["guild_id"]))
                    if guild is not None:
                        await self.refresh_guild(guild)
            await self.bot.db.commit()

    @action_worker.before_loop
    async def before_action_worker(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventsSync(bot))
