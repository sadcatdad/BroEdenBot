import asyncio
import json
import re
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.message_context import MessageContext, PUBLIC_USER_EVALUATION_SCHEMA, PUBLIC_EVALUATION_SYSTEM_INSTRUCTION
from tests.community_fixtures import evaluation_rows, evaluation_data
from utils.ai_config import get_ai_config
from utils.public_evaluation import (EVIDENCE_SCHEMA, PublicEvaluationError, pack_evidence,
                                     parse_evidence, strict_object, validate_evaluation)


def response(text, **kwargs):
    return SimpleNamespace(ok=True, text=text, blocked_by_budget=False, model_used='test', error=None, **kwargs)


def evidence_response(kwargs):
    ids = re.findall(r'(?m)^TARGET messageId=([^\n]+)', kwargs['prompt'])
    return response(json.dumps({'observations': [{'category': 'connection',
        'explanation': 'Thoughtful replies.', 'concernType': '', 'evidenceMessageIds': ids[:4]}],
        'quoteMessageIds': ids[:1]}))


class PublicEvaluationValidationTests(unittest.TestCase):
    def test_schemas_serialize_through_sdk_without_empty_enum_options(self):
        # Exercise the pinned SDK's actual wire conversion, not a mocked
        # generate_content method that silently accepts invalid schemas.
        from google.genai import models, types

        def validate(node):
            if isinstance(node, dict):
                if 'enum' in node:
                    self.assertTrue(node['enum'])
                    self.assertTrue(all(isinstance(value, str) and value for value in node['enum']))
                for child in node.values():
                    validate(child)
            elif isinstance(node, list):
                for child in node:
                    validate(child)

        for schema in (EVIDENCE_SCHEMA, PUBLIC_USER_EVALUATION_SCHEMA):
            parameters = types._GenerateContentParameters(
                model='gemini-2.5-flash-lite', contents='Offline schema check',
                config=types.GenerateContentConfig(response_schema=schema),
            )
            wire = models._GenerateContentParameters_to_mldev(SimpleNamespace(vertexai=False), parameters)
            validate(wire['generationConfig']['responseSchema'])

    def test_truncated_json_is_never_salvaged(self):
        for text in ['{"summary":"Enthusiastic and supportive","contributionRubric":',
                     '{"summary":"Enthusiastic and supportive"} trailing prose']:
            with self.assertRaises(ValueError):
                strict_object(text)

    def test_summary_only_and_incomplete_rubrics_cannot_be_published(self):
        for data in [{'summary': 'A highly engaged and positive member.'},
                     {'summary': 'A positive member.', 'strengths': [], 'growthOpportunities': [],
                      'contextQuotes': [], 'limitations': ''}]:
            with self.assertRaises(ValueError):
                validate_evaluation(data)
        data = evaluation_data()
        validate_evaluation(data)
        del data['contributionRubric']['categories']['support']
        with self.assertRaises(ValueError):
            validate_evaluation(data)

    def test_evidence_cannot_reference_context_only_or_invented_messages(self):
        data = {'observations': [{'category': 'support', 'explanation': 'Helpful.',
                'evidenceMessageIds': ['other-user'], 'concernType': ''}], 'quoteMessageIds': []}
        with self.assertRaises(ValueError):
            parse_evidence(json.dumps(data), {'target-id'})

    def test_large_evidence_is_locally_bounded_and_keeps_categories_dates_and_source_quotes(self):
        rows = evaluation_rows(200)
        categories = ['connection', 'inclusion', 'support', 'initiative', 'conduct']
        batches = [{'observations': [{'category': categories[i % 5], 'explanation': 'x' * 160,
                    'concernType': '', 'evidenceMessageIds': [row['message_id']]}],
                    'quoteMessageIds': [row['message_id']]} for i, row in enumerate(rows)]
        text, condensed = pack_evidence(batches, rows, 6000)
        self.assertLessEqual(len(text), 6000)
        self.assertTrue(condensed)
        packed = json.loads(text)
        self.assertEqual({o['category'] for o in packed['observations']}, set(categories))
        self.assertGreater(len({o['dates'][0][:7] for o in packed['observations']}), 1)
        by_id = {row['message_id']: row for row in rows}
        for quote in packed['quoteCandidates']:
            self.assertIn(quote['quote'], by_id[quote['messageId']]['content'])


class PublicEvaluationPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = MessageContext(SimpleNamespace())
        self.rows = evaluation_rows(8)
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=42, channel_id=99)
        self.kwargs = dict(task='Evaluate member', request='90d', schema=PUBLIC_USER_EVALUATION_SCHEMA,
            include_links=True, interaction=self.interaction, source_command='/context user',
            task_type='public_context_user', public_result=True, nearby={},
            system_instruction=PUBLIC_EVALUATION_SYSTEM_INSTRUCTION)

    async def invoke(self):
        return await self.cog._synthesize_structured(self.rows, **self.kwargs)

    async def test_first_provider_failure_stops_all_other_batches_and_surfaces_safe_reason(self):
        failure = SimpleNamespace(ok=False, text=None, blocked_by_budget=False,
            model_used='gemini-2.5-flash-lite', error='Gemini rejected the AI request configuration (400).',
            failure_kind='invalid_request')
        self.rows = evaluation_rows(26)
        with patch('cogs.message_context.MAX_GEMINI_ROWS_PER_CHUNK', 2), \
             patch('cogs.message_context.generate_ai_response', new=AsyncMock(return_value=failure)) as generate:
            with self.assertRaisesRegex(PublicEvaluationError, r'configuration \(400\).*remaining history requests were stopped'):
                await self.invoke()
            generate.assert_awaited_once()
        self.assertFalse(self.cog._active_public_evaluations)

    async def test_later_shared_failure_stops_queued_batches_but_keeps_valid_cache(self):
        self.rows = evaluation_rows(26)
        calls = []
        async def generate(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return evidence_response(kwargs)
            await asyncio.sleep(0)
            return SimpleNamespace(ok=False, text=None, blocked_by_budget=False, model_used='test',
                error='AI is rate limited right now.', failure_kind='rate_limited')
        with patch('cogs.message_context.MAX_GEMINI_ROWS_PER_CHUNK', 2), \
             patch('cogs.message_context.generate_ai_response', side_effect=generate):
            with self.assertRaisesRegex(PublicEvaluationError, 'rate limited'):
                await self.invoke()
        self.assertLessEqual(len(calls), 4)  # First batch plus at most three already in flight.
        self.assertEqual(len(self.cog._public_evidence_cache), 1)
        self.assertFalse(self.cog._active_public_evaluations)

    async def test_generic_first_failure_also_prevents_an_entire_failed_review(self):
        failure = SimpleNamespace(ok=False, text=None, blocked_by_budget=False,
            model_used='test', error='AI could not complete that request right now.')
        with patch('cogs.message_context.MAX_GEMINI_ROWS_PER_CHUNK', 2), \
             patch('cogs.message_context.generate_ai_response', new=AsyncMock(return_value=failure)) as generate:
            with self.assertRaisesRegex(PublicEvaluationError, 'first history batch failed'):
                await self.invoke()
            generate.assert_awaited_once()

    async def test_failed_final_reuses_batches_and_edited_content_invalidates_cache(self):
        calls = []
        final_count = 0
        async def generate(**kwargs):
            nonlocal final_count
            calls.append(kwargs)
            if kwargs['response_schema'] == EVIDENCE_SCHEMA:
                return evidence_response(kwargs)
            final_count += 1
            if final_count == 1:
                return response('{"summary":"A highly engaged and positive member."}')
            return response(json.dumps(evaluation_data(self.rows)))
        with patch('cogs.message_context.MAX_GEMINI_ROWS_PER_CHUNK', 2), patch('cogs.message_context.generate_ai_response', side_effect=generate):
            with self.assertRaises(PublicEvaluationError):
                await self.invoke()
            self.assertEqual(len(calls), 5)
            parsed, _ = await self.invoke()
            self.assertEqual(len(calls), 6)
            self.assertEqual(parsed['_rubricCoverage']['cachedBatches'], 4)
            self.rows[0]['content'] = 'Updated thoughtful reply.'
            await self.invoke()
            self.assertEqual(len(calls), 8)  # One changed batch and one final call.
        self.assertTrue(all(call['sensitive_content'] for call in calls))
        self.assertTrue(all(call['requested_tier'] == 'fast' for call in calls if call['response_schema'] == EVIDENCE_SCHEMA))

    async def test_low_output_or_input_caps_fail_before_spending(self):
        config = get_ai_config()
        for limits in [replace(config.token_limits, max_output_tokens=600),
                       replace(config.token_limits, max_input_tokens=1000)]:
            with patch('cogs.message_context.get_ai_config', return_value=replace(config, token_limits=limits)), \
                 patch('cogs.message_context.generate_ai_response', new_callable=AsyncMock) as generate:
                with self.assertRaises(PublicEvaluationError):
                    await self.invoke()
                generate.assert_not_awaited()

    async def test_whole_review_budget_precheck_stops_before_first_batch(self):
        with patch('cogs.message_context.can_run_ai_request', new=AsyncMock(return_value=(False, 'daily', .34, 1, .02))), \
             patch('cogs.message_context.generate_ai_response', new_callable=AsyncMock) as generate:
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                await self.invoke()
            generate.assert_not_awaited()

    async def test_same_member_cannot_start_two_paid_reviews_at_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def generate(**kwargs):
            calls.append(kwargs)
            if kwargs['response_schema'] == EVIDENCE_SCHEMA:
                started.set()
                await release.wait()
                return evidence_response(kwargs)
            return response(json.dumps(evaluation_data(self.rows)))
        with patch('cogs.message_context.generate_ai_response', side_effect=generate):
            first = asyncio.create_task(self.invoke())
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                with self.assertRaisesRegex(PublicEvaluationError, 'already running'):
                    await self.invoke()
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await first
        self.assertFalse(self.cog._active_public_evaluations)

    async def test_summary_only_response_is_private_generation_error_not_insufficient_member_evidence(self):
        self.cog._synthesize_structured = AsyncMock(return_value=({'summary': 'A positive member.'}, 'private raw text'))
        self.interaction.followup = SimpleNamespace(send=AsyncMock())
        await self.cog._send_structured_analysis(self.interaction, self.rows, kind='public_user',
            title='Community Evaluation: Alice', task='Evaluate', request='90d', schema={},
            timeframe_text='1184 messages reviewed', total_matching_count=1184,
            target_label='user=123', timeframe_label='90d', source_command='/context user',
            task_type='public_context_user', public_result=True)
        payload = self.interaction.followup.send.await_args
        self.assertTrue(payload.kwargs['ephemeral'])
        self.assertNotIn('embeds', payload.kwargs)
        self.assertIn('incomplete rubric', payload.args[0])
        self.assertNotIn('Insufficient evidence', payload.args[0])
        self.assertNotIn('private raw text', str(payload))

    async def test_full_2500_message_review_stays_within_final_input_budget(self):
        from datetime import datetime, timedelta
        from utils.ai_costs import estimate_tokens_from_text
        self.rows = evaluation_rows(2500)
        start = datetime.fromisoformat(self.rows[0]['timestamp'])
        for index, row in enumerate(self.rows):
            row['message_id'] = str(100000000000000000 + index)
            row['timestamp'] = (start + timedelta(days=index // 28, minutes=index % 28)).isoformat()
            row['content'] = 'Happy to help. How was your day? Welcome to the group! ' * 4
        final_prompts = []
        async def generate(**kwargs):
            if kwargs['response_schema'] == EVIDENCE_SCHEMA:
                ids = re.findall(r'(?m)^TARGET messageId=([^\n]+)', kwargs['prompt'])
                observations = [{'category': category, 'explanation': 'Thoughtful, welcoming replies. ' * 4,
                    'concernType': '', 'evidenceMessageIds': [ids[index % len(ids)], ids[-1]]}
                    for index, category in enumerate(['connection', 'inclusion', 'support', 'initiative', 'conduct'] * 2)]
                return response(json.dumps({'observations': observations, 'quoteMessageIds': ids[:1]}))
            final_prompts.append(kwargs)
            return response(json.dumps(evaluation_data(self.rows[::150])))
        with patch('cogs.message_context.generate_ai_response', side_effect=generate):
            parsed, _ = await self.invoke()
        self.assertEqual(len(parsed['_rubricCoverage']['reviewedIds']), 2500)
        self.assertTrue(parsed['_rubricCoverage']['evidenceCondensed'])
        final = final_prompts[0]
        combined = final['prompt'] + final['system_instruction'] + json.dumps(final['response_schema'])
        self.assertLessEqual(estimate_tokens_from_text(combined), get_ai_config().token_limits.max_input_tokens)


if __name__ == '__main__':
    unittest.main()
