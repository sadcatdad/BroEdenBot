"""Discord event adapters and runtime for Garden Automations."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import AsyncExitStack, suppress
from hashlib import sha256
import json
import logging
from pathlib import Path
import time
from typing import Literal, Optional
from uuid import uuid4

import discord
from discord import app_commands
from discord.ext import commands

from automations.evaluator import Event, User, command_access, conditions_in, emoji_key, parse_command, trigger_matches
from automations.executor import Executor
from automations.models import DryRun, Rule
from automations.storage import MissingRule, Store, RevisionConflict, configured_guild
from automations.views import AutomationButton

logger = logging.getLogger(__name__)
COG_NAME = "AutomationCog"
WORKER_COUNT = 4
QUEUE_SIZE = 200


class AutomationCog(commands.Cog, name=COG_NAME):
    automation = app_commands.Group(
        name="automation", description="Manage rules in the configured Discord server.",
        guild_only=True, default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot):
        self.bot = bot
        self.store = None
        self.stack = AsyncExitStack()
        self.tasks = []
        self.queue = asyncio.Queue(QUEUE_SIZE)
        self.guild_id = None
        self.rules_by_trigger = {}
        self.snapshot = None
        self.refresh_lock = asyncio.Lock()
        self.last_queue_warning = 0.0
        self.next_prune = 0.0
        self.seeds = [
            Rule.model_validate(item)
            for item in json.loads(Path(__file__).with_name("rules.json").read_text(encoding="utf-8"))
        ]

    async def cog_load(self):
        try:
            self.store = await self.stack.enter_async_context(Store.open())
            await self.store.initialize()
            await self.refresh()
            self.bot.add_dynamic_items(AutomationButton)
            self.tasks = [asyncio.create_task(self.worker(), name=f"automation-worker-{i}") for i in range(WORKER_COUNT)]
            self.tasks.append(asyncio.create_task(self.watch(), name="automation-rules"))
        except BaseException:
            await self.stack.aclose()
            raise

    async def cog_unload(self):
        self.bot.remove_dynamic_items(AutomationButton)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.stack.aclose()

    async def refresh(self, *, force=False):
        async with self.refresh_lock:
            guild_id = await asyncio.to_thread(configured_guild)
            if guild_id and guild_id != self.guild_id:
                await self.store.seed_once(guild_id, self.seeds)
            async with self.store.lock:
                revision_rows = await self.store.rows(
                    "SELECT id,revision FROM automation_rules WHERE guild_id=? AND enabled=1 AND archived=0 ORDER BY id",
                    (guild_id,),
                )
            signature = (guild_id, tuple(tuple(row) for row in revision_rows))
            if not force and signature == self.snapshot:
                return
            index = defaultdict(list)
            if guild_id:
                for stored in reversed(await self.store.list(guild_id, enabled_only=True)):
                    index[stored.rule.trigger.type].append(stored)
            self.guild_id = guild_id
            self.rules_by_trigger = dict(index)
            self.snapshot = signature

    async def watch(self):
        while True:
            await asyncio.sleep(2)
            try:
                await self.refresh()
                if time.monotonic() >= self.next_prune:
                    await self.store.prune()
                    self.next_prune = time.monotonic() + 3600
            except Exception as exc:
                logger.warning("Automation refresh failed (%s)", type(exc).__name__)

    def in_scope(self, guild_id):
        return self.guild_id is not None and self.guild_id == str(guild_id)

    def enqueue(self, kind, guild, *args):
        if guild is None or not self.in_scope(guild.id) or not self.rules_by_trigger.get(kind):
            return
        try:
            self.queue.put_nowait((kind, guild, args))
        except asyncio.QueueFull:
            if time.monotonic() - self.last_queue_warning >= 60:
                logger.warning("Automation event queue is full; excess gateway events were skipped")
                self.last_queue_warning = time.monotonic()

    async def worker(self):
        while True:
            kind, guild, args = await self.queue.get()
            try:
                if self.in_scope(guild.id):
                    await self.process(kind, guild, *args)
            except discord.HTTPException as exc:
                logger.info("Automation event unavailable (%s, HTTP %s)", kind, exc.status)
            except Exception as exc:
                logger.warning("Automation event failed (%s, %s)", kind, type(exc).__name__)
            finally:
                self.queue.task_done()

    def user(self, user):
        if user is None:
            return None
        member = isinstance(user, discord.Member)
        presence = user.guild.get_member(user.id) if member and self.bot.intents.presences else None
        return User(
            str(user.id), user.display_name, user.bot,
            {str(role.id) for role in user.roles} if member else None,
            user.created_at, user.joined_at if member else None,
            str(presence.status) if presence is not None else None,
        )

    def event(self, kind, guild, actor, *, channel=None, message=None, subject=None, **values):
        is_thread = isinstance(channel, discord.Thread) if channel is not None else None
        parent = channel.parent if is_thread else None
        base = parent if is_thread else channel
        forum_tags = {str(tag.id) for tag in channel.applied_tags} if isinstance(parent, discord.ForumChannel) else None
        category = getattr(base, "category_id", None)
        return Event(
            trigger=kind, actor=self.user(actor), subject=self.user(subject),
            channel_id=str(channel.id) if channel else None,
            parent_id=str(channel.parent_id) if is_thread else None,
            category_id=str(category) if category else None, is_thread=is_thread,
            forum_tags=forum_tags, guild_name=guild.name,
            message_content=message.content if message else None,
            message_id=str(message.id) if message else None,
            message_link=message.jump_url if message else None,
            has_attachment=bool(message.attachments) if message else None,
            has_embed=bool(message.embeds) if message else None,
            mentions_users={str(user.id) for user in message.mentions} if message else None,
            mentions_roles={str(role.id) for role in message.role_mentions} if message else None,
            **values,
        )

    async def subject_for_message(self, message):
        reference = message.reference
        if reference and reference.message_id and reference.channel_id == message.channel.id:
            resolved = reference.resolved or reference.cached_message
            if isinstance(resolved, discord.Message):
                return resolved.author
            if message.type == discord.MessageType.reply and resolved is None:
                with suppress(discord.HTTPException):
                    reply = await message.channel.fetch_message(reference.message_id)
                    return reply.author
        return message.mentions[0] if message.mentions else None

    async def member(self, guild, user_id):
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        with suppress(discord.HTTPException):
            return await guild.fetch_member(int(user_id))
        return None

    async def process(self, kind, guild, *args):
        rules = self.rules_by_trigger.get(kind, ())
        if not rules and kind != "BUTTON_CLICK":
            return
        channel = message = previous_channel = None
        if kind == "BUTTON_CLICK":
            event, interaction = args
            try:
                if time.time() - interaction.created_at.timestamp() > 60:
                    await interaction.edit_original_response(content="This automation request expired. Try again.")
                    return
                if not await self.button_active(str(guild.id), event.source_rule_id, event.button_id):
                    await interaction.edit_original_response(content="This automation button is unavailable.")
                    return
                await self.run_rules(guild, event, str(interaction.id), rules, channel=interaction.channel,
                                     message=interaction.message, interaction=interaction)
            except Exception:
                with suppress(discord.HTTPException):
                    await interaction.edit_original_response(content="This automation could not finish.")
                raise
            return
        if kind in {"MESSAGE", "COMMAND"}:
            message = args[0]
            channel = message.channel
            event = self.event(kind, guild, message.author, channel=channel, message=message)
            if kind == "COMMAND":
                if not await self.resolve_command(guild, message, event, rules):
                    return
            else:
                if not any(trigger_matches(rule.rule.trigger, event) for rule in rules):
                    return
                event.subject = self.user(await self.subject_for_message(message))
            key = f"message:{message.id}"
        elif kind in {"REACTION_ADD", "REACTION_REMOVE"}:
            payload = args[0]
            channel = guild.get_channel_or_thread(payload.channel_id) or await guild.fetch_channel(payload.channel_id)
            emoji = str(payload.emoji)
            if not isinstance(channel, discord.abc.Messageable):
                return
            stub = self.event(kind, guild, None, channel=channel, emoji=emoji)
            if not any(trigger_matches(rule.rule.trigger, stub) for rule in rules):
                return
            actor = payload.member or await self.member(guild, payload.user_id)
            if not actor or (actor.bot and not any(rule.rule.allow_bots for rule in rules)):
                return
            message = await channel.fetch_message(payload.message_id)
            count = next((reaction.count for reaction in message.reactions if emoji_key(reaction.emoji) == emoji_key(emoji)), 0)
            event = self.event(kind, guild, actor, channel=channel, message=message, subject=message.author, emoji=emoji, reaction_count=count)
            key = str(uuid4())
        elif kind == "MEMBER_JOIN":
            member = args[0]
            event = self.event(kind, guild, member)
            key = f"join:{member.id}:{member.joined_at}" if member.joined_at else str(uuid4())
        elif kind == "MEMBER_UPDATE":
            before, member = args
            old_roles, new_roles = {str(r.id) for r in before.roles}, {str(r.id) for r in member.roles}
            added, removed = new_roles - old_roles, old_roles - new_roles
            changes = {change for change, changed in (
                ("role_added", added), ("role_removed", removed), ("nickname", before.nick != member.nick),
                ("timeout", before.timed_out_until != member.timed_out_until), ("screening", before.pending != member.pending),
            ) if changed}
            event = self.event(kind, guild, member, member_changes=changes, added_roles=added, removed_roles=removed)
            key = str(uuid4())
        elif kind == "THREAD_CREATE":
            channel = args[0]
            actor = await self.member(guild, channel.owner_id) if channel.owner_id else None
            if not actor:
                return
            event = self.event(kind, guild, actor, channel=channel)
            key = f"thread:{channel.id}"
        else:
            actor, before, after = args
            channel = after.channel or before.channel
            previous_channel = before.channel if after.channel and before.channel != after.channel else None
            if before.channel == after.channel:
                change = "settings"
            elif before.channel is None:
                change = "join"
            elif after.channel is None:
                change = "leave"
            else:
                change = "move"
            event = self.event(kind, guild, actor, channel=channel, voice_change=change,
                               previous_channel_id=str(before.channel.id) if before.channel else None)
            key = str(uuid4())
        await self.run_rules(guild, event, key, rules, channel=channel, previous_channel=previous_channel, message=message)

    async def resolve_command(self, guild, message, event, rules):
        parsed = parse_command(message.content)
        author = message.author
        if not parsed or not isinstance(author, discord.Member):
            return False
        _, target_id = parsed
        mentioned = next((user for user in message.mentions if str(user.id) == target_id), None)
        event.subject = self.user(mentioned)
        event.actor_is_admin = author.guild_permissions.administrator
        event.actor_pending = author.pending
        if not any(
            (not author.bot or row.rule.allow_bots)
            and trigger_matches(row.rule.trigger, event)
            and command_access(row.rule.trigger.command, event)
            for row in rules
        ):
            return False

        # Refresh the invoker before any user-triggered access decision can start actions.
        author = await guild.fetch_member(author.id)
        event.actor = self.user(author)
        event.actor_is_admin = author.guild_permissions.administrator
        event.actor_pending = author.pending
        if target_id:
            subject = author if str(author.id) == target_id else await self.member(guild, target_id)
            event.subject = self.user(subject)
            if event.subject is None:
                return False
        return True

    async def run_rules(self, guild, event, key, rules, *, channel=None, previous_channel=None, message=None, interaction=None):
        bot_id = self.bot.user.id
        if not event.actor or event.actor.id == str(bot_id):
            return
        key = sha256(f"{event.trigger}:{key}".encode()).hexdigest()
        attempted = set()
        used = False
        replied = False
        for stored in rules:
            if (event.actor.bot and not stored.rule.allow_bots) or not trigger_matches(stored.rule.trigger, event):
                continue
            if stored.rule.trigger.command and not command_access(stored.rule.trigger.command, event):
                continue
            for condition in conditions_in(stored.rule.conditions):
                target = condition.target
                if (not target or target in {"actor", "subject"} or target in event.users or target in attempted
                        or target in {event.actor.id, getattr(event.subject, "id", None)}
                        or condition.type in {"USER_ID_IN", "USER_ID_NOT_IN"}):
                    continue
                cached = guild.get_member(int(target))
                if cached is None and len(attempted) >= 10:
                    continue
                if cached is None:
                    attempted.add(target)
                    cached = await self.member(guild, target)
                if cached:
                    event.users[target] = self.user(cached)
            run_id = await self.store.claim(stored, event, key, bot_id=str(bot_id))
            if run_id is None:
                continue
            runner = Executor(guild, event, channel=channel, previous_channel=previous_channel,
                              message=message, interaction=interaction, bot_id=bot_id, stored=stored)
            runner.responded = replied

            async def active():
                # Every effect rechecks the rule's current revision, including after a paced wait.
                if not self.in_scope(guild.id):
                    return False
                async with self.store.lock:
                    rows = await self.store.rows(
                        "SELECT 1 FROM automation_rules WHERE id=? AND guild_id=? AND revision=? AND enabled=1 AND archived=0",
                        (stored.id, str(guild.id), stored.revision),
                    )
                return bool(rows)

            try:
                results = await runner.run(active)
            except asyncio.CancelledError:
                await self.store.finish(run_id, "interrupted", [])
                raise
            except Exception:
                await self.store.finish(run_id, "interrupted", [])
                raise
            statuses = {result["status"] for result in results}
            status = statuses.pop() if len(statuses) == 1 else "partial"
            await self.store.finish(run_id, status, results)
            replied = runner.responded
            used = used or any(result["status"] == "succeeded" for result in results)
        if interaction and not replied:
            await interaction.edit_original_response(content="Automation completed." if used else "No automation action completed.")

    @commands.Cog.listener()
    async def on_message(self, message):
        if self.bot.user and message.author.id != self.bot.user.id and message.webhook_id is None:
            self.enqueue("MESSAGE", message.guild, message)
            if self.rules_by_trigger.get("COMMAND"):
                parsed = parse_command(message.content)
                if parsed and any(row.rule.trigger.command.name == parsed[0] for row in self.rules_by_trigger["COMMAND"]):
                    self.enqueue("COMMAND", message.guild, message)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        self.enqueue("MEMBER_JOIN", member.guild, member)

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        self.enqueue("MEMBER_UPDATE", after.guild, before, after)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.guild_id and self.bot.user and payload.user_id != self.bot.user.id:
            self.enqueue("REACTION_ADD", self.bot.get_guild(payload.guild_id), payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        if payload.guild_id and self.bot.user and payload.user_id != self.bot.user.id:
            self.enqueue("REACTION_REMOVE", self.bot.get_guild(payload.guild_id), payload)

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        self.enqueue("THREAD_CREATE", thread.guild, thread)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before != after:
            self.enqueue("VOICE_STATE_UPDATE", member.guild, member, before, after)

    async def button_active(self, guild_id, source_id, button_id):
        try:
            source = await self.store.get(guild_id, source_id)
        except MissingRule:
            return False
        return source.rule.enabled and any(
            button.id == button_id for action in source.rule.actions for button in (action.buttons or ())
        )

    async def handle_button(self, interaction, source_id, button_id):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.in_scope(interaction.guild_id) or not interaction.message or interaction.message.author.id != self.bot.user.id:
            await interaction.edit_original_response(content="This automation button is unavailable.")
            return
        if not await self.button_active(self.guild_id, source_id, button_id):
            await interaction.edit_original_response(content="This automation button is unavailable.")
            return
        message = interaction.message
        event = self.event("BUTTON_CLICK", interaction.guild, interaction.user, channel=interaction.channel,
                           message=message, subject=message.author, button_id=button_id, source_rule_id=source_id)
        try:
            self.queue.put_nowait(("BUTTON_CLICK", interaction.guild, (event, interaction)))
        except asyncio.QueueFull:
            await interaction.edit_original_response(content="Automations are busy. Try again shortly.")

    async def check_admin(self, interaction):
        if (not self.in_scope(interaction.guild_id) or not isinstance(interaction.user, discord.Member)
                or not interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("This command requires Discord Administrator access in the configured server.", ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        return True

    @automation.command(name="list", description="List rules and their IDs.")
    async def list_command(self, interaction: discord.Interaction):
        if not await self.check_admin(interaction):
            return
        rules = await self.store.list(self.guild_id)
        lines = [f"#{row.id} · {'On' if row.rule.enabled else 'Off'} · {discord.utils.escape_markdown(row.rule.name)}" for row in rules[:15]]
        if len(rules) > 15:
            lines.append(f"Showing 15 of {len(rules)}. Open Automations in The Garden for the full list.")
        await interaction.followup.send("\n".join(lines)[:1950] or "No automation rules.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @automation.command(name="toggle", description="Enable or disable a saved rule.")
    async def toggle_command(self, interaction: discord.Interaction, rule_id: app_commands.Range[int, 1], enabled: bool):
        if not await self.check_admin(interaction):
            return
        try:
            row = await self.store.get(self.guild_id, rule_id)
            await self.store.update(self.guild_id, rule_id, row.revision, f"discord:{interaction.user.id}", enabled=enabled)
        except (MissingRule, RevisionConflict) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.refresh(force=True)
        await interaction.followup.send(f"Rule #{rule_id} {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    @automation.command(name="reload", description="Refresh enabled rules from The Garden's database.")
    async def reload_command(self, interaction: discord.Interaction):
        if await self.check_admin(interaction):
            await self.refresh(force=True)
            await interaction.followup.send("Automation rules refreshed.", ephemeral=True)

    @automation.command(name="test", description="Simulate a rule without running actions.")
    async def test_command(self, interaction: discord.Interaction, rule_id: app_commands.Range[int, 1],
                           actor: Optional[discord.Member] = None, subject: Optional[discord.Member] = None,
                           message: Optional[app_commands.Range[str, 1, 2000]] = None, emoji: Optional[str] = None,
                           button_id: Optional[str] = None, source_rule_id: Optional[app_commands.Range[int, 1]] = None,
                           reaction_count: Optional[app_commands.Range[int, 0]] = None,
                           member_change: Optional[Literal["role_added", "role_removed", "nickname", "timeout", "screening"]] = None,
                           changed_role: Optional[discord.Role] = None,
                           voice_change: Optional[Literal["join", "leave", "move", "settings"]] = None):
        if not await self.check_admin(interaction):
            return
        try:
            row = await self.store.get(self.guild_id, rule_id)
        except MissingRule as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        trigger = row.rule.trigger.type
        actor = actor or interaction.user
        if row.rule.trigger.command and message is None:
            message = row.rule.trigger.command.name + (f" <@{subject.id}>" if subject else "")
        role_ids = {str(changed_role.id)} if changed_role else set()
        event = self.event(
            trigger, interaction.guild, actor, subject=subject,
            channel=interaction.channel if trigger not in {"MEMBER_JOIN", "MEMBER_UPDATE"} else None,
            emoji=emoji, button_id=button_id, source_rule_id=source_rule_id, reaction_count=reaction_count,
            voice_change=voice_change, member_changes={member_change} if member_change else set(),
            added_roles=role_ids if member_change == "role_added" else set(),
            removed_roles=role_ids if member_change == "role_removed" else set(),
            actor_is_admin=actor.guild_permissions.administrator, actor_pending=actor.pending,
        )
        sample = Event.simulated(event.trigger, DryRun(message_content=message), time.time())
        event.message_content, event.mentions_users, event.mentions_roles = message, sample.mentions_users, sample.mentions_roles
        if trigger == "COMMAND" and (not event.subject or not sample.subject or event.subject.id != sample.subject.id):
            event.subject = sample.subject
        matched, _, trace = await self.store.preview(row.rule, event, row.id, bot_id=str(self.bot.user.id))

        def lines(node, depth=0):
            status = "pass" if node.result is True else "fail" if node.result is False else "unavailable"
            yield f"{'  ' * depth}{node.label}: {status}"
            for child in node.children:
                yield from lines(child, depth + 1)

        preview = "\n".join(lines(trace))
        actions = ", ".join(action.type for action in row.rule.actions) if matched else "None"
        await interaction.followup.send(
            f"Actions: {actions}\n```\n{preview[:1570]}\n```", ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )
