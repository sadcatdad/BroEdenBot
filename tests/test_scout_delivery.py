import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from cogs.scouts import ScoutsCog
from utils.scouts import Scouts, Actor, DEFAULTS
from dashboard.rbac import PERMISSION_KEYS


class ScoutDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Scouts(Path(self.tmp.name)/'data.db')
        self.s.initialize()
        self.admin=Actor('1','10',frozenset(PERMISSION_KEYS))
        self.s.save_settings(self.admin,dict(DEFAULTS,enabled=True,scout_roles=['5'],activity_channel='9'))
        self.record=self.s.manual(self.admin,dict(type='other',summary='Context',action='Reminder'))
        self.channel=MagicMock()
        self.channel.send=AsyncMock(return_value=SimpleNamespace(id=100))
        self.channel.fetch_message=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock(),id=100))
        self.guild=MagicMock()
        self.guild.get_channel.return_value=self.channel
        self.bot=MagicMock()
        self.bot.get_guild.return_value=self.guild
        self.bot.user.id=42
        self.cog=ScoutsCog(self.bot)
        self.cog.service=self.s

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def job(self):
        return self.s.rows('SELECT * FROM scout_jobs ORDER BY created_at DESC')[0]

    async def test_successful_send_persists_receipt_and_edited_card(self):
        await self.cog.deliver(self.job())
        self.assertEqual((self.job()['status'],self.job()['message_id']),('sent','100'))
        self.s.edit(self.admin,self.record['id'],dict(type='other',summary='Edited',action='Reminder'),1)
        await self.cog.deliver(self.job())
        self.channel.send.assert_awaited_once()
        self.channel.fetch_message.assert_awaited_once_with(100)
        self.channel.fetch_message.return_value.edit.assert_awaited_once()

    async def test_ambiguous_send_is_not_resent_and_blocks_later_revision(self):
        self.channel.send.side_effect=TimeoutError()
        first=self.job()
        await self.cog.deliver(first)
        self.assertEqual(self.job()['status'],'uncertain')
        self.s.edit(self.admin,self.record['id'],dict(type='other',summary='Edited',action='Reminder'),1)
        await self.cog.deliver(self.job())
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.job()['status'],'pending')

    async def test_uncertain_receipt_reconciled_after_restart_without_new_send(self):
        first=self.job()
        marker='Scout delivery '+first['id']
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='uncertain',attempted_at=?",(time.time(),))
        found=SimpleNamespace(id=100,author=SimpleNamespace(id=42),embeds=[SimpleNamespace(footer=SimpleNamespace(text=marker))])
        async def history(*args,**kw):
            yield found
        self.channel.history=history
        restarted=ScoutsCog(self.bot)
        restarted.service=Scouts(self.s.path)
        await restarted.deliver(self.job())
        self.assertEqual(self.job()['status'],'sent')
        self.channel.send.assert_not_awaited()

    async def test_missing_history_does_not_trigger_a_retry(self):
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='uncertain',attempted_at=?",(time.time(),))
        async def history(*args,**kw):
            if False:yield None
        self.channel.history=history
        await self.cog.deliver(self.job())
        self.assertEqual(self.job()['status'],'uncertain')
        self.channel.send.assert_not_awaited()

    async def test_compact_card_receipt_recovers_the_correct_send_after_restart(self):
        from cogs.scouts import card_embed
        marker = 'Scout delivery ' + self.job()['id']
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='uncertain',attempted_at=?", (time.time(),))
        # Cards for the same record have identical human-readable footers. Only
        # the delivery receipt in the title link identifies this particular send.
        decoy = SimpleNamespace(id=99, author=SimpleNamespace(id=42),
            embeds=[card_embed(self.record, 'Scout delivery another-attempt')])
        found = SimpleNamespace(id=100, author=SimpleNamespace(id=42),
            embeds=[card_embed(self.record, marker)])
        async def history(*args, **kwargs):
            yield decoy
            yield found
        self.channel.history = history
        restarted = ScoutsCog(self.bot)
        restarted.service = Scouts(self.s.path)
        await restarted.deliver(self.job())
        self.assertEqual((self.job()['status'], self.job()['message_id']), ('sent', '100'))
        self.channel.send.assert_not_awaited()

    async def test_permission_denied_is_definite_failure_and_can_retry(self):
        self.channel.send.side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'), 'No access')
        await self.cog.deliver(self.job())
        self.assertEqual(self.job()['status'],'failed')
        self.s.retry_delivery(self.admin,self.job()['id'])
        self.channel.send.side_effect=None
        await self.cog.deliver(self.job())
        self.assertEqual(self.job()['status'],'sent')

    async def test_cancelled_before_delivery_never_sends_new_dm(self):
        checkin=self.s.request_checkins(self.admin,['11'])[0]
        self.s.checkin_action(self.admin,checkin['id'],'cancel')
        jobs=self.s.rows('SELECT * FROM scout_jobs WHERE record_id=?',(checkin['id'],))
        for job in jobs:
            await self.cog.deliver(job)
        self.guild.fetch_member.assert_not_called()
        self.assertTrue(all(j['status']=='cancelled' for j in self.s.rows('SELECT * FROM scout_jobs WHERE record_id=?',(checkin['id'],))))

    async def test_closed_dm_failure_and_partial_bulk_delivery(self):
        requests=self.s.request_checkins(self.admin,['11','12'])
        eligible=Actor('1','11',frozenset({'scouts.checkins.respond'}))
        self.cog.actor=AsyncMock(return_value=eligible)
        closed=SimpleNamespace(create_dm=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'Closed')))
        open_member=SimpleNamespace(create_dm=AsyncMock(return_value=self.channel))
        self.guild.fetch_member=AsyncMock(side_effect=[closed,open_member])
        jobs=self.s.rows('SELECT * FROM scout_jobs WHERE record_id IN (?,?) ORDER BY created_at',(requests[0]['id'],requests[1]['id']))
        for job in jobs:
            await self.cog.deliver(job)
        statuses=[self.s.rows('SELECT status FROM scout_jobs WHERE id=?',(j['id'],))[0]['status'] for j in jobs]
        self.assertEqual(statuses,['failed','sent'])
        self.guild.get_channel.assert_not_called()

    async def test_missing_original_card_is_recreated_after_discord_confirms_absence(self):
        await self.cog.deliver(self.job())
        self.s.edit(self.admin,self.record['id'],dict(type='other',summary='Edit',action='Reminder'),1)
        self.channel.fetch_message.side_effect=discord.NotFound(SimpleNamespace(status=404,reason='Not Found'),'Missing')
        await self.cog.deliver(self.job())
        self.assertEqual(self.channel.send.await_count,2)
        self.assertEqual(self.job()['status'],'sent')

    async def test_cancel_waits_for_uncertain_initial_dm_receipt(self):
        r=self.s.request_checkins(self.admin,['11'])[0]
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='uncertain',attempted_at=? WHERE record_id=?",(time.time(),r['id']))
        self.s.checkin_action(self.admin,r['id'],'cancel')
        later=self.job()
        await self.cog.deliver(later)
        self.assertEqual(self.job()['status'],'pending')
        self.guild.fetch_member.assert_not_called()
