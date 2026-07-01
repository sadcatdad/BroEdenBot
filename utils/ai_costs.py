"""Token and cost helpers for BroEdenBot AI usage tracking."""

from __future__ import annotations

import math
from dataclasses import dataclass


DEFAULT_PRICING = {
    "input_per_million_usd": 0.50,
    "output_per_million_usd": 3.00,
}

MODEL_PRICING = {
    "gemini-2.5-flash-lite": {
        "input_per_million_usd": 0.10,
        "output_per_million_usd": 0.40,
    },
    "gemini-2.5-flash": {
        "input_per_million_usd": 0.30,
        "output_per_million_usd": 2.50,
    },
    "gemini-3-flash-preview": {
        "input_per_million_usd": 0.50,
        "output_per_million_usd": 3.00,
    },
}


@dataclass(frozen=True)
class AITokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    usage_was_estimated: bool = False

    def as_dict(self) -> dict[str, int]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
        }


def pricing_for_model(model: str) -> dict[str, float]:
    return MODEL_PRICING.get(str(model or "").strip(), DEFAULT_PRICING)


def estimate_tokens_from_text(text: object) -> int:
    if text is None:
        return 0
    return max(0, int(math.ceil(len(str(text)) / 4.0)))


def estimate_ai_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    pricing = pricing_for_model(model)
    input_cost = max(0, input_tokens) / 1_000_000 * pricing["input_per_million_usd"]
    output_cost = (
        max(0, output_tokens) / 1_000_000 * pricing["output_per_million_usd"]
    )
    return input_cost + output_cost
