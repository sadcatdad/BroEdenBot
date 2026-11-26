import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import aiosqlite
import discord

from cogs.message_context import MessageContext
from utils.context_commands import COMMAND_USAGE_SQL, COMMAND_USAGE_INDEX_SQL, command_name, public_command_response
from utils.message_context import MESSAGE_CONTEXT_TABLE_SQL


class CommandMetadataTests(unittest.TestCase):
    def test_command_paths_never_contain_argument_values(self):
        data = {'name': 'context', 'options': [{'type': 1, 'name': 'user', 'options': [
            {'type': 6, 'name': 'user', 'value': 'SECRET-MEMBER'},
            {'type': 3, 'name': 'question', 'value': 'SECRET-TEXT'}]}]}
        self.assertEqual(command_name(data), 'context user')
        self.assertNotIn('SECRET', command_name(data))

    def test_new_metadata_without_command_name_is_not_assumed_rank(self):
        message = SimpleNamespace(author=SimpleNamespace(id=88, bot=True), content='', embeds=[],
            interaction=None, interaction_metadata=SimpleNamespace(id=100, type=discord.InteractionType.application_command,
            user=SimpleNamespace(id=123, bot=False)))
        self.assertEqual(public_command_response(message, {88})[2:], ('unknown', 'unknown'))
        message.content = 'Rank 42 | Level 4'
        self.assertEqual(public_command_response(message, set())[2:], ('unknown', 'unknown'))
        self.assertEqual(public_command_response(message, {88})[2:], ('rank', 'response_inferred'))
        message.flags = SimpleNamespace(ephemeral=True)
        self.assertIsNone(public_command_response(message, {88}))
        message.flags.ephemeral = False
        message.author.bot = False
        self.assertIsNone(public_command_response(message, {88}))


class CommandArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.member = SimpleNamespace(id=123, bot=False, roles=[])
        self.guild = SimpleNamespace(id=42, get_member=lambda uid: self.member if uid == 123 else None)
        self.channel = SimpleNamespace(id=99, parent=None)
        self.cog = MessageContext(SimpleNamespace(user=SimpleNamespace(id=1), intents=SimpleNamespace(message_content=True)))
        self.cog.enabled = True
        self.cog.db = await aiosqlite.connect(':memory:')
        self.cog.db.row_factory = aiosqlite.Row
        self.addAsyncCleanup(self.cog.db.close)
        for sql in [MESSAGE_CONTEXT_TABLE_SQL, COMMAND_USAGE_SQL, COMMAND_USAGE_INDEX_SQL]:
            await self.cog.db.execute(sql)
        self.cog.nsfw_access_role_ids = {5}
        self.cog.rank_bot_ids = {88}
        self.cog._query_excluded_channel_ids = Mock(return_value=set())
        self.stamp = datetime(2026, 11, 1, tzinfo=timezone.utc)

    def response(self, iid=100, name='rank', modern=False, channel=None):
        metadata = SimpleNamespace(id=iid, name=name, type=discord.InteractionType.application_command, user=self.member)
        return SimpleNamespace(guild=self.guild, channel=channel or self.channel, id=iid+1000,
            author=SimpleNamespace(id=88, bot=True), created_at=self.stamp, content='Rank 1 | Level 4',
            embeds=[], attachments=[], stickers=[], webhook_id=None, application_id=88,
            interaction=None if modern else metadata, interaction_metadata=metadata)

    async def rows(self):
        return await self.cog._fetchall('SELECT * FROM message_context_command_usage ORDER BY timestamp', ())

    async def test_public_responses_are_captured_even_when_bot_messages_are_ignored(self):
        self.cog.ignore_bots = True
        message = self.response()
        await self.cog.on_message(message)
        await self.cog.on_message(message)  # Duplicate gateway delivery/follow-up.
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['command_name'], 'rank')
        self.assertEqual(rows[0]['confidence'], 'exact')
        self.assertEqual(rows[0]['nsfw_access'], 0)
        self.assertEqual(await self.cog._fetchall('SELECT * FROM message_context_messages', ()), [])
        self.assertNotIn('Level 4', str(dict(rows[0])))

    async def test_role_snapshot_is_not_rewritten_by_later_role_changes(self):
        message = self.response()
        await self.cog._observe_command_response(message)
        self.member.roles = [SimpleNamespace(id=5)]
        await self.cog._observe_command_response(message)
        self.assertEqual((await self.rows())[0]['nsfw_access'], 0)
        await self.cog._observe_command_response(self.response(iid=101))
        self.assertEqual((await self.rows())[1]['nsfw_access'], 1)

    async def test_loading_response_can_be_enriched_without_duplicate_or_new_snapshot(self):
        message = self.response(modern=True)
        message.content = 'Thinking…'
        await self.cog._observe_command_response(message)
        self.assertEqual((await self.rows())[0]['command_name'], 'unknown')
        self.member.roles = [SimpleNamespace(id=5)]
        message.content = 'Rank 20 | Level 4'
        await self.cog.on_message_edit(message, message)
        rows = await self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['confidence'], 'response_inferred')
        self.assertEqual(rows[0]['nsfw_access'], 0)

    async def test_own_commands_are_private_metadata_not_public_usage(self):
        interaction = SimpleNamespace(type=discord.InteractionType.application_command, guild=self.guild,
            channel=self.channel, user=self.member, application_id=1, id=12, created_at=self.stamp,
            data={'name': 'staffai', 'options': [{'type': 3, 'name': 'query', 'value': 'PRIVATE'}]})
        await self.cog.on_interaction(interaction)
        rows = await self.rows()
        self.assertEqual(rows[0]['command_name'], 'staffai')
        self.assertNotIn('PRIVATE', str(dict(rows[0])))
        summary = await self.cog._command_usage_summary(42, 123, after='2026-10-01', before='2026-12-01')
        self.assertNotIn('staffai', summary)
        self.assertIn('No public', summary)

    async def test_rank_summary_describes_frequency_without_claiming_level_or_intent(self):
        for iid in range(100, 106):
            await self.cog._observe_command_response(self.response(iid=iid))
        summary = await self.cog._command_usage_summary(42, 123, after='2026-10-01', before='2026-12-01')
        self.assertIn('Rank checks: 6', summary)
        self.assertIn('Busiest 15 minutes: 6', summary)
        self.assertIn('6 rank checks occurred without', summary)
        self.assertIn('no score deduction', summary)
        self.assertNotIn('below level', summary)
        self.assertNotIn('farming', summary)

    async def test_disabled_tracking_exclusions_guild_and_channel_scopes_are_respected(self):
        self.cog.enabled = False
        await self.cog._observe_command_response(self.response())
        self.assertEqual(await self.rows(), [])
        self.cog.enabled = True
        thread = SimpleNamespace(id=100, parent=SimpleNamespace(id=99))
        await self.cog._observe_command_response(self.response(channel=thread))
        self.cog._query_excluded_channel_ids.return_value = {99}
        await self.cog._observe_command_response(self.response(iid=101))
        self.assertEqual(len(await self.rows()), 1)
        summary = await self.cog._command_usage_summary(42, 123, after='2026-10-01', before='2026-12-01')
        self.assertIn('No public', summary)
        self.cog._query_excluded_channel_ids.return_value = set()
        summary = await self.cog._command_usage_summary(42, 123, after='2026-10-01', before='2026-12-01', channel_id=99)
        self.assertIn('Rank checks: 1', summary)
        for gid, uid in [(77, 123), (42, 456)]:
            summary = await self.cog._command_usage_summary(gid, uid, after='2026-10-01', before='2026-12-01')
            self.assertIn('No public', summary)

    async def test_command_observations_follow_archive_retention(self):
        await self.cog._observe_command_response(self.response())
        self.cog.retention_days = 1
        await self.cog._prune()
        self.assertEqual(await self.rows(), [])


if __name__ == '__main__':
    unittest.main()
