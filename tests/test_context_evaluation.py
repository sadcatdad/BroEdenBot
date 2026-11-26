import asyncio
import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

from cogs.message_context import MessageContext, PUBLIC_USER_EVALUATION_SCHEMA, PUBLIC_EVALUATION_SYSTEM_INSTRUCTION
from utils.message_context import MESSAGE_CONTEXT_TABLE_SQL, MESSAGE_CONTEXT_INDEX_SQL
from utils.context_commands import COMMAND_USAGE_SQL, COMMAND_USAGE_INDEX_SQL
from tests.community_fixtures import evaluation_rows, evaluation_data
from utils.public_evaluation import EVIDENCE_SCHEMA, PublicEvaluationError


class EvaluationArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cog = MessageContext(SimpleNamespace())
        self.cog.db = await aiosqlite.connect(':memory:')
        self.cog.db.row_factory = aiosqlite.Row
        self.addAsyncCleanup(self.cog.db.close)
        await self.cog.db.execute(MESSAGE_CONTEXT_TABLE_SQL)
        for sql in MESSAGE_CONTEXT_INDEX_SQL:
            await self.cog.db.execute(sql)
        await self.cog.db.execute(COMMAND_USAGE_SQL)
        await self.cog.db.execute(COMMAND_USAGE_INDEX_SQL)
        self.cog._query_excluded_channel_ids = Mock(return_value={999})
        self.after = '2026-08-01T00:00:00+00:00'
        self.before = '2026-11-01T00:00:00+00:00'

    async def insert(self, mid, stamp, channel='99', author='123', guild='42', **extra):
        values = dict(guild_id=guild, channel_id=channel, channel_name='general',
                      message_id=mid, author_id=author, author_name='alice', author_display_name='Alice',
                      timestamp=stamp, content='I can help with that.', stored_at=stamp)
        values.update(extra)
        await self.cog.db.execute(f"INSERT INTO message_context_messages ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))

    async def test_sampling_covers_90_days_and_channels_not_just_latest_messages(self):
        start = datetime.fromisoformat(self.after)
        for day in range(90):
            for message in range(31):
                await self.insert(f'{day}-{message}', (start+timedelta(days=day, minutes=message)).isoformat(),
                                  channel='100' if message == 30 else '99')
        for mid, extra in [('deleted', {'is_deleted': 1}), ('bot', {'is_bot': 1}),
                           ('webhook', {'is_webhook': 1}), ('excluded', {'channel': '999'}),
                           ('thread', {'parent_channel_id': '999'}), ('other-user', {'author': '456'}),
                           ('other-guild', {'guild': '43'}), ('unknown', {'author_name': 'Deleted User'})]:
            await self.insert(mid, self.after, **extra)
        await self.cog.db.commit()
        rows, total = await self.cog._evaluation_rows(42, user_id=123, after=self.after,
                        before=self.before, channel_id=None, max_messages=60)
        self.assertEqual(total, 2790)
        self.assertEqual(len(rows), 60)
        self.assertEqual({row['channel_id'] for row in rows}, {'99', '100'})
        stamps = [datetime.fromisoformat(row['timestamp']) for row in rows]
        self.assertLess((min(stamps)-start).days, 7)
        self.assertGreater((max(stamps)-start).days, 82)
        rows2, _ = await self.cog._evaluation_rows(42, user_id=123, after=self.after,
                        before=self.before, channel_id=None, max_messages=60)
        self.assertEqual([r['message_id'] for r in rows], [r['message_id'] for r in rows2])

    async def test_nearby_context_keeps_guild_channel_time_and_exclusion_boundaries(self):
        for mid, stamp, extra in [
            ('target', '2026-09-01T12:00:00+00:00', {}),
            ('reply', '2026-09-01T12:01:00+00:00', {'author': '456'}),
            ('other-guild', '2026-09-01T12:01:00+00:00', {'guild': '77'}),
            ('other-channel', '2026-09-01T12:01:00+00:00', {'channel': '100'}),
            ('too-late', '2026-09-01T12:10:00+00:00', {}),
            ('deleted', '2026-09-01T12:01:00+00:00', {'is_deleted': 1}),
        ]:
            await self.insert(mid, stamp, **extra)
        await self.cog.db.commit()
        rows, _ = await self.cog._evaluation_rows(42, user_id=123, after=self.after,
                        before=self.before, channel_id=99, max_messages=1)
        target = await self.cog._fetchall("SELECT * FROM message_context_messages WHERE message_id='target'", ())
        nearby = await self.cog._evaluation_nearby(42, target, after=self.after, before=self.before)
        self.assertEqual([r['message_id'] for r in nearby['target']], ['reply'])
        self.cog._query_excluded_channel_ids.return_value = {99}
        self.assertEqual(await self.cog._evaluation_nearby(42, target, after=self.after, before=self.before), {})

    async def test_default_review_uses_2500_messages_and_90_days(self):
        self.cog._deny = AsyncMock(return_value=False)
        self.cog._staff_ai_cooldown = AsyncMock(return_value=False)
        self.cog._evaluation_rows = AsyncMock(return_value=([], 0))
        self.cog._evaluation_nearby = AsyncMock(return_value={})
        self.cog._command_usage_summary = AsyncMock(return_value='No observed commands.')
        self.cog._send_structured_analysis = AsyncMock()
        interaction = SimpleNamespace(guild_id=42, response=SimpleNamespace(defer=AsyncMock()))
        await MessageContext.context_user.callback(self.cog, interaction, user=SimpleNamespace(id=123, display_name='Alice'))
        query = self.cog._evaluation_rows.await_args.kwargs
        self.assertEqual(query['max_messages'], 2500)
        self.assertGreater((datetime.fromisoformat(query['before'])-datetime.fromisoformat(query['after'])).days, 89)
        max_option = next(p for p in MessageContext.context_user.parameters if p.name == 'max_messages')
        self.assertEqual(max_option.max_value, 5000)


class EvaluationSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_batches_report_exact_coverage_and_bound_concurrency(self):
        cog = MessageContext(SimpleNamespace())
        rows = evaluation_rows(8)
        active = peak = 0

        async def generate(**kwargs):
            nonlocal active, peak
            if kwargs.get('response_schema') == PUBLIC_USER_EVALUATION_SCHEMA:
                return SimpleNamespace(ok=True, blocked_by_budget=False, text=json.dumps(evaluation_data(rows)), model_used='test')
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            if 'Evidence batch 2/' in kwargs['prompt']:
                raise RuntimeError('One batch failed')
            ids = re.findall(r'(?m)^TARGET messageId=([^\n]+)', kwargs['prompt'])
            evidence = {'observations': [{'category': 'connection', 'explanation': 'Welcoming replies.',
                        'evidenceMessageIds': ids[:2], 'concernType': ''}], 'quoteMessageIds': ids[:1]}
            return SimpleNamespace(ok=True, blocked_by_budget=False, text=json.dumps(evidence), error=None)
        interaction = SimpleNamespace(user=SimpleNamespace(id=1), guild_id=42, channel_id=99)
        with patch('cogs.message_context.MAX_GEMINI_ROWS_PER_CHUNK', 2), patch('cogs.message_context.generate_ai_response', side_effect=generate):
            parsed, _ = await cog._synthesize_structured(rows, task='Rubric', request='90d',
                schema=PUBLIC_USER_EVALUATION_SCHEMA, include_links=True, interaction=interaction,
                source_command='/context user', task_type='public_context_user', public_result=True,
                nearby={}, system_instruction=PUBLIC_EVALUATION_SYSTEM_INSTRUCTION)
        self.assertLessEqual(peak, 3)
        self.assertGreater(peak, 1)
        self.assertEqual(parsed['_rubricCoverage']['failedChunks'], 1)
        self.assertEqual(set(parsed['_rubricCoverage']['reviewedIds']), {'1000', '1001', '1004', '1005', '1006', '1007'})


if __name__ == '__main__':
    unittest.main()
