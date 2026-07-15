"""Reusable Gemini AI service, routing, budget, and usage logging helpers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import aiosqlite
from google import genai
from google.genai import errors, types

from utils.ai_config import AIConfig, get_ai_config
from utils.ai_costs import (
    AITokenUsage,
    estimate_ai_cost_usd,
    estimate_tokens_from_text,
)


logger = logging.getLogger(__name__)

AI_DISABLED_MESSAGE = "AI is disabled in configuration."
AI_BUDGET_MESSAGE = (
    "AI is temporarily paused because the configured budget limit has been reached."
)
AI_EMPTY_RESPONSE_MESSAGE = "AI returned an empty response. Please try again later."
AI_GENERIC_ERROR_MESSAGE = "AI could not complete that request right now."

FUTURE_TASK_TYPES = {
    "ask_server_guide",
    "staff_context_user",
    "public_context_user",
    "staff_context_channel",
    "staff_context_topic",
    "rulecard_draft",
    "ticket_summary",
    "moderation_classification",
    "weekly_recap",
    "onboarding_helper",
}

_COOLDOWNS: dict[tuple[str, str], float] = {}
_COOLDOWN_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class AIResult:
    ok: bool
    text: Optional[str]
    model_used: str
    tier_used: str
    usage: Optional[AITokenUsage]
    estimated_cost_usd: float
    blocked_by_budget: bool = False
    error: Optional[str] = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "text": self.text,
            "modelUsed": self.model_used,
            "tierUsed": self.tier_used,
            "usage": self.usage.as_dict() if self.usage is not None else None,
            "usageWasEstimated": (
                self.usage.usage_was_estimated if self.usage is not None else None
            ),
            "estimatedCostUsd": self.estimated_cost_usd,
            "blockedByBudget": self.blocked_by_budget,
            "error": self.error,
        }


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _day_prefix() -> str:
    return _local_now().date().isoformat()


def _month_prefix() -> str:
    return _local_now().strftime("%Y-%m")


def select_ai_model(
    requested_tier: Optional[str],
    config: Optional[AIConfig] = None,
) -> tuple[str, str]:
    ai_config = config or get_ai_config()
    tier = str(requested_tier or "default").strip().casefold()
    if tier not in {"fast", "default", "advanced"}:
        tier = "default"
    if tier == "advanced" and not ai_config.advanced_enabled:
        tier = "default"

    default_model = ai_config.models.default or "gemini-2.5-flash"
    if tier == "fast":
        model = ai_config.models.fast or default_model
        if not model:
            model = default_model
    elif tier == "advanced":
        model = ai_config.models.advanced or default_model
    else:
        model = default_model
    return model or "gemini-2.5-flash", tier


async def initialize_ai_usage_schema(connection: aiosqlite.Connection) -> None:
    await connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            guild_id TEXT,
            channel_id TEXT,
            user_id TEXT,
            source_command TEXT,
            task_type TEXT,
            requested_tier TEXT,
            tier_used TEXT,
            model_used TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            estimated_cost_usd REAL DEFAULT 0,
            usage_was_estimated INTEGER DEFAULT 0,
            success INTEGER DEFAULT 1,
            blocked_by_budget INTEGER DEFAULT 0,
            error_message TEXT
        )
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_usage_logs_created_at
        ON ai_usage_logs (created_at)
        """
    )
    await connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_usage_logs_model_success
        ON ai_usage_logs (model_used, success, blocked_by_budget)
        """
    )
    await connection.commit()


async def get_daily_ai_usage_usd(connection: aiosqlite.Connection) -> float:
    cursor = await connection.execute(
        """
        SELECT COALESCE(SUM(estimated_cost_usd), 0)
        FROM ai_usage_logs
        WHERE created_at LIKE ?
        """,
        (_day_prefix() + "%",),
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    return float(row[0] or 0) if row else 0.0


async def get_monthly_ai_usage_usd(connection: aiosqlite.Connection) -> float:
    cursor = await connection.execute(
        """
        SELECT COALESCE(SUM(estimated_cost_usd), 0)
        FROM ai_usage_logs
        WHERE created_at LIKE ?
        """,
        (_month_prefix() + "%",),
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    return float(row[0] or 0) if row else 0.0


async def can_run_ai_request(
    connection: Optional[aiosqlite.Connection],
    *,
    estimated_input_tokens: int,
    max_output_tokens: int,
    model: str,
    config: Optional[AIConfig] = None,
) -> tuple[bool, str, float, float, float]:
    ai_config = config or get_ai_config()
    estimated_cost = estimate_ai_cost_usd(
        model=model,
        input_tokens=estimated_input_tokens,
        output_tokens=max_output_tokens,
    )
    if connection is None:
        return True, "", 0.0, 0.0, estimated_cost

    await initialize_ai_usage_schema(connection)
    daily_spend = await get_daily_ai_usage_usd(connection)
    monthly_spend = await get_monthly_ai_usage_usd(connection)
    if daily_spend + estimated_cost > ai_config.budgets.daily_usd:
        return False, "daily", daily_spend, monthly_spend, estimated_cost
    if monthly_spend + estimated_cost > ai_config.budgets.monthly_usd:
        return False, "monthly", daily_spend, monthly_spend, estimated_cost
    return True, "", daily_spend, monthly_spend, estimated_cost


async def log_ai_usage(
    connection: Optional[aiosqlite.Connection],
    result: AIResult,
    *,
    guild_id: Optional[object] = None,
    channel_id: Optional[object] = None,
    user_id: Optional[object] = None,
    source_command: Optional[str] = None,
    task_type: Optional[str] = None,
    requested_tier: Optional[str] = None,
) -> None:
    if connection is None:
        return
    await initialize_ai_usage_schema(connection)
    usage = result.usage or AITokenUsage(0, 0, 0, usage_was_estimated=True)
    await connection.execute(
        """
        INSERT INTO ai_usage_logs (
            created_at, guild_id, channel_id, user_id, source_command,
            task_type, requested_tier, tier_used, model_used, input_tokens,
            output_tokens, total_tokens, estimated_cost_usd,
            usage_was_estimated, success, blocked_by_budget, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _local_now().isoformat(),
            str(guild_id) if guild_id is not None else None,
            str(channel_id) if channel_id is not None else None,
            str(user_id) if user_id is not None else None,
            source_command,
            task_type,
            requested_tier,
            result.tier_used,
            result.model_used,
            usage.input_tokens,
            usage.output_tokens,
            usage.total_tokens,
            result.estimated_cost_usd,
            1 if usage.usage_was_estimated else 0,
            1 if result.ok else 0,
            1 if result.blocked_by_budget else 0,
            result.error,
        ),
    )
    await connection.commit()


async def check_ai_cooldown(
    user_id: object,
    scope: str,
    config: Optional[AIConfig] = None,
) -> tuple[bool, float]:
    ai_config = config or get_ai_config()
    normalized_scope = "staff" if str(scope).casefold() == "staff" else "member"
    cooldown_seconds = (
        ai_config.cooldowns.staff_seconds
        if normalized_scope == "staff"
        else ai_config.cooldowns.member_seconds
    )
    if cooldown_seconds <= 0:
        return True, 0.0
    now = time.monotonic()
    key = (str(user_id), normalized_scope)
    async with _COOLDOWN_LOCK:
        last_use = _COOLDOWNS.get(key)
        if last_use is not None and now - last_use < cooldown_seconds:
            return False, cooldown_seconds - (now - last_use)
    return True, 0.0


async def set_ai_cooldown(user_id: object, scope: str) -> None:
    normalized_scope = "staff" if str(scope).casefold() == "staff" else "member"
    async with _COOLDOWN_LOCK:
        _COOLDOWNS[(str(user_id), normalized_scope)] = time.monotonic()


checkAICooldown = check_ai_cooldown
setAICooldown = set_ai_cooldown


def _combined_prompt_text(
    *,
    prompt: str,
    system_instruction: Optional[str],
    context: Optional[object],
) -> str:
    parts = []
    if system_instruction:
        parts.append(str(system_instruction))
    if context:
        parts.append(str(context))
    parts.append(str(prompt or ""))
    return "\n\n".join(parts)


def _contents_for_request(prompt: str, context: Optional[object]) -> str:
    if not context:
        return str(prompt or "")
    return "{context}\n\n{prompt}".format(context=str(context), prompt=str(prompt or ""))


def _usage_from_response(
    response: object,
    *,
    prompt_text: str,
    response_text: str,
) -> AITokenUsage:
    usage_metadata = getattr(response, "usage_metadata", None)
    input_tokens = getattr(usage_metadata, "prompt_token_count", None)
    output_tokens = getattr(usage_metadata, "candidates_token_count", None)
    total_tokens = getattr(usage_metadata, "total_token_count", None)
    if input_tokens is not None or output_tokens is not None or total_tokens is not None:
        input_value = int(input_tokens or 0)
        output_value = int(output_tokens or 0)
        total_value = int(total_tokens or input_value + output_value)
        return AITokenUsage(input_value, output_value, total_value, False)
    input_value = estimate_tokens_from_text(prompt_text)
    output_value = estimate_tokens_from_text(response_text)
    return AITokenUsage(input_value, output_value, input_value + output_value, True)


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "").casefold()
        if code in {401, 403} or "permission" in status or "unauthenticated" in status:
            return "AI could not authenticate with Gemini."
        if code == 404 or "not_found" in status:
            return "The configured AI model was not found."
        if code == 429 or "rate" in status:
            return "AI is rate limited right now. Please try again later."
    return AI_GENERIC_ERROR_MESSAGE


async def generate_ai_response(
    *,
    task_type: str,
    prompt: str,
    system_instruction: Optional[str] = None,
    context: Optional[object] = None,
    requested_tier: str = "default",
    max_output_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    response_schema: Optional[dict[str, object]] = None,
    allow_thinking: bool = False,
    user_id: Optional[object] = None,
    guild_id: Optional[object] = None,
    channel_id: Optional[object] = None,
    source_command: Optional[str] = None,
    metadata: Optional[dict[str, object]] = None,
    db: Optional[aiosqlite.Connection] = None,
) -> AIResult:
    ai_config = get_ai_config()
    model, tier = select_ai_model(requested_tier, ai_config)
    usage = AITokenUsage(0, 0, 0, usage_was_estimated=True)

    if not ai_config.available:
        result = AIResult(
            ok=False,
            text=None,
            model_used=model,
            tier_used=tier,
            usage=usage,
            estimated_cost_usd=0.0,
            error=AI_DISABLED_MESSAGE,
        )
        await log_ai_usage(
            db,
            result,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_command=source_command,
            task_type=task_type,
            requested_tier=requested_tier,
        )
        return result

    prompt_text = _combined_prompt_text(
        prompt=prompt,
        system_instruction=system_instruction,
        context=context,
    )
    input_tokens = estimate_tokens_from_text(prompt_text)
    if input_tokens > ai_config.token_limits.max_input_tokens:
        result = AIResult(
            ok=False,
            text=None,
            model_used=model,
            tier_used=tier,
            usage=AITokenUsage(input_tokens, 0, input_tokens, True),
            estimated_cost_usd=0.0,
            error="AI request is too large for the configured input limit.",
        )
        await log_ai_usage(
            db,
            result,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_command=source_command,
            task_type=task_type,
            requested_tier=requested_tier,
        )
        return result

    output_limit = max(
        1,
        min(
            int(max_output_tokens or ai_config.token_limits.max_output_tokens),
            ai_config.token_limits.max_output_tokens,
        ),
    )
    can_run, _, _, _, preflight_cost = await can_run_ai_request(
        db,
        estimated_input_tokens=input_tokens,
        max_output_tokens=output_limit,
        model=model,
        config=ai_config,
    )
    if not can_run:
        result = AIResult(
            ok=False,
            text=None,
            model_used=model,
            tier_used=tier,
            usage=AITokenUsage(input_tokens, output_limit, input_tokens + output_limit, True),
            estimated_cost_usd=preflight_cost,
            blocked_by_budget=True,
            error=AI_BUDGET_MESSAGE,
        )
        await log_ai_usage(
            db,
            result,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            source_command=source_command,
            task_type=task_type,
            requested_tier=requested_tier,
        )
        return result

    if ai_config.logging.log_prompts:
        logger.info(
            "AI prompt logging enabled: task=%s source=%s prompt=%r",
            task_type,
            source_command,
            prompt_text,
        )

    try:
        client = genai.Client(api_key=ai_config.api_key)
        config_kwargs: dict[str, Any] = {
            "temperature": (
                ai_config.default_temperature
                if temperature is None
                else float(temperature)
            ),
            "max_output_tokens": output_limit,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = str(system_instruction)
        if response_schema is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = response_schema
        if "gemini-2.5" in model.casefold():
            # Gemini 2.5 Flash is a hybrid reasoning model. We disable thinking
            # by default for latency/cost on extractive calls. Reasoning-heavy
            # synthesis (e.g. building a structured, multi-field summary from
            # intermediate recaps) opts back in via allow_thinking — otherwise
            # the model tends to satisfy an all-required schema with empty field
            # values. When thinking is enabled we cap it to a fixed, configurable
            # budget (AI_STRUCTURED_THINKING_BUDGET) rather than leaving it
            # dynamic, so the thinking pass cannot starve the answer's
            # output-token budget and truncate the JSON.
            thinking_budget = (
                ai_config.token_limits.structured_thinking_budget
                if allow_thinking
                else 0
            )
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=thinking_budget
            )
        response = await client.aio.models.generate_content(
            model=model,
            contents=_contents_for_request(prompt, context),
            config=types.GenerateContentConfig(**config_kwargs),
        )
        try:
            response_text = response.text
        except Exception as exc:
            raise RuntimeError("Gemini returned a blocked response.") from exc
        if not response_text or not response_text.strip():
            result = AIResult(
                ok=False,
                text=None,
                model_used=model,
                tier_used=tier,
                usage=AITokenUsage(input_tokens, 0, input_tokens, True),
                estimated_cost_usd=0.0,
                error=AI_EMPTY_RESPONSE_MESSAGE,
            )
        else:
            text = response_text.strip()
            usage = _usage_from_response(
                response,
                prompt_text=prompt_text,
                response_text=text,
            )
            estimated_cost = estimate_ai_cost_usd(
                model=model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            result = AIResult(
                ok=True,
                text=text,
                model_used=model,
                tier_used=tier,
                usage=usage,
                estimated_cost_usd=estimated_cost,
            )
            if ai_config.logging.log_responses:
                logger.info(
                    "AI response logging enabled: task=%s source=%s response=%r",
                    task_type,
                    source_command,
                    text,
                )
    except Exception as exc:
        logger.error(
            "AI request failed: task=%s source=%s model=%s tier=%s type=%s code=%r status=%r metadata=%r",
            task_type,
            source_command,
            model,
            tier,
            type(exc).__name__,
            getattr(exc, "code", None),
            getattr(exc, "status", None),
            metadata or {},
        )
        result = AIResult(
            ok=False,
            text=None,
            model_used=model,
            tier_used=tier,
            usage=AITokenUsage(input_tokens, 0, input_tokens, True),
            estimated_cost_usd=0.0,
            error=_friendly_error(exc),
        )

    await log_ai_usage(
        db,
        result,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        source_command=source_command,
        task_type=task_type,
        requested_tier=requested_tier,
    )
    return result


generateAIResponse = generate_ai_response
