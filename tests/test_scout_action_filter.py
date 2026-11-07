"""Only the acting human moderator's Scout role may authorize automatic logging."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from cogs.scouts import ScoutsCog
from dashboard.rbac import PERMISSION_KEYS
from utils.scouts import Actor, Scouts, DEFAULTS, ACTION_TYPES, CORRELATION_RETENTION_SECONDS


def audit_data(audit_id='100', **changes):
    return dict(dict(action_type='message_delete', event_at=time.time(), audit_id=audit_id,
                     moderator_id='10', scout_roles=['5'], moderator_is_bot=False,
                     confirmed_scout=True, count=1, channel_id='2'), **changes)


class ScoutActionFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Scouts(Path(self.tmp.name) / 'data.db')
        self.s.initialize()
        self.admin = Actor('1', '99', frozenset(PERMISSION_KEYS))
        self.s.save_settings(self.admin, dict(DEFAULTS, enabled=True, automatic_enabled=True,
            scout_roles=['5'], activity_channel='9', monitored_channels=['2']))
        self.channel = SimpleNamespace(id=2, category_id=None, parent_id=None)
        self.guild = MagicMock(id=1)
        self.guild.get_channel_or_thread.return_value = self.channel
        self.guild.fetch_member = AsyncMock(return_value=SimpleNamespace(id=10, bot=False,
            roles=[SimpleNamespace(id=5)]))
        self.bot = MagicMock()
        self.bot.get_guild.return_value = self.guild
        self.bot.user.id = 42
        self.cog = ScoutsCog(self.bot)
        self.cog.service = self.s

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def entry(self, action, audit_id=100):
        timeout = action == discord.AuditLogAction.member_update
        return SimpleNamespace(id=audit_id, guild=self.guild, action=action,
            user=SimpleNamespace(id=10, bot=False), created_at=datetime.now(timezone.utc),
            target=SimpleNamespace(id=20), extra=SimpleNamespace(channel=self.channel, count=1),
            before=SimpleNamespace(timed_out_until=None) if timeout else SimpleNamespace(),
            after=SimpleNamespace(timed_out_until=datetime.now(timezone.utc) + timedelta(hours=1)) if timeout else SimpleNamespace(),
            reason='Moderation reason')

    def records(self):
        return self.s.rows('SELECT * FROM scout_records')

    def jobs(self):
        return self.s.rows('SELECT * FROM scout_jobs')

    async def test_all_supported_actions_require_acting_scout_role(self):
        actions = (discord.AuditLogAction.message_delete, discord.AuditLogAction.message_bulk_delete,
                   discord.AuditLogAction.member_update, discord.AuditLogAction.member_move,
                   discord.AuditLogAction.member_disconnect)
        for i, action in enumerate(actions):
            with self.subTest(action=action):
                # The affected member is a Scout; the acting moderator is not.
                self.guild.fetch_member.return_value = SimpleNamespace(id=10, bot=False,
                    roles=[SimpleNamespace(id=6)])
                await self.cog.ingest_audit(self.guild, self.entry(action, 100 + i))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.jobs(), [])

    async def test_confirmed_scout_actions_log_once_with_non_scout_targets(self):
        for i, action in enumerate((discord.AuditLogAction.message_delete, discord.AuditLogAction.message_bulk_delete,
                discord.AuditLogAction.member_update, discord.AuditLogAction.member_move,
                discord.AuditLogAction.member_disconnect)):
            entry = self.entry(action, 200 + i)
            await self.cog.ingest_audit(self.guild, entry)
            await self.cog.ingest_audit(self.guild, entry)
        self.assertEqual(len(self.records()), len(ACTION_TYPES))
        self.assertEqual(len(self.jobs()), len(ACTION_TYPES))
        self.assertTrue(all(json.loads(r['data'])['moderator_id'] == '10' for r in self.records()))

    async def test_bot_with_scout_role_or_named_human_reason_is_not_logged(self):
        self.guild.fetch_member.return_value = SimpleNamespace(id=10, bot=True,
            roles=[SimpleNamespace(id=5)])
        entry = self.entry(discord.AuditLogAction.message_delete)
        entry.reason = 'Requested by Scout 99'
        await self.cog.ingest_audit(self.guild, entry)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.jobs(), [])

    async def test_missing_actor_and_failed_role_verification_fail_closed(self):
        entry = self.entry(discord.AuditLogAction.message_delete)
        entry.user = None
        await self.cog.ingest_audit(self.guild, entry)
        self.guild.fetch_member.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'No access')
        await self.cog.ingest_audit(self.guild, self.entry(discord.AuditLogAction.member_disconnect, 101))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.jobs(), [])

    async def test_self_deleted_scout_message_and_bulk_events_never_post_raw_cards(self):
        self.s.buffer('1', dict(message_id='30', channel_id='2', author_id='10', content='A Scout wrote this', attachments=[]))
        await self.cog.deleted(1, 2, [30])
        await self.cog.deleted(1, 2, [30, 31], bulk=True)
        self.s.reconcile_links('1')
        self.assertEqual(self.records(), [])
        self.assertEqual(self.jobs(), [])
        observation = json.loads(self.s.rows('SELECT data FROM scout_observations')[0]['data'])
        self.assertEqual(observation['messages'][0]['content'], 'A Scout wrote this')
        self.assertNotIn('moderator_id', observation)

    async def test_delayed_scout_audit_does_not_publish_unrelated_self_deleted_content(self):
        self.s.buffer('1', dict(message_id='30', channel_id='2', author_id='20', content='SELF-DELETED CONTENT', attachments=[]))
        await self.cog.deleted(1, 2, [30])
        await self.cog.ingest_audit(self.guild, self.entry(discord.AuditLogAction.message_delete))
        self.s.reconcile_links('1')
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.jobs()), 1)
        self.assertNotIn('SELF-DELETED CONTENT', self.records()[0]['data'])
        self.assertNotIn('SELF-DELETED CONTENT', json.dumps(self.jobs()))

    async def test_role_removal_before_verification_does_not_reuse_cached_scout_membership(self):
        entry = self.entry(discord.AuditLogAction.member_disconnect)
        await self.cog.ingest_audit(self.guild, entry)
        self.guild.fetch_member.return_value = SimpleNamespace(id=10, bot=False, roles=[])
        entry.id = 101
        await self.cog.ingest_audit(self.guild, entry)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.guild.fetch_member.await_count, 2)

    async def test_shared_storage_rejects_wrong_roles_and_forged_gateway_confirmation(self):
        for data, key in ((audit_data(scout_roles=['6']), 'audit:100'),
                          (audit_data(moderator_is_bot=True), 'audit:100'),
                          (audit_data(confirmed_scout=False), 'audit:100'),
                          (audit_data(), 'gateway:delete')):
            self.assertIsNone(self.s.system('1', key, data))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.jobs(), [])

    async def test_observations_expire_and_never_appear_in_dashboard_or_ai_sources(self):
        self.s.system('1', 'gateway:delete', dict(action_type='message_delete', event_at=time.time(), message_ids=['30']))
        with self.s.connect() as c:
            c.execute('UPDATE scout_observations SET observed_at=?', (time.time() - CORRELATION_RETENTION_SECONDS - 1,))
        self.assertEqual(self.s.listing(self.admin, 'system'), [])
        self.assertEqual(self.s.ai_sources(self.admin, '10'), [])
        self.s.prune('1')
        self.assertEqual(self.s.rows('SELECT * FROM scout_observations'), [])

    async def test_pending_legacy_non_scout_deliveries_are_cancelled_without_posting(self):
        for i, data in enumerate((dict(action_type='message_delete'), audit_data(confirmed_scout=False))):
            with self.s.connect() as c:
                record = self.s._create(c, '1', 'system', '', data, source='gateway:' + str(i), notify=False)
                c.execute('INSERT INTO scout_jobs(id,guild_id,record_id,version,purpose,destination,created_at) VALUES(?,?,?,?,?,?,?)',
                    (str(i), '1', record['id'], 1, 'card', '9', time.time()))
            job = self.jobs()[-1]
            await self.cog.deliver(job)
        self.assertTrue(all(j['status'] == 'cancelled' for j in self.jobs()))
        self.guild.get_channel.assert_not_called()
        self.assertEqual(self.s.listing(self.admin, 'system'), [])
        self.assertEqual(self.s.ai_sources(self.admin, '10'), [])
        # Preserve original historical records instead of deleting evidence.
        self.assertEqual(len(self.records()), 2)

    async def test_confirmed_scout_delivery_still_posts_and_automatic_toggle_pauses_it(self):
        record = self.s.system('1', 'audit:100', audit_data())
        destination = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=500)))
        self.guild.get_channel.return_value = destination
        job = self.jobs()[0]
        self.s.save_settings(self.admin, dict(self.s.settings('1'), automatic_enabled=False))
        await self.cog.deliver(job)
        destination.send.assert_not_awaited()
        self.s.save_settings(self.admin, dict(self.s.settings('1'), automatic_enabled=True))
        await self.cog.deliver(job)
        destination.send.assert_awaited_once()
        self.assertEqual(self.jobs()[0]['status'], 'sent')
