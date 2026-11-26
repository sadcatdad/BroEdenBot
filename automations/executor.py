"""Discord actions with live permission checks and isolated results."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

import discord

from automations.evaluator import substitute
from automations.views import button_view

logger = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


class ActionUnavailable(Exception):
    pass


def rendered_embed(value, event):
    def render(text, limit):
        return substitute(text, event)[:limit] if text else None

    embed = discord.Embed(
        title=render(value.title, 256),
        description=render(value.description, 4096),
        color=value.color,
    )
    if value.footer:
        embed.set_footer(text=render(value.footer, 2048))
    for field in value.fields:
        embed.add_field(name=render(field.name, 256), value=render(field.value, 1024), inline=field.inline)
    if len(embed) > 6000:
        raise ActionUnavailable("The rendered embed exceeds Discord's 6,000-character limit.")
    return embed


def preview_action(action, event):
    result = action.model_dump(exclude_none=True)
    for key in ("template", "name", "reason"):
        if key in result:
            result[key] = substitute(result[key], event)
    if action.embed:
        try:
            result["embed"] = rendered_embed(action.embed, event).to_dict()
        except ActionUnavailable as exc:
            result["preview_error"] = str(exc)
    if action.target:
        target = event.user(action.target)
        result["resolved_user_id"] = target.id if target else None
    if action.channel_id is None and action.type in {"SEND_MESSAGE", "CREATE_THREAD", "EDIT_MESSAGE", "PIN_MESSAGE", "REPLY_MESSAGE"}:
        result["resolved_channel_id"] = event.channel_id
    return result


def visibility_channel(channel):
    if isinstance(channel, discord.Thread):
        return None if channel.is_private() else channel.parent
    if isinstance(channel, discord.abc.GuildChannel):
        return channel
    return None


def source_audience_allows(source, destination):
    """Allow crossing channels only within the same or a narrower visibility boundary."""
    if source is None or source.id == destination.id:
        return True
    if source.guild.id != destination.guild.id:
        return False
    if isinstance(source, discord.Thread) and source.is_private():
        return False
    if isinstance(destination, discord.Thread) and destination.is_private():
        destination = destination.parent
    source = visibility_channel(source)
    destination = visibility_channel(destination)
    if not source or not destination:
        return False
    if getattr(source, "is_nsfw", lambda: False)():
        if not hasattr(destination, "is_nsfw") or not destination.is_nsfw():
            return False

    def visibility_signature(channel):
        return {
            target.id: (overwrite.view_channel, overwrite.read_message_history)
            for target, overwrite in channel.overwrites.items()
            if overwrite.view_channel is not None or overwrite.read_message_history is not None
        }

    audience = visibility_signature(source)
    if audience == visibility_signature(destination):
        return True
    everyone = source.permissions_for(source.guild.default_role)
    return everyone.view_channel and everyone.read_message_history and all(
        False not in overwrite for overwrite in audience.values()
    )


async def member_can_read(channel, member):
    if channel is None:
        return True
    permissions = channel.permissions_for(member)
    if not (permissions.view_channel and permissions.read_message_history):
        return False
    if isinstance(channel, discord.Thread) and channel.is_private() and not permissions.manage_threads:
        try:
            await channel.fetch_member(member.id)
        except discord.NotFound:
            return False
    return True


class Executor:
    def __init__(self, guild, event, *, channel=None, previous_channel=None, message=None, interaction=None, bot_id, stored):
        self.guild = guild
        self.event = event
        self.channel = channel
        self.source_channels = tuple(source for source in (channel, previous_channel) if source is not None)
        self.message = message
        self.interaction = interaction
        self.bot_id = bot_id
        self.stored = stored
        self.reason = f"Garden automation #{stored.id}"
        self.thread = None
        self.responded = False

    def check_destination(self, destination):
        if any(not source_audience_allows(source, destination) for source in self.source_channels):
            raise ActionUnavailable("The destination may expose activity from a restricted channel.")

    async def get_channel(self, channel_id):
        if not channel_id:
            if self.channel is not None:
                return self.channel
            raise ActionUnavailable("No destination channel is available.")
        channel = self.guild.get_channel_or_thread(int(channel_id))
        if channel is None:
            channel = await self.guild.fetch_channel(int(channel_id))
        if channel.guild.id != self.guild.id:
            raise ActionUnavailable("This channel is outside the configured server.")
        return channel

    async def get_message(self, action):
        if not action.message_id:
            if self.message is None:
                raise ActionUnavailable("The trigger message is unavailable.")
            return self.message
        channel = await self.get_channel(action.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise ActionUnavailable("The channel cannot contain messages.")
        return await channel.fetch_message(int(action.message_id))

    async def get_thread(self, action):
        thread = await self.get_channel(action.thread_id) if action.thread_id else (self.thread or self.channel)
        if not isinstance(thread, discord.Thread):
            raise ActionUnavailable("Choose a thread or forum post.")
        self.check_destination(thread)
        return thread

    async def get_member(self, target):
        user = self.event.user(target)
        if user is None:
            raise ActionUnavailable("The selected user is unavailable.")
        # Fetch current roles and membership before each user-affecting action.
        return await self.guild.fetch_member(int(user.id))

    def manageable(self, member):
        bot = self.guild.me
        if (bot is None or bot.top_role <= member.top_role or member.id in (self.guild.owner_id, self.bot_id)
                or member.guild_permissions.administrator):
            raise ActionUnavailable("Discord's role hierarchy protects this member.")
        return bot

    def payload(self, action, *, editing=False):
        result = {"allowed_mentions": NO_MENTIONS}
        if action.template:
            result["content"] = substitute(action.template, self.event)[:2000]
        if action.embed:
            result["embed"] = rendered_embed(action.embed, self.event)
        if action.buttons is not None and (action.buttons or editing):
            result["view"] = button_view(self.stored.id, action.buttons) if action.buttons else None
        if not ((editing and len(result) > 1) or result.get("content") or result.get("embed") or result.get("view")):
            raise ActionUnavailable("The rendered message is empty.")
        return result

    async def create_thread(self, action):
        channel = await self.get_channel(action.channel_id)
        self.check_destination(channel)
        name = substitute(action.name, self.event).strip()[:100]
        if not name:
            raise ActionUnavailable("The rendered thread name is empty.")
        content = substitute(action.template, self.event)[:2000] if action.template else None
        if isinstance(channel, discord.ForumChannel):
            created = await channel.create_thread(name=name, content=content or name, allowed_mentions=NO_MENTIONS, reason=self.reason)
            self.thread = created.thread
        elif isinstance(channel, discord.TextChannel):
            if self.message and self.message.channel.id == channel.id and not self.message.thread:
                self.thread = await self.message.create_thread(name=name, reason=self.reason)
            elif channel.type == discord.ChannelType.news:
                raise ActionUnavailable("Announcement threads need a message to start from.")
            else:
                self.thread = await channel.create_thread(name=name, type=discord.ChannelType.public_thread, reason=self.reason)
            if content:
                await self.thread.send(content, allowed_mentions=NO_MENTIONS)
        else:
            raise ActionUnavailable("Threads need a text, announcement or forum channel.")

    async def execute(self, action):
        kind = action.type
        if kind == "SEND_MESSAGE":
            channel = await self.get_channel(action.channel_id)
            self.check_destination(channel)
            if not isinstance(channel, discord.abc.Messageable):
                raise ActionUnavailable("This channel cannot receive messages.")
            await channel.send(**self.payload(action))
        elif kind == "DM_USER":
            member = await self.get_member(action.target)
            for source in self.source_channels:
                channel = source.parent if isinstance(source, discord.Thread) else source
                if getattr(channel, "is_nsfw", lambda: False)():
                    raise ActionUnavailable("Age-restricted channel activity cannot be sent in a direct message.")
                if not await member_can_read(source, member):
                    raise ActionUnavailable("This member cannot read the trigger channel.")
            await member.send(**self.payload(action))
        elif kind == "EPHEMERAL_REPLY":
            if self.interaction is None:
                raise ActionUnavailable("A button interaction is required.")
            if self.responded:
                await self.interaction.followup.send(ephemeral=True, **self.payload(action))
            else:
                await self.interaction.edit_original_response(**self.payload(action))
                self.responded = True
        elif kind in {"REPLY_MESSAGE", "EDIT_MESSAGE", "DELETE_TRIGGER_MESSAGE", "PIN_MESSAGE"}:
            message = await self.get_message(action)
            self.check_destination(message.channel)
            if kind == "REPLY_MESSAGE":
                await message.reply(**self.payload(action), mention_author=False)
            elif kind == "EDIT_MESSAGE":
                if message.author.id != self.bot_id:
                    raise ActionUnavailable("The bot can only edit its own messages.")
                await message.edit(**self.payload(action, editing=True))
            elif kind == "PIN_MESSAGE":
                await message.pin(reason=self.reason)
            else:
                await message.delete()
        elif kind in {"ADD_ROLE", "REMOVE_ROLE", "TIMEOUT_USER"}:
            member = await self.get_member(action.target)
            bot = self.manageable(member)
            if kind == "TIMEOUT_USER":
                await member.timeout(timedelta(seconds=action.duration_seconds), reason=action.reason or self.reason)
            else:
                role = self.guild.get_role(int(action.role_id))
                if role is None or role.is_default() or role.managed or bot.top_role <= role:
                    raise ActionUnavailable("The role is unavailable or above the bot's role.")
                if kind == "ADD_ROLE":
                    if any(getattr(role.permissions, perm) for perm in (
                        "administrator", "manage_guild", "manage_roles", "manage_channels",
                        "manage_webhooks", "ban_members", "kick_members", "moderate_members",
                    )):
                        raise ActionUnavailable("Automation cannot grant a privileged role.")
                    if role not in member.roles:
                        await member.add_roles(role, reason=self.reason)
                elif role in member.roles:
                    await member.remove_roles(role, reason=self.reason)
        elif kind == "CREATE_THREAD":
            await self.create_thread(action)
        elif kind in {"ADD_USER_TO_THREAD", "APPLY_FORUM_TAG", "CLOSE_FORUM_POST"}:
            thread = await self.get_thread(action)
            if kind == "ADD_USER_TO_THREAD":
                member = await self.get_member(action.target)
                if not thread.parent or not await member_can_read(thread.parent, member):
                    raise ActionUnavailable("This member cannot read the thread's parent channel.")
                await thread.add_user(member)
                return
            if not isinstance(thread.parent, discord.ForumChannel):
                raise ActionUnavailable("This action requires a forum post.")
            if kind == "CLOSE_FORUM_POST":
                await thread.edit(archived=True, locked=True, reason=self.reason)
            else:
                tag = thread.parent.get_tag(int(action.tag_id))
                if tag is None:
                    raise ActionUnavailable("The tag is unavailable in this forum.")
                if tag not in thread.applied_tags:
                    if len(thread.applied_tags) >= 5:
                        raise ActionUnavailable("The forum post already has five tags.")
                    await thread.add_tags(tag, reason=self.reason)

    async def run(self, active):
        results = []
        for index, action in enumerate(self.stored.rule.actions):
            if index:
                await asyncio.sleep(0.25)
            if not await active():
                results.append({"type": action.type, "status": "skipped", "detail": "The rule was changed or disabled."})
                break
            try:
                await self.execute(action)
            except ActionUnavailable as exc:
                results.append({"type": action.type, "status": "failed", "detail": str(exc)})
            except discord.HTTPException as exc:
                results.append({"type": action.type, "status": "failed", "detail": f"Discord returned HTTP {exc.status} (code {exc.code})."})
            except Exception as exc:
                # Exception bodies can contain rendered private content.
                logger.warning("Automation %d action %s failed (%s)", self.stored.id, action.type, type(exc).__name__)
                results.append({"type": action.type, "status": "failed", "detail": "The action could not be completed."})
            else:
                results.append({"type": action.type, "status": "succeeded"})
        return results
