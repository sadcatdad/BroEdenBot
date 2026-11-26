"""Side-effect-free matching and dry-run traces for normalized Discord events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
from time import monotonic
from typing import Optional

from automations.models import (
    COMMAND_NAME, CONDITIONS, CONDITION_ALIASES, NEGATIVE_CONDITIONS, PLACEHOLDER, TRIGGERS,
    Condition, DryRun, TraceNode, compiled_pattern, is_id,
)

CooldownKey = tuple[str, str, int]
COMMAND_MESSAGE = re.compile(rf"({COMMAND_NAME})(?:[ \t]+<@!?([0-9]{{1,20}})>)?[ \t]*")


def parse_command(content):
    if not content or not content.startswith("?"):
        return None
    match = COMMAND_MESSAGE.fullmatch(content)
    if not match or (match[2] is not None and not is_id(match[2])):
        return None
    return match[1].lower(), match[2]


@dataclass
class User:
    id: str
    name: Optional[str] = None
    bot: bool = False
    roles: Optional[set[str]] = None
    created_at: Optional[datetime] = None
    joined_at: Optional[datetime] = None
    status: Optional[str] = None

    @property
    def mention(self):
        return f"<@{self.id}>"


@dataclass
class Event:
    trigger: str
    actor: Optional[User] = None
    subject: Optional[User] = None
    users: dict[str, User] = field(default_factory=dict)
    channel_id: Optional[str] = None
    parent_id: Optional[str] = None
    category_id: Optional[str] = None
    is_thread: Optional[bool] = None
    forum_tags: Optional[set[str]] = None
    message_content: Optional[str] = None
    message_id: Optional[str] = None
    message_link: Optional[str] = None
    has_attachment: Optional[bool] = None
    has_embed: Optional[bool] = None
    mentions_users: Optional[set[str]] = None
    mentions_roles: Optional[set[str]] = None
    emoji: Optional[str] = None
    reaction_count: Optional[int] = None
    button_id: Optional[str] = None
    source_rule_id: Optional[int] = None
    member_changes: set[str] = field(default_factory=set)
    added_roles: set[str] = field(default_factory=set)
    removed_roles: set[str] = field(default_factory=set)
    voice_change: Optional[str] = None
    previous_channel_id: Optional[str] = None
    guild_name: Optional[str] = None
    actor_is_admin: bool = False
    actor_pending: bool = False

    def user(self, target):
        if target in (None, "actor"):
            return self.actor
        if target == "subject":
            return self.subject
        for user in (self.actor, self.subject, self.users.get(target)):
            if user and user.id == target:
                return user
        # An explicit ID is known even when role/member details are unavailable.
        return User(target)

    @classmethod
    def simulated(cls, trigger, sample: DryRun, now, *, channel=None, category=None):
        def from_id(value):
            if value is None:
                return None
            try:
                return datetime.fromtimestamp(((int(value) >> 22) + 1420070400000) / 1000, timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None

        def age_date(days, default=None):
            if days is None:
                return default
            return datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)

        users = {
            user.id: User(
                user.id, user.name, user.bot,
                set(user.roles) if user.roles is not None else None,
                age_date(user.account_age_days, from_id(user.id)),
                age_date(user.join_age_days), user.status,
            )
            for user in sample.users
        }
        message = sample.message_content
        subject_id = sample.subject_id
        if trigger == "COMMAND":
            command = parse_command(message)
            subject_id = command[1] if command else None
        for value in (sample.actor_id, subject_id):
            if value and value not in users:
                users[value] = User(value, created_at=from_id(value))
        voice_change = sample.voice_change
        return cls(
            trigger=trigger,
            actor=users.get(sample.actor_id),
            subject=users.get(subject_id),
            actor_is_admin=sample.actor_is_admin,
            users=users,
            channel_id=sample.channel_id,
            previous_channel_id=sample.previous_channel_id,
            parent_id=channel.get("parent_id") if channel and channel.get("is_thread") else None,
            category_id=sample.category_id or category,
            is_thread=sample.is_thread if sample.is_thread is not None else (bool(channel["is_thread"]) if channel else None),
            forum_tags=set(sample.forum_tags) if sample.forum_tags is not None else None,
            message_content=message,
            has_attachment=sample.has_attachment,
            has_embed=sample.has_embed,
            mentions_users=set(sample.mentions_users) if sample.mentions_users is not None else
                (set(re.findall(r"<@!?([0-9]+)>", message)) if message is not None else None),
            mentions_roles=set(sample.mentions_roles) if sample.mentions_roles is not None else
                (set(re.findall(r"<@&([0-9]+)>", message)) if message is not None else None),
            emoji=sample.emoji,
            reaction_count=sample.reaction_count,
            button_id=sample.button_id,
            source_rule_id=sample.source_rule_id,
            member_changes={sample.member_change} if sample.member_change else set(),
            added_roles=set(sample.changed_role_ids),
            removed_roles=set(sample.changed_role_ids),
            voice_change=voice_change,
        )


def emoji_key(value):
    value = str(value)
    return re.sub(r"^<a?:[^:]+:([0-9]+)>$", r"\1", value).replace("\ufe0f", "")


def trigger_matches(trigger, event):
    if trigger.type != event.trigger:
        return False
    if trigger.channel_ids and not set(trigger.channel_ids).intersection(
        (event.channel_id, event.parent_id, event.previous_channel_id)
    ):
        return False
    if trigger.emoji and (event.emoji is None or emoji_key(trigger.emoji) != emoji_key(event.emoji)):
        return False
    if trigger.command:
        parsed = parse_command(event.message_content)
        if not parsed or parsed[0] != trigger.command.name:
            return False
        target_id = parsed[1]
        if target_id:
            return event.subject is not None and target_id == event.subject.id
        return not trigger.command.require_mention
    if trigger.button_id and trigger.button_id != event.button_id:
        return False
    if trigger.source_rule_id and trigger.source_rule_id != event.source_rule_id:
        return False
    if trigger.voice_change not in (None, "any", event.voice_change):
        return False
    if trigger.member_change not in (None, "any"):
        if trigger.member_change not in event.member_changes:
            return False
        if trigger.role_id:
            roles = event.added_roles if trigger.member_change == "role_added" else event.removed_roles
            return trigger.role_id in roles
    return True


def command_access(command, event):
    if not event.actor or event.actor_pending:
        return False
    if command.access == "everyone":
        return True
    if command.access == "roles":
        return bool(event.actor.roles and event.actor.roles.intersection(command.role_ids))
    return event.actor_is_admin


def conditions_in(node):
    if isinstance(node, Condition):
        yield node
    elif node is not None:
        for child in node.rules:
            yield from conditions_in(child)


def cooldown_key(value, event) -> Optional[CooldownKey]:
    if value.scope == "server":
        return ("server", "", value.seconds)
    if value.scope == "user":
        user = event.user(value.target)
        return ("user", user.id, value.seconds) if user else None
    return ("channel", event.channel_id, value.seconds) if event.channel_id else None


def _age_gte(date, days, now):
    if date is None:
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return now - date.timestamp() >= days * 86400


def _condition(condition, event, now, timestamps, regex_deadline):
    kind, value = condition.type, condition.value
    user_target = CONDITIONS[kind][2]
    if user_target:
        user = event.user(condition.target)
        if user is None:
            return None, "The selected user is unavailable."
        if kind in {"USER_ID_IN", "USER_ID_NOT_IN"}:
            return (user.id in value) == (kind == "USER_ID_IN"), None
        if kind == "IS_BOT":
            known = any(u is user for u in (event.actor, event.subject, event.users.get(user.id)))
            return (user.bot == value if known else None), None
        if kind == "ACCOUNT_AGE_DAYS_GTE":
            return _age_gte(user.created_at, value, now), None
        if kind == "SERVER_JOIN_AGE_DAYS_GTE":
            return _age_gte(user.joined_at, value, now), None
        if kind == "USER_STATUS_IS":
            return (user.status == value if user.status is not None else None), None
        if user.roles is None:
            return None, "Roles are unavailable for the selected user."
        if kind in {"HAS_ROLE", "LACKS_ROLE"}:
            return (value in user.roles) == (kind == "HAS_ROLE"), None
        if kind == "HAS_ANY_ROLE":
            return bool(user.roles.intersection(value)), None
        return user.roles.issuperset(value), None

    if kind in {"CHANNEL_IN", "CHANNEL_NOT_IN"}:
        found = bool(set(value).intersection((event.channel_id, event.parent_id)))
        return (found == (kind == "CHANNEL_IN") if event.channel_id else None), None
    if kind == "CATEGORY_IN":
        return (event.category_id in value if event.channel_id else None), None
    if kind == "FORUM_TAG_IS":
        return (value in event.forum_tags if event.forum_tags is not None else None), None
    if kind in {"HAS_ATTACHMENT", "HAS_EMBED", "IS_THREAD"}:
        actual = getattr(event, kind.lower())
        return (actual == value if actual is not None else None), None
    if kind in {"MENTIONS_USER_ID", "MENTIONS_ROLE_ID"}:
        mentions = event.mentions_users if kind == "MENTIONS_USER_ID" else event.mentions_roles
        return (value in mentions if mentions is not None else None), None
    if kind == "EMOJI_IS":
        return (emoji_key(event.emoji) == emoji_key(value) if event.emoji is not None else None), None
    if kind == "REACTION_COUNT_GTE":
        return (event.reaction_count >= value if event.reaction_count is not None else None), None
    if kind == "BUTTON_ID_IS":
        return (event.button_id == value if event.button_id is not None else None), None
    if kind == "COOLDOWN_READY":
        key = cooldown_key(value, event)
        if key is None:
            return None, "The cooldown target is unavailable."
        last = timestamps.get(key)
        remaining = max(0, last + value.seconds - now) if last is not None else 0
        return not remaining, f"{remaining:.1f} seconds remaining." if remaining else None

    message = event.message_content
    if message is None:
        return None, "Message content is unavailable."
    if kind == "STARTS_WITH":
        return message.startswith(value), None
    if kind == "EXACT_MATCH":
        return message == value, None
    if kind == "MESSAGE_LENGTH_GTE":
        return len(message) >= value, None
    try:
        remaining = regex_deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        return bool(compiled_pattern(value).search(message, timeout=min(remaining, 0.005))), None
    except TimeoutError:
        return None, "The regular expression exceeded its matching time limit."


def evaluate(rule, event, *, now, timestamps=None, trace=False, bot_id=None):
    """Return (matches, cooldown keys to claim, trace). Unknown values never pass NOT."""
    timestamps = timestamps or {}
    regex_deadline = monotonic() + 0.025
    gate = [
        TraceNode(label=TRIGGERS[rule.trigger.type], result=trigger_matches(rule.trigger, event)),
        TraceNode(label="Actor allowed", result=bool(
            event.actor and event.actor.id != bot_id and (rule.allow_bots or not event.actor.bot)
        )),
    ]
    if rule.trigger.command:
        gate.append(TraceNode(label="Command access", result=command_access(rule.trigger.command, event)))
    if not trace and not all(node.result for node in gate):
        return False, set(), None

    def walk(node):
        if isinstance(node, Condition):
            passed, detail = _condition(node, event, now, timestamps, regex_deadline)
            if node.negate and passed is not None:
                passed = not passed
            keys = {cooldown_key(node.value, event)} if passed and not node.negate and node.type == "COOLDOWN_READY" else set()
            canonical = CONDITION_ALIASES.get(node.type, node.type)
            negative = node.negate ^ (canonical != node.type or node.value is False)
            label = NEGATIVE_CONDITIONS[canonical] if negative else CONDITIONS[canonical][0]
            if CONDITIONS[node.type][2]:
                label += f" ({node.target or 'actor'})"
            return passed, keys, TraceNode(label=label, result=passed, detail=detail) if trace else None
        results = []
        keys = set()
        children = []
        for child in node.rules:
            result, claim, step = walk(child)
            results.append(result)
            if result is True and (node.operator == "AND" or True not in results[:-1]):
                keys.update(claim)
            if trace:
                children.append(step)
            if not trace and ((node.operator == "AND" and result is False) or
                              (node.operator == "OR" and result is True)):
                break
        if node.operator == "NOT":
            passed = None if results[0] is None else not results[0]
            keys.clear()
        elif node.operator == "AND":
            passed = False if False in results else (None if None in results else True)
        else:
            passed = True if True in results else (None if None in results else False)
        return passed, keys if passed else set(), TraceNode(label=node.operator, result=passed, children=children) if trace else None

    passed, keys, step = walk(rule.conditions) if rule.conditions else (True, set(), None)
    matched = bool(passed and all(node.result for node in gate))
    if step is not None:
        gate.append(step)
    return matched, keys if matched else set(), TraceNode(label="Rule matches", result=matched, children=gate) if trace else None


def substitute(template, event):
    def replacement(match):
        key = match.group(1)
        root, _, attribute = key.partition(".")
        if root in {"actor", "subject"} and attribute in {"id", "name", "mention"}:
            return getattr(event.user(root), attribute, None) or ""
        values = {
            "channel.id": event.channel_id,
            "channel.mention": f"<#{event.channel_id}>" if event.channel_id else "",
            "message.id": event.message_id,
            "message.link": event.message_link,
            "message.content": event.message_content,
            "guild.name": event.guild_name,
            "emoji": event.emoji,
            "button.id": event.button_id,
        }
        return (values[key] or "") if key in values else match.group(0)

    return PLACEHOLDER.sub(replacement, template)
