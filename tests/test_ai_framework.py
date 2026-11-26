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
                "AI_STRUCTURED_THINKING_BUDGET": "768",
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
        self.assertEqual(config.token_limits.structured_thinking_budget, 768)
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

    async def test_blocked_attempts_never_count_as_spending(self):
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

        self.assertEqual(await get_daily_ai_usage_usd(self.connection), 0.0)

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


class AIProviderAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        self.connection = await aiosqlite.connect(':memory:')
        self.addAsyncCleanup(self.connection.close)
        await initialize_ai_usage_schema(self.connection)
        self.environment = patch.dict(os.environ, dict(
            AI_ENABLED='true', GEMINI_API_KEY='test-key', AI_MODEL_DEFAULT='gemini-2.5-flash',
            AI_MAX_OUTPUT_TOKENS='256', AI_DAILY_BUDGET_USD='0.001', AI_MONTHLY_BUDGET_USD='10',
        ))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.response = SimpleNamespace(text='A response',
            candidates=[SimpleNamespace(finish_reason='STOP')],
            usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=32,
                                           thoughts_token_count=64, total_token_count=196))
        self.generate = AsyncMock(return_value=self.response)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.generate)))
        self.client_patch = patch('utils.ai_service.genai.Client', return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def test_provider_configuration_errors_are_actionable_without_exposing_raw_details(self):
        from google.genai import errors
        secret = 'private archived message and API credential'
        for code, kind, expected in [
            (400, 'invalid_request', 'configuration (400)'),
            (401, 'authentication', 'authenticate'),
            (403, 'authentication', 'authenticate'),
            (404, 'model_not_found', 'model was not found'),
            (429, 'rate_limited', 'rate limited'),
        ]:
            with self.subTest(code=code):
                self.generate.side_effect = errors.ClientError(code, {'error': {
                    'code': code, 'status': 'INVALID_ARGUMENT', 'message': secret}})
                with self.assertLogs('utils.ai_service', level='ERROR') as logs:
                    result = await generate_ai_response(task_type='test', prompt='hello', db=self.connection,
                                                        sensitive_content=True)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure_kind, kind)
                self.assertIn(expected, result.error)
                self.assertNotIn(secret, str(result.as_dict()))
                self.assertNotIn(secret, str(logs.output))
        cursor = await self.connection.execute('SELECT error_message FROM ai_usage_logs')
        self.assertNotIn(secret, str(await cursor.fetchall()))
        await cursor.close()

    async def test_client_initialization_failure_is_distinguished_from_provider_failure(self):
        self.client_patch.stop()
        with patch('utils.ai_service.genai.Client', side_effect=ImportError('sensitive proxy URI')):
            result = await generate_ai_response(task_type='test', prompt='hello', db=self.connection)
        self.assertEqual(result.failure_kind, 'client_configuration')
        self.assertIn('dependencies and proxy', result.error)
        self.assertNotIn('sensitive proxy URI', result.error)
        self.generate.assert_not_awaited()

    async def test_legacy_blocked_estimates_are_excluded_without_erasing_history(self):
        from datetime import datetime
        from utils.ai_service import get_monthly_ai_usage_usd
        await self.connection.execute('INSERT INTO ai_usage_logs(created_at,estimated_cost_usd,blocked_by_budget) VALUES (?,?,1)',
                                      (datetime.now().astimezone().isoformat(), .75))
        await self.connection.commit()
        self.assertEqual(await get_daily_ai_usage_usd(self.connection), 0)
        self.assertEqual(await get_monthly_ai_usage_usd(self.connection), 0)
        result = await generate_ai_response(task_type='test', prompt='hello', db=self.connection)
        self.assertTrue(result.ok)
        self.generate.assert_awaited_once()
        cursor = await self.connection.execute('SELECT estimated_cost_usd FROM ai_usage_logs WHERE blocked_by_budget=1')
        self.assertEqual((await cursor.fetchone())[0], .75)
        await cursor.close()

    async def test_blocked_request_never_reaches_provider_or_consumes_tokens(self):
        with patch.dict(os.environ, {'AI_DAILY_BUDGET_USD': '0'}):
            result = await generate_ai_response(task_type='test', prompt='hello', db=self.connection)
        self.assertTrue(result.blocked_by_budget)
        self.assertEqual(result.estimated_cost_usd, 0)
        self.assertEqual(result.usage.total_tokens, 0)
        self.generate.assert_not_awaited()
        self.assertEqual(await get_daily_ai_usage_usd(self.connection), 0)

    async def test_response_schema_counts_against_input_limit_before_provider_call(self):
        with patch.dict(os.environ, {'AI_MAX_INPUT_TOKENS': '20'}):
            result = await generate_ai_response(task_type='test', prompt='hello',
                response_schema={'type': 'string', 'description': 'long schema ' * 100}, db=self.connection)
        self.assertFalse(result.ok)
        self.assertIn('input limit', result.error)
        self.generate.assert_not_awaited()

    async def test_truncated_json_is_failure_and_actual_thinking_tokens_are_billed(self):
        self.response.text = '{"summary":"A positive member","contributionRubric":'
        self.response.candidates[0].finish_reason = 'MAX_TOKENS'
        result = await generate_ai_response(task_type='test', prompt='hello', response_schema={'type':'object'}, db=self.connection)
        self.assertFalse(result.ok)
        self.assertIsNone(result.text)
        self.assertEqual(result.finish_reason, 'MAX_TOKENS')
        self.assertEqual(result.usage.output_tokens, 96)
        expected = estimate_ai_cost_usd(model='gemini-2.5-flash', input_tokens=100, output_tokens=96)
        self.assertAlmostEqual(await get_daily_ai_usage_usd(self.connection), expected)

    async def test_empty_paid_response_is_accounted_for_not_reported_as_free(self):
        self.response.text = ''
        self.response.usage_metadata.candidates_token_count = 0
        self.response.usage_metadata.total_token_count = 164
        result = await generate_ai_response(task_type='test', prompt='hello', db=self.connection)
        self.assertFalse(result.ok)
        self.assertEqual(result.usage.output_tokens, 64)
        self.assertGreater(await get_daily_ai_usage_usd(self.connection), 0)

    async def test_concurrent_calls_cannot_spend_the_same_remaining_budget(self):
        import asyncio
        started, release = asyncio.Event(), asyncio.Event()
        async def pending(**kwargs):
            started.set()
            await release.wait()
            return self.response
        self.generate.side_effect = pending
        first = asyncio.create_task(generate_ai_response(task_type='test', prompt='hello', db=self.connection))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            second = await generate_ai_response(task_type='test', prompt='hello', db=self.connection)
            self.assertTrue(second.blocked_by_budget)
            self.assertEqual(self.generate.await_count, 1)
        finally:
            release.set()
            self.assertTrue((await first).ok)
        self.assertLess(await get_daily_ai_usage_usd(self.connection), .001)
