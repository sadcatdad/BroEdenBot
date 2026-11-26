"""Validation and editor metadata shared by the engine and Garden."""

from __future__ import annotations

from functools import lru_cache
import re
from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator
import regex

def check_snowflake(value: str) -> str:
    if int(value) >= 2**64:
        raise ValueError("Discord IDs must fit in an unsigned 64-bit integer.")
    return value


Snowflake = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$"), AfterValidator(check_snowflake)]
Target = Union[Literal["actor", "subject"], Snowflake]
ButtonID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,32}$")]
Template = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
MAX_DEPTH = 8
MAX_NODES = 64
MAX_RULES = 100

TRIGGERS = {
    "MESSAGE": "Message sent",
    "MEMBER_JOIN": "Member joined",
    "MEMBER_UPDATE": "Member updated",
    "REACTION_ADD": "Reaction added",
    "REACTION_REMOVE": "Reaction removed",
    "THREAD_CREATE": "Thread created",
    "VOICE_STATE_UPDATE": "Voice state changed",
    "BUTTON_CLICK": "Automation button clicked",
}
MESSAGE_TRIGGERS = {"MESSAGE", "REACTION_ADD", "REACTION_REMOVE", "BUTTON_CLICK"}
CHANNEL_TRIGGERS = MESSAGE_TRIGGERS | {"THREAD_CREATE", "VOICE_STATE_UPDATE"}
VOICE_CHANGES = ("any", "join", "leave", "move", "settings")
MEMBER_CHANGES = ("any", "role_added", "role_removed", "nickname", "timeout", "screening")

# Each entry gives the label, value editor and whether a Discord user is required.
CONDITIONS = {
    "USER_ID_IN": ("User is in list", "users", True),
    "USER_ID_NOT_IN": ("User is outside list", "users", True),
    "IS_BOT": ("User is a bot", "boolean", True),
    "ACCOUNT_AGE_DAYS_GTE": ("Account age is at least (days)", "number", True),
    "SERVER_JOIN_AGE_DAYS_GTE": ("Server membership is at least (days)", "number", True),
    "USER_STATUS_IS": ("User presence is", "presence", True),
    "HAS_ROLE": ("User has role", "role", True),
    "LACKS_ROLE": ("User lacks role", "role", True),
    "HAS_ANY_ROLE": ("User has any listed role", "roles", True),
    "HAS_ALL_ROLES": ("User has all listed roles", "roles", True),
    "CHANNEL_IN": ("Channel is in list", "channels", False),
    "CHANNEL_NOT_IN": ("Channel is outside list", "channels", False),
    "CATEGORY_IN": ("Category is in list", "categories", False),
    "IS_THREAD": ("Channel is a thread", "boolean", False),
    "FORUM_TAG_IS": ("Forum post has tag", "id", False),
    "KEYWORD_MATCH": ("Message matches regular expression", "regex", False),
    "STARTS_WITH": ("Message starts with", "text", False),
    "EXACT_MATCH": ("Message equals", "text", False),
    "HAS_ATTACHMENT": ("Message has an attachment", "boolean", False),
    "HAS_EMBED": ("Message has an embed", "boolean", False),
    "MENTIONS_USER_ID": ("Message mentions user", "user", False),
    "MENTIONS_ROLE_ID": ("Message mentions role", "role", False),
    "MESSAGE_LENGTH_GTE": ("Message is at least (characters)", "number", False),
    "EMOJI_IS": ("Reaction emoji is", "emoji", False),
    "REACTION_COUNT_GTE": ("Emoji reaction count is at least", "number", False),
    "BUTTON_ID_IS": ("Button ID is", "button", False),
    "COOLDOWN_READY": ("Cooldown has elapsed", "cooldown", False),
}

ACTIONS = {
    "SEND_MESSAGE": ("Send message", (), ("channel_id", "template", "embed", "buttons")),
    "REPLY_MESSAGE": ("Reply to message", (), ("template", "embed", "buttons")),
    "DM_USER": ("Direct message user", ("target",), ("template", "embed")),
    "EDIT_MESSAGE": ("Edit bot's message", (), ("template", "embed", "buttons", "message_id", "channel_id")),
    "DELETE_TRIGGER_MESSAGE": ("Delete trigger message", (), ()),
    "PIN_MESSAGE": ("Pin message", (), ("channel_id", "message_id")),
    "ADD_ROLE": ("Add role", ("target", "role_id"), ()),
    "REMOVE_ROLE": ("Remove role", ("target", "role_id"), ()),
    "TIMEOUT_USER": ("Timeout user", ("target", "duration_seconds"), ("reason",)),
    "CREATE_THREAD": ("Create thread", ("name",), ("channel_id", "template")),
    "ADD_USER_TO_THREAD": ("Add user to thread", ("target",), ("thread_id",)),
    "APPLY_FORUM_TAG": ("Apply forum tag", ("tag_id",), ("thread_id",)),
    "CLOSE_FORUM_POST": ("Close forum post", (), ("thread_id",)),
    "EPHEMERAL_REPLY": ("Reply privately to button", (), ("template", "embed")),
}
MESSAGE_ACTIONS = {"SEND_MESSAGE", "REPLY_MESSAGE", "DM_USER", "EDIT_MESSAGE", "EPHEMERAL_REPLY"}
PRESENCES = ("online", "idle", "dnd", "offline")
TEMPLATE_KEYS = [
    f"{target}.{attribute}"
    for target in ("actor", "subject")
    for attribute in ("id", "name", "mention")
] + ["channel.id", "channel.mention", "message.id", "message.link", "message.content",
     "guild.name", "emoji", "button.id"]
PLACEHOLDER = re.compile(r"\{([\w.]+)\}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class Trigger(StrictModel):
    type: str
    channel_ids: list[Snowflake] = Field(default_factory=list, max_length=25)
    emoji: Optional[str] = Field(default=None, min_length=1, max_length=100)
    button_id: Optional[ButtonID] = None
    source_rule_id: Optional[Revision] = None
    member_change: Optional[Literal["any", "role_added", "role_removed", "nickname", "timeout", "screening"]] = None
    role_id: Optional[Snowflake] = None
    voice_change: Optional[Literal["any", "join", "leave", "move", "settings"]] = None

    @model_validator(mode="after")
    def check_fields(self):
        if self.type not in TRIGGERS:
            raise ValueError("Choose a supported trigger.")
        if self.channel_ids and self.type not in CHANNEL_TRIGGERS:
            raise ValueError("This trigger has no channel.")
        if self.emoji and self.type not in {"REACTION_ADD", "REACTION_REMOVE"}:
            raise ValueError("Emoji filters require a reaction trigger.")
        if (self.button_id or self.source_rule_id) and self.type != "BUTTON_CLICK":
            raise ValueError("Button filters require a button trigger.")
        if self.type == "BUTTON_CLICK" and not self.button_id:
            raise ValueError("Enter the button ID to listen for.")
        if (self.member_change or self.role_id) and self.type != "MEMBER_UPDATE":
            raise ValueError("Member change filters require a member update trigger.")
        if self.role_id and self.member_change not in {"role_added", "role_removed"}:
            raise ValueError("Choose Role added or Role removed to filter by role.")
        if self.voice_change and self.type != "VOICE_STATE_UPDATE":
            raise ValueError("Voice change filters require a voice trigger.")
        return self


class Cooldown(StrictModel):
    scope: Literal["user", "channel", "server"]
    seconds: Annotated[int, Field(strict=True, ge=1, le=2592000)]
    target: Optional[Target] = None

    @model_validator(mode="after")
    def check_target(self):
        if self.scope != "user" and self.target is not None:
            raise ValueError("Only user cooldowns have a user target.")
        return self


@lru_cache(maxsize=512)
def compiled_pattern(pattern: str):
    return regex.compile(pattern)


def is_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]{0,19}", value)) and int(value) < 2**64


class Condition(StrictModel):
    type: str
    target: Optional[Target] = None
    value: Union[StrictBool, int, str, list[Snowflake], Cooldown]

    @model_validator(mode="after")
    def check_value(self):
        definition = CONDITIONS.get(self.type)
        if definition is None:
            raise ValueError("Choose a supported condition.")
        _, kind, user_target = definition
        if self.target is not None and not user_target:
            raise ValueError("This condition does not take a user target.")
        value = self.value
        if kind in {"users", "roles", "channels", "categories"}:
            valid = isinstance(value, list) and 1 <= len(value) <= 25 and all(map(is_id, value))
        elif kind in {"id", "user", "role"}:
            valid = is_id(value)
        elif kind == "boolean":
            valid = type(value) is bool
        elif kind == "number":
            valid = type(value) is int and 0 <= value <= 100000
        elif kind == "cooldown":
            valid = isinstance(value, Cooldown)
        elif kind == "presence":
            valid = isinstance(value, str) and value in PRESENCES
        elif kind == "button":
            valid = isinstance(value, str) and bool(re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", value))
        else:
            limits = {"emoji": 100, "regex": 256, "text": 2000}
            valid = isinstance(value, str) and 1 <= len(value) <= limits[kind]
        if not valid:
            raise ValueError(f"Enter a valid value for {definition[0]}.")
        if kind == "regex":
            try:
                compiled_pattern(value)
            except regex.error as exc:
                raise ValueError(f"Invalid regular expression ({exc}).") from None
        return self


class ConditionGroup(StrictModel):
    operator: Literal["AND", "OR", "NOT"]
    rules: list[Union[Condition, ConditionGroup]] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def check_not(self):
        if self.operator == "NOT" and len(self.rules) != 1:
            raise ValueError("NOT requires exactly one child condition or group.")
        return self


ConditionGroup.model_rebuild()


class Button(StrictModel):
    id: ButtonID
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    style: Literal["primary", "secondary", "success", "danger"] = "secondary"


class EmbedField(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    inline: bool = False


class Embed(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=256)
    description: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    color: Optional[int] = Field(default=None, ge=0, le=0xFFFFFF)
    footer: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    fields: list[EmbedField] = Field(default_factory=list, max_length=25)

    @model_validator(mode="after")
    def check_content(self):
        size = sum(len(value or "") for value in (self.title, self.description, self.footer))
        size += sum(len(field.name) + len(field.value) for field in self.fields)
        if size > 6000:
            raise ValueError("An embed can contain at most 6,000 characters.")
        if not (self.title or self.description or self.footer or self.fields):
            raise ValueError("An embed needs a title, description, footer or field.")
        return self


class Action(StrictModel):
    type: str
    target: Optional[Target] = None
    channel_id: Optional[Snowflake] = None
    message_id: Optional[Snowflake] = None
    role_id: Optional[Snowflake] = None
    thread_id: Optional[Snowflake] = None
    tag_id: Optional[Snowflake] = None
    duration_seconds: Optional[int] = Field(default=None, ge=1, le=2419200)
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=400)
    template: Optional[Template] = None
    embed: Optional[Embed] = None
    buttons: Optional[list[Button]] = Field(default=None, max_length=5)

    @model_validator(mode="after")
    def check_fields(self):
        if self.type not in ACTIONS:
            raise ValueError("Choose a supported action.")
        label, required, optional = ACTIONS[self.type]
        missing = [field for field in required if getattr(self, field) is None]
        if missing:
            raise ValueError(f"{label} requires {', '.join(missing)}.")
        invalid = self.model_fields_set - {"type", *required, *optional}
        if invalid:
            raise ValueError(f"{label} does not accept {', '.join(sorted(invalid))}.")
        has_buttons = bool(self.buttons) or (self.type == "EDIT_MESSAGE" and self.buttons is not None)
        if self.type in MESSAGE_ACTIONS and not (self.template or self.embed or has_buttons):
            raise ValueError(f"{label} needs message content, an embed or buttons.")
        if self.buttons and len({button.id for button in self.buttons}) != len(self.buttons):
            raise ValueError("Button IDs must be unique within a message.")
        return self


class Rule(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    enabled: bool = False
    allow_bots: bool = False
    trigger: Trigger
    conditions: Optional[ConditionGroup] = None
    actions: list[Action] = Field(min_length=1, max_length=12)

    @model_validator(mode="before")
    @classmethod
    def bound_tree(cls, data):
        items = [(data.get("conditions"), 1)] if isinstance(data, dict) and data.get("conditions") is not None else []
        count = 0
        while items:
            node, depth = items.pop()
            count += 1
            if depth > MAX_DEPTH or count > MAX_NODES:
                raise ValueError(f"Use at most {MAX_DEPTH} levels and {MAX_NODES} condition nodes.")
            if isinstance(node, dict) and isinstance(node.get("rules"), list):
                items.extend((child, depth + 1) for child in node["rules"])
        return data

    @model_validator(mode="after")
    def check_triggers_and_actions(self):
        trigger = self.trigger.type
        for action in self.actions:
            if action.type == "EPHEMERAL_REPLY" and trigger != "BUTTON_CLICK":
                raise ValueError("Private button replies need a button trigger.")
            if action.type in {"REPLY_MESSAGE", "DELETE_TRIGGER_MESSAGE"} and trigger not in MESSAGE_TRIGGERS:
                raise ValueError(f"{ACTIONS[action.type][0]} needs a message, reaction or button trigger.")
            if action.type in {"PIN_MESSAGE", "EDIT_MESSAGE"} and trigger not in MESSAGE_TRIGGERS and not (action.message_id and action.channel_id):
                raise ValueError(f"{ACTIONS[action.type][0]} requires a destination channel and message ID with this trigger.")
            if action.channel_id and action.type in {"PIN_MESSAGE", "EDIT_MESSAGE"} and not action.message_id:
                raise ValueError(f"{ACTIONS[action.type][0]} needs a message ID when a channel ID is supplied.")
            if action.type in {"SEND_MESSAGE", "CREATE_THREAD"} and trigger not in CHANNEL_TRIGGERS and not action.channel_id:
                raise ValueError(f"{ACTIONS[action.type][0]} needs a destination channel with this trigger.")
            if action.type in {"ADD_USER_TO_THREAD", "APPLY_FORUM_TAG", "CLOSE_FORUM_POST"} and trigger not in MESSAGE_TRIGGERS | {"THREAD_CREATE"} and not action.thread_id:
                raise ValueError(f"{ACTIONS[action.type][0]} needs a thread ID with this trigger.")
        return self


class RuleUpdate(StrictModel):
    revision: Revision
    rule: Rule


class Toggle(StrictModel):
    revision: Revision
    enabled: bool


class SimulatedUser(StrictModel):
    id: Snowflake
    name: Optional[str] = Field(default=None, max_length=100)
    bot: bool = False
    roles: Optional[list[Snowflake]] = Field(default=None, max_length=100)
    account_age_days: Optional[int] = Field(default=None, ge=0, le=100000)
    join_age_days: Optional[int] = Field(default=None, ge=0, le=100000)
    status: Optional[Literal["online", "idle", "dnd", "offline"]] = None


class DryRun(StrictModel):
    rule: Optional[Rule] = None
    actor_id: Optional[Snowflake] = None
    subject_id: Optional[Snowflake] = None
    users: list[SimulatedUser] = Field(default_factory=list, max_length=10)
    channel_id: Optional[Snowflake] = None
    category_id: Optional[Snowflake] = None
    previous_channel_id: Optional[Snowflake] = None
    is_thread: Optional[bool] = None
    forum_tags: Optional[list[Snowflake]] = Field(default=None, max_length=5)
    message_content: Optional[str] = Field(default=None, max_length=4000)
    has_attachment: Optional[bool] = None
    has_embed: Optional[bool] = None
    mentions_users: Optional[list[Snowflake]] = Field(default=None, max_length=100)
    mentions_roles: Optional[list[Snowflake]] = Field(default=None, max_length=100)
    emoji: Optional[str] = Field(default=None, max_length=100)
    reaction_count: Optional[int] = Field(default=None, ge=0, le=100000)
    button_id: Optional[ButtonID] = None
    source_rule_id: Optional[Revision] = None
    member_change: Optional[Literal["any", "role_added", "role_removed", "nickname", "timeout", "screening"]] = None
    changed_role_ids: list[Snowflake] = Field(default_factory=list, max_length=100)
    voice_change: Optional[Literal["any", "join", "leave", "move", "settings"]] = None


class TraceNode(StrictModel):
    label: str
    result: Optional[bool]
    detail: Optional[str] = None
    children: list[TraceNode] = Field(default_factory=list)


TraceNode.model_rebuild()
