"""Central AI framework configuration for BroEdenBot."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_FAST_MODEL = "gemini-2.5-flash-lite"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_ADVANCED_MODEL = "gemini-3-flash-preview"


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default


def _model_env(name: str, default: str) -> str:
    return (os.getenv(name, "").strip() or default).strip()


@dataclass(frozen=True)
class AIModels:
    fast: str
    default: str
    advanced: str


@dataclass(frozen=True)
class AIBudgets:
    daily_usd: float
    monthly_usd: float


@dataclass(frozen=True)
class AITokenLimits:
    max_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class AICooldowns:
    member_seconds: int
    staff_seconds: int


@dataclass(frozen=True)
class AILogging:
    log_prompts: bool
    log_responses: bool


@dataclass(frozen=True)
class AIConfig:
    enabled: bool
    api_key: str
    models: AIModels
    advanced_enabled: bool
    budgets: AIBudgets
    token_limits: AITokenLimits
    default_temperature: float
    cooldowns: AICooldowns
    logging: AILogging
    dashboard_visible: bool

    @property
    def api_key_present(self) -> bool:
        return bool(self.api_key)

    @property
    def available(self) -> bool:
        return self.enabled and self.api_key_present

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "apiKey": self.api_key,
            "models": {
                "fast": self.models.fast,
                "default": self.models.default,
                "advanced": self.models.advanced,
            },
            "advancedEnabled": self.advanced_enabled,
            "budgets": {
                "dailyUsd": self.budgets.daily_usd,
                "monthlyUsd": self.budgets.monthly_usd,
            },
            "tokenLimits": {
                "maxInputTokens": self.token_limits.max_input_tokens,
                "maxOutputTokens": self.token_limits.max_output_tokens,
            },
            "defaultTemperature": self.default_temperature,
            "cooldowns": {
                "memberSeconds": self.cooldowns.member_seconds,
                "staffSeconds": self.cooldowns.staff_seconds,
            },
            "logging": {
                "logPrompts": self.logging.log_prompts,
                "logResponses": self.logging.log_responses,
            },
            "dashboardVisible": self.dashboard_visible,
        }


def get_ai_config() -> AIConfig:
    return AIConfig(
        enabled=parse_bool(os.getenv("AI_ENABLED"), default=True),
        api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        models=AIModels(
            fast=_model_env("AI_MODEL_FAST", DEFAULT_FAST_MODEL),
            default=_model_env("AI_MODEL_DEFAULT", DEFAULT_MODEL),
            advanced=_model_env("AI_MODEL_ADVANCED", DEFAULT_ADVANCED_MODEL),
        ),
        advanced_enabled=parse_bool(os.getenv("AI_ENABLE_ADVANCED_MODEL"), False),
        budgets=AIBudgets(
            daily_usd=max(0.0, _float_env("AI_DAILY_BUDGET_USD", 0.35)),
            monthly_usd=max(0.0, _float_env("AI_MONTHLY_BUDGET_USD", 10.0)),
        ),
        token_limits=AITokenLimits(
            max_input_tokens=max(1, _int_env("AI_MAX_INPUT_TOKENS", 12000)),
            max_output_tokens=max(1, _int_env("AI_MAX_OUTPUT_TOKENS", 1200)),
        ),
        default_temperature=_float_env("AI_DEFAULT_TEMPERATURE", 0.4),
        cooldowns=AICooldowns(
            member_seconds=max(0, _int_env("AI_MEMBER_COOLDOWN_SECONDS", 20)),
            staff_seconds=max(0, _int_env("AI_STAFF_COOLDOWN_SECONDS", 5)),
        ),
        logging=AILogging(
            log_prompts=parse_bool(os.getenv("AI_LOG_PROMPTS"), False),
            log_responses=parse_bool(os.getenv("AI_LOG_RESPONSES"), False),
        ),
        dashboard_visible=parse_bool(os.getenv("AI_DASHBOARD_VISIBLE"), True),
    )
