"""Garden Automations pages and JSON API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
import time
from typing import Annotated, Literal, Optional

import httpx
from fastapi import HTTPException, Path, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import Field, ValidationError

from automations.evaluator import Event
from automations.executor import preview_action
from automations.models import (
    ACTIONS, CHANNEL_TRIGGERS, CONDITIONS, CONDITION_ALIASES, MAX_DEPTH, MAX_NODES,
    MEMBER_CHANGES, MESSAGE_TRIGGERS, NEGATIVE_CONDITIONS, PRESENCES,
    TEMPLATE_KEYS, TRIGGERS, VOICE_CHANGES, DryRun, Revision, Rule, RuleUpdate, SimulatedUser, StrictModel, Toggle,
)
from automations.storage import MissingRule, RevisionConflict, Store, configured_guild
from dashboard.auth import csrf_is_valid, current_permissions, current_user, has_permission
from dashboard.features import FEATURES_BY_KEY, feature_is_enabled
from dashboard.rbac import permissions_for_discord_member, record_audit
from utils import ai_service
from utils.ai_config import get_ai_config
from utils.discord_metadata import initialize_discord_metadata_schema
from utils.scout_access import fetch_member

RULE_ID = Annotated[int, Path(ge=1, le=2**63 - 1)]
MAX_BODY = 512 * 1024


class Archive(StrictModel):
    revision: Revision


class MemberSearch(StrictModel):
    query: str = Field(min_length=2, max_length=80)


class ChatTurn(StrictModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=4000)


class DraftRequest(StrictModel):
    messages: list[ChatTurn] = Field(min_length=1, max_length=8)
    previous_draft: Optional[Rule] = None


class DraftReply(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    rule_json: str = Field(max_length=48000)


EDITOR = {
    "triggers": TRIGGERS,
    "channel_triggers": sorted(CHANNEL_TRIGGERS),
    "message_triggers": sorted(MESSAGE_TRIGGERS),
    "conditions": CONDITIONS,
    "condition_aliases": CONDITION_ALIASES,
    "negative_conditions": NEGATIVE_CONDITIONS,
    "actions": ACTIONS,
    "member_changes": MEMBER_CHANGES,
    "voice_changes": VOICE_CHANGES,
    "presences": PRESENCES,
    "template_keys": TEMPLATE_KEYS,
    "max_depth": MAX_DEPTH,
    "max_nodes": MAX_NODES,
}

DRAFT_SYSTEM = """Draft a Discord automation for an administrator to review in The Garden.
Return a short message and a rule_json string containing one JSON rule, or an empty rule_json
string with a clarifying question. Use the requested trigger, an optional conditions tree and
an ordered actions array. Never invent any Discord IDs, claim activation, ask for secrets,
or recommend transferring private content. Use actor for the event initiator, subject for a
related message author or mention, and numeric strings for literal IDs. Ask for exact IDs
where needed. A new draft is always disabled. Unsupported operations need an explanation.
Templates use only the listed placeholders. Supply no markdown around rule_json.

Rule example:
{"name":"Welcome","trigger":{"type":"MEMBER_JOIN"},"actions":[{"type":"SEND_MESSAGE",
"channel_id":"123456789012345678","template":"Welcome, {actor.mention}!"}]}
Conditions example: {"operator":"AND","rules":[{"type":"HAS_ROLE","target":"actor","value":"123"},
{"operator":"NOT","rules":[{"type":"IS_BOT","value":true}]}]}
Use negate:true on any condition to invert its comparison. For no attachment use
{"type":"HAS_ATTACHMENT","value":true,"negate":true}.
For ?verify @member use {"type":"COMMAND","command":{"name":"?verify","require_mention":true,
"access":"administrators"}}. The actor runs the command and the mentioned member is the subject.
For role-based command access, set command.access to "roles" and supply command.role_ids.
A cooldown has value {"scope":"user","seconds":60,"target":"actor"} (or scope channel/server
without a user target). For button clicks the trigger needs button_id. SEND_MESSAGE buttons
are objects with id, label and style (primary, secondary, success, danger). For role changes
use trigger.member_change with role_added or role_removed and optionally trigger.role_id.
For voice choose trigger.voice_change. The optional trigger.channel_ids is an array.
Embed fields are title, description, integer color, footer, and fields (name, value, inline).
Supported trigger, condition and action metadata (condition entries are label, value type,
takes user target; action entries are label, required fields, optional fields):
""" + json.dumps(EDITOR, separators=(",", ":"))


def validation_errors(exc):
    return [
        {"path": ".".join(map(str, item["loc"])), "message": item["msg"].removeprefix("Value error, ")}
        for item in exc.errors(include_url=False, include_context=False, include_input=False)[:12]
    ]


async def json_body(request, schema):
    if not csrf_is_valid(request, request.headers.get("X-CSRF-Token", "")):
        raise HTTPException(400, "Invalid CSRF token.")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "Send a JSON request body.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BODY:
            raise HTTPException(413, "This automation request is too large.")
        data.extend(chunk)
    try:
        return schema.model_validate_json(data)
    except ValidationError as exc:
        raise HTTPException(422, validation_errors(exc)) from None


async def access(request, *, manage=False):
    cap = "automations.manage" if manage else "automations.view"
    if not has_permission(request, cap):
        raise HTTPException(403, "You do not have access to Automations.")
    user = current_user(request)
    guild = await asyncio.to_thread(configured_guild)
    if not guild:
        return guild, user, current_permissions(request)
    if user.get("auth_provider") == "discord":
        if not user.get("discord_user_id"):
            raise HTTPException(403, "A Discord identity is required.")
        try:
            member = await fetch_member(guild, user["discord_user_id"])
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        if member.get("pending") or member.get("user", {}).get("bot"):
            raise HTTPException(403, "Current server membership is required.")
        permissions = await asyncio.to_thread(permissions_for_discord_member, user["id"], member["roles"])
        if cap not in permissions:
            raise HTTPException(403, "Your current Discord roles do not grant this access.")
        return guild, user, permissions
    return guild, user, current_permissions(request)


@asynccontextmanager
async def session(request, *, manage=False):
    guild, user, permissions = await access(request, manage=manage)
    if not guild:
        raise HTTPException(409, "Set GUILD_ID before configuring Automations.")
    try:
        async with Store.open() as store:
            await store.initialize()
            yield store, guild, user, permissions
    except MissingRule as exc:
        raise HTTPException(404, str(exc)) from None
    except RevisionConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


async def audit(user, operation, rule_id):
    await asyncio.to_thread(
        record_audit, actor_user_id=user["id"], actor_label=user["username"],
        action=f"automations.{operation}", target_type="automation_rule", target_id=str(rule_id),
    )


def actor_label(user):
    if user.get("discord_user_id"):
        return f"discord:{user['discord_user_id']}"
    return f"dashboard:{user['id']}"


async def metadata(store, guild):
    await asyncio.to_thread(initialize_discord_metadata_schema)
    return {
        "channel": [dict(row) for row in await store.rows(
            "SELECT id,name,type,parent_id,parent_name,is_thread FROM dashboard_discord_channels WHERE guild_id=? ORDER BY parent_name,position,name", (guild,)
        )],
        "role": [dict(row) for row in await store.rows(
            "SELECT id,name FROM dashboard_discord_roles WHERE guild_id=? AND id<>? ORDER BY position DESC", (guild, guild)
        )],
        "category": [dict(row) for row in await store.rows(
            "SELECT id,name FROM dashboard_discord_categories WHERE guild_id=? ORDER BY position,name", (guild,)
        )],
        "emoji": [dict(row) for row in await store.rows(
            "SELECT id,name FROM dashboard_discord_emojis WHERE guild_id=? ORDER BY name", (guild,)
        )],
    }


async def search_members(guild, query):
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Discord member search is unavailable. Enter a user ID to simulate instead.")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(
                f"https://discord.com/api/v10/guilds/{guild}/members/search",
                headers={"Authorization": f"Bot {token}"}, params={"query": query, "limit": 10},
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Discord member search is unavailable. Enter a user ID to simulate instead.") from None


def simulate_member(member):
    user = member["user"]
    joined = member.get("joined_at")
    join_age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(joined.replace("Z", "+00:00"))).days) if joined else None
    return SimulatedUser(
        id=user["id"], bot=user.get("bot", False), roles=member["roles"],
        name=(member.get("nick") or user.get("global_name") or user["username"]), join_age_days=join_age,
    ).model_dump(exclude_none=True)


async def simulate(store, guild, rule, sample, rule_id):
    channel = category = None
    if sample.channel_id:
        channel_rows = await store.rows(
            """SELECT d.is_thread,d.parent_id,c.id category
               FROM dashboard_discord_channels d
               LEFT JOIN dashboard_discord_channels p ON d.is_thread=1 AND p.id=d.parent_id AND p.guild_id=d.guild_id
               LEFT JOIN dashboard_discord_categories c ON c.id=coalesce(p.parent_id,d.parent_id) AND c.guild_id=d.guild_id
               WHERE d.id=? AND d.guild_id=?""",
            (sample.channel_id, guild),
        )
        if channel_rows:
            channel = dict(channel_rows[0])
            category = channel["category"]
    event = Event.simulated(rule.trigger.type, sample, time.time(), channel=channel, category=category)
    matched, _, trace = await store.preview(rule, event, rule_id)
    return {
        "matched": matched, "active": rule.enabled, "trace": trace.model_dump(),
        "actions": [preview_action(action, event) for action in rule.actions] if matched else [],
    }


def install_automation_routes(app, templates, context):
    def module_enabled():
        return feature_is_enabled(FEATURES_BY_KEY["automations"])

    @app.get("/automations", name="automations_page", response_class=HTMLResponse)
    async def page(request: Request):
        guild, _, permissions = await access(request)
        return templates.TemplateResponse(
            request=request, name="automations.html",
            context=context(request, page_title="Automations", automations_configured=bool(guild),
                            automations_enabled=module_enabled(),
                            automation_manage="automations.manage" in permissions and has_permission(request, "automations.manage"),
                            gemini_available=get_ai_config().available),
        )

    @app.get("/api/automations")
    async def listing(request: Request, archived: bool = False):
        async with session(request) as (store, guild, _, _):
            rules = await store.list(guild, archive=archived)
            return {"rules": [row.public() for row in rules], "editor": EDITOR, "discord": await metadata(store, guild)}

    @app.post("/api/automations")
    async def create(request: Request):
        async with session(request, manage=True) as (store, guild, user, _):
            rule = await json_body(request, Rule)
            result = await store.create(guild, rule, actor_label(user))
            await audit(user, "create", result.id)
            return JSONResponse(result.public(), status_code=201)

    @app.post("/api/automations/validate")
    async def validate(request: Request):
        await access(request, manage=True)
        rule = await json_body(request, Rule)
        return {"valid": True, "rule": rule.model_dump(exclude_none=True)}

    @app.post("/api/automations/test")
    async def test_draft(request: Request):
        async with session(request, manage=True) as (store, guild, _, _):
            sample = await json_body(request, DryRun)
            if sample.rule is None:
                raise HTTPException(422, "Supply a rule draft to test.")
            await asyncio.to_thread(initialize_discord_metadata_schema)
            return await simulate(store, guild, sample.rule, sample, None)

    @app.post("/api/automations/draft")
    async def draft(request: Request):
        async with session(request, manage=True) as (store, guild, user, _):
            chat = await json_body(request, DraftRequest)
            if chat.messages[-1].role != "user" or sum(len(turn.text) for turn in chat.messages) > 8000:
                raise HTTPException(422, "Send a new prompt and keep the conversation under 8,000 characters.")
            if not get_ai_config().available:
                raise HTTPException(503, "Gemini is unavailable in the current AI configuration.")
            identity = user.get("discord_user_id") or f"dashboard:{user['id']}"
            ready, delay = await ai_service.check_ai_cooldown(identity, "staff")
            if not ready:
                raise HTTPException(429, f"Try another Gemini draft in {max(1, int(delay + 1))} seconds.")
            await ai_service.set_ai_cooldown(identity, "staff")
            await ai_service.initialize_ai_usage_schema(store.db)
            result = await ai_service.generate_ai_response(
                task_type="automation_draft", prompt=chat.model_dump_json(exclude_none=True),
                system_instruction=DRAFT_SYSTEM, sensitive_content=True,
                user_id=identity, guild_id=guild, source_command="automations.dashboard.draft",
                response_schema={
                    "type": "OBJECT",
                    "properties": {"message": {"type": "STRING"}, "rule_json": {"type": "STRING"}},
                    "required": ["message", "rule_json"],
                }, max_output_tokens=4096, db=store.db,
            )
            if not result.ok:
                raise HTTPException(503, "Gemini could not create a draft. Check the AI configuration and budget, or try again.")
            try:
                reply = DraftReply.model_validate_json(result.text or "")
                rule = Rule.model_validate_json(reply.rule_json) if reply.rule_json.strip() else None
            except ValidationError:
                raise HTTPException(422, "Gemini returned an invalid draft. Try describing the trigger, conditions and actions more specifically.") from None
            return {
                "message": reply.message,
                "rule": rule.model_copy(update={"enabled": False}).model_dump(exclude_none=True) if rule else None,
            }

    @app.post("/api/automations/members")
    async def members(request: Request):
        guild, _, _ = await access(request)
        if not guild:
            raise HTTPException(409, "Set GUILD_ID before searching for members.")
        values = await json_body(request, MemberSearch)
        query = values.query.strip()
        if len(query) < 2:
            raise HTTPException(422, "Enter at least two characters.")
        rows = await search_members(guild, query)
        return {"members": [simulate_member(member) for member in rows[:10]]}

    @app.get("/api/automations/{rule_id}")
    async def detail(rule_id: RULE_ID, request: Request):
        async with session(request) as (store, guild, _, _):
            return (await store.get(guild, rule_id, archived=True)).public()

    @app.put("/api/automations/{rule_id}")
    async def update(rule_id: RULE_ID, request: Request):
        async with session(request, manage=True) as (store, guild, user, _):
            values = await json_body(request, RuleUpdate)
            result = await store.update(guild, rule_id, values.revision, actor_label(user), rule=values.rule)
            await audit(user, "edit", result.id)
            return result.public()

    @app.post("/api/automations/{rule_id}/toggle")
    async def toggle(rule_id: RULE_ID, request: Request):
        async with session(request, manage=True) as (store, guild, user, _):
            values = await json_body(request, Toggle)
            if values.enabled and not module_enabled():
                raise HTTPException(409, "Enable the automations module in the bot configuration first.")
            result = await store.update(guild, rule_id, values.revision, actor_label(user), enabled=values.enabled)
            await audit(user, "enable" if values.enabled else "disable", result.id)
            return result.public()

    @app.delete("/api/automations/{rule_id}")
    async def archive(rule_id: RULE_ID, request: Request):
        async with session(request, manage=True) as (store, guild, user, _):
            values = await json_body(request, Archive)
            result = await store.update(guild, rule_id, values.revision, actor_label(user), archive=True)
            await audit(user, "archive", result.id)
            return result.public()

    @app.get("/api/automations/{rule_id}/history")
    async def history(rule_id: RULE_ID, request: Request):
        async with session(request) as (store, guild, _, _):
            return await store.history(guild, rule_id)

    @app.post("/api/automations/{rule_id}/test")
    async def test(rule_id: RULE_ID, request: Request):
        async with session(request) as (store, guild, _, permissions):
            stored = await store.get(guild, rule_id)
            sample = await json_body(request, DryRun)
            if sample.rule and ("automations.manage" not in permissions or not has_permission(request, "automations.manage")):
                raise HTTPException(403, "Only automation managers can test an unsaved draft.")
            await asyncio.to_thread(initialize_discord_metadata_schema)
            return await simulate(store, guild, sample.rule or stored.rule, sample, stored.id)
