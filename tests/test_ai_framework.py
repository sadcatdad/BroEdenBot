import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from utils.ai_config import get_ai_config
from utils.ai_costs import estimate_ai_cost_usd, estimate_tokens_from_text
from utils.ai_service import (
    AI_DISABLED_MESSAGE,
    AIResult,
    AITokenUsage,
    can_run_ai_request,
    generate_ai_response,
    get_daily_ai_usage_usd,
    initialize_ai_usage_schema,
    log_ai_usage,
    select_ai_model,
)


class AIConfigTests(unittest.TestCase):
    def test_ai_config_parses_booleans_numbers_and_defaults(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-key",
                "AI_ENABLED": "false",
                "AI_ENABLE_ADVANCED_MODEL": "true",
                "AI_DAILY_BUDGET_USD": "0.12",
                "AI_MAX_OUTPUT_TOKENS": "600",
                "AI_LOG_PROMPTS": "TRUE",
                "AI_LOG_RESPONSES": "no",
            },
            clear=False,
        ):
            config = get_ai_config()

        self.assertFalse(config.enabled)
        self.assertTrue(config.api_key_present)
        self.assertTrue(config.advanced_enabled)
        self.assertEqual(config.budgets.daily_usd, 0.12)
        self.assertEqual(config.token_limits.max_output_tokens, 600)
        self.assertTrue(config.logging.log_prompts)
        self.assertFalse(config.logging.log_responses)

    def test_model_routing_falls_back_when_advanced_is_disabled(self):
        with patch.dict(
            os.environ,
            {
                "AI_MODEL_DEFAULT": "gemini-2.5-flash",
                "AI_MODEL_ADVANCED": "gemini-3-flash-preview",
                "AI_ENABLE_ADVANCED_MODEL": "false",
            },
            clear=False,
        ):
            model, tier = select_ai_model("advanced")

        self.assertEqual(model, "gemini-2.5-flash")
        self.assertEqual(tier, "default")


class AICostTests(unittest.TestCase):
    def test_token_estimate_and_cost_are_stable(self):
        self.assertEqual(estimate_tokens_from_text("abcd"), 1)
        self.assertEqual(estimate_tokens_from_text("abcde"), 2)
        cost = estimate_ai_cost_usd(
            model="gemini-2.5-flash",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        self.assertEqual(cost, 2.8)


class AIUsageDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.connection = await aiosqlite.connect(self.database)
        await initialize_ai_usage_schema(self.connection)

    async def asyncTearDown(self):
        await self.connection.close()
        self.temporary_directory.cleanup()

    async def test_usage_logging_does_not_store_prompt_or_response_text(self):
        result = AIResult(
            ok=True,
            text="AI response text that should not be stored",
            model_used="gemini-2.5-flash",
            tier_used="default",
            usage=AITokenUsage(10, 5, 15, False),
            estimated_cost_usd=0.0001,
        )
        await log_ai_usage(
            self.connection,
            result,
            guild_id=1,
            channel_id=2,
            user_id=3,
            source_command="/ai test",
            task_type="framework_test",
            requested_tier="default",
        )

        raw = sqlite3.connect(self.database)
        try:
            columns = [row[1] for row in raw.execute("PRAGMA table_info(ai_usage_logs)")]
            stored = raw.execute("SELECT * FROM ai_usage_logs").fetchone()
        finally:
            raw.close()

        self.assertNotIn("prompt", columns)
        self.assertNotIn("response", columns)
        self.assertEqual(stored is not None, True)

    async def test_budget_check_blocks_when_worst_case_exceeds_daily_budget(self):
        with patch.dict(
            os.environ,
            {
                "AI_DAILY_BUDGET_USD": "0.000001",
                "AI_MONTHLY_BUDGET_USD": "10.00",
            },
            clear=False,
        ):
            can_run, reason, daily, monthly, estimated = await can_run_ai_request(
                self.connection,
                estimated_input_tokens=1000,
                max_output_tokens=1200,
                model="gemini-2.5-flash",
            )

        self.assertFalse(can_run)
        self.assertEqual(reason, "daily")
        self.assertEqual(daily, 0)
        self.assertEqual(monthly, 0)
        self.assertGreater(estimated, 0)

    async def test_daily_usage_reads_logged_costs(self):
        result = AIResult(
            ok=False,
            text=None,
            model_used="gemini-2.5-flash",
            tier_used="default",
            usage=AITokenUsage(1, 1, 2, True),
            estimated_cost_usd=0.25,
            blocked_by_budget=True,
            error="blocked",
        )
        await log_ai_usage(self.connection, result, source_command="/ai test")

        self.assertEqual(await get_daily_ai_usage_usd(self.connection), 0.25)

    async def test_missing_gemini_key_returns_disabled_result_without_network(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "", "AI_ENABLED": "true"},
            clear=False,
        ):
            result = await generate_ai_response(
                task_type="framework_test",
                prompt="hello",
                source_command="/ai test",
                db=self.connection,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, AI_DISABLED_MESSAGE)
        self.assertEqual(await get_daily_ai_usage_usd(self.connection), 0.0)
