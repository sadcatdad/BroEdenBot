"""Exercise Scout log inputs through the real service, form and delivery adapter."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.scouts import ScoutsCog, ScoutModal, DeliveryStatus
from dashboard.rbac import PERMISSION_KEYS
from utils.scouts import Actor, Scouts, DEFAULTS

MESSAGE = 'https://discord.com/channels/1/2/300'


class ScoutLogWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Scouts(Path(self.tmp.name) / 'test.db')
        self.s.initialize()
        self.actor = Actor('1', '10', frozenset(PERMISSION_KEYS))
        # Manual activity must work while automatic moderation capture is off.
        self.s.save_settings(self.actor, dict(DEFAULTS, enabled=True, automatic_enabled=False,
            scout_roles=['5'], activity_channel='9', report_roles=['5'], escalation_roles=['6']))
        self.destination = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=500)))
        self.guild = MagicMock()
        self.guild.get_channel.return_value = self.destination
        self.bot = MagicMock()
        self.bot.get_guild.return_value = self.guild
        self.cog = ScoutsCog(self.bot)
        self.cog.service = self.s
        self.cog.actor = AsyncMock(return_value=self.actor)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def interaction(self, uid=10, iid=100):
        return SimpleNamespace(id=iid, user=SimpleNamespace(id=uid), guild_id=1,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())

    def values(self):
        return dict(type='rule reminder', summary='What happened', action='Gave a reminder',
                    members='jojo', channel='2', incident_at='3:30am', outcome='They apologized')

    def records(self):
        return self.s.rows('SELECT * FROM scout_records')

    def jobs(self):
        return self.s.rows('SELECT * FROM scout_jobs ORDER BY created_at')

    async def submit_form(self, related=MESSAGE, iid=100):
        modal = ScoutModal(self.cog, '1', 'manual', values=self.values(), related=related)
        interaction = self.interaction(iid=iid)
        await modal.on_submit(interaction)
        return modal, interaction

    async def test_screenshot_message_link_saves_and_posts_to_staff_channel(self):
        modal, interaction = await self.submit_form()
        self.assertEqual(len(self.records()), 1)
        record = self.records()[0]
        data = json.loads(record['data'])
        self.assertEqual(data['evidence'], MESSAGE)
        self.assertNotIn('related', data)
        job = self.jobs()[0]
        self.assertEqual((job['status'], job['destination']), ('pending', '9'))
        self.assertIn('<#9>', interaction.followup.send.call_args.args[0])
        await self.cog.deliver(job)
        self.guild.get_channel.assert_called_once_with(9)
        embed = self.destination.send.call_args.kwargs['embed']
        for field in ('jojo', '3:30am', MESSAGE, '<#2>', 'They apologized'):
            self.assertIn(field, embed.description)
        self.assertEqual(self.jobs()[0]['status'], 'sent')
        self.assertFalse(self.s.settings('1')['automatic_enabled'])

    async def test_scout_card_link_resolves_to_its_internal_record(self):
        parent = self.s.manual(self.actor, self.values())
        await self.cog.deliver(self.jobs()[0])
        child = self.s.manual(self.actor, self.values(), 'https://discord.com/channels/1/9/500')
        self.assertEqual(child['data']['related'], parent['id'])
        self.assertEqual(child['data']['evidence'], 'https://discord.com/channels/1/9/500')

    async def test_receipt_resolution_checks_channel_and_guild_together(self):
        parent = self.s.manual(self.actor, self.values())
        await self.cog.deliver(self.jobs()[0])
        ordinary = self.s.resolve_reference(self.actor, 'https://discord.com/channels/1/8/500')
        self.assertEqual(ordinary['record_id'], '')
        with self.assertRaisesRegex(ValueError, 'another server'):
            self.s.resolve_reference(self.actor, 'https://discord.com/channels/2/9/500')
        with self.assertRaises(ValueError):
            self.s.resolve_reference(Actor('2','10',self.actor.permissions), parent['id'])

    async def test_garden_links_and_displayed_record_ids_are_supported(self):
        parent = self.s.manual(self.actor, self.values())
        with patch.dict(os.environ, {'DASHBOARD_PUBLIC_URL':'https://garden.example.test'}):
            for value in ('#' + parent['id'], '/scouts/records/' + parent['id'],
                    'https://garden.example.test/scouts/records/' + parent['id'] + '#scout-delivery=test'):
                self.assertEqual(self.s.resolve_reference(self.actor, value)['record_id'], parent['id'])
            with self.assertRaises(ValueError):
                self.s.resolve_reference(self.actor, 'https://untrusted.example/scouts/records/' + parent['id'])

    async def test_unknown_link_cannot_bypass_parent_permissions(self):
        with self.s.connect() as c:
            parent = self.s._create(c,'1','system','20',dict(action_type='timeout'),source='audit:100',notify=False)
            c.execute('INSERT INTO scout_jobs(id,guild_id,record_id,version,destination,message_id,status,created_at) VALUES(?,?,?,?,?,?,?,?)',
                ('parent','1',parent['id'],1,'9','500','sent',time.time()))
        restricted = Actor('1','10',self.actor.permissions - {'scouts.moderation.view'})
        with self.assertRaises(PermissionError):
            self.s.manual(restricted,self.values(),'https://discord.com/channels/1/9/500')
        self.assertEqual(len(self.records()),1)

    async def test_private_checkin_cannot_be_linked_into_public_activity(self):
        checkin = self.s.request_checkins(self.actor,['10'])[0]
        with self.assertRaisesRegex(ValueError,'Private check-ins'):
            self.s.manual(self.actor,self.values(),checkin['id'])

    async def test_invalid_related_reference_is_rejected_before_opening_form(self):
        interaction = self.interaction()
        await self.cog.log_activity.callback(self.cog,interaction,related_record='not-a-record')
        self.assertNotIn('view',interaction.followup.send.call_args.kwargs)
        self.assertIn('record ID',interaction.followup.send.call_args.args[0])
        self.assertEqual(self.records(),[])
        self.assertEqual(self.jobs(),[])

    async def test_command_prefills_message_evidence_and_explains_actual_destination(self):
        interaction = self.interaction()
        await self.cog.log_activity.callback(self.cog,interaction,activity_type='rule reminder',
            members='jojo',channel=SimpleNamespace(id=2),incident_time='3:30am',related_record=MESSAGE)
        args = interaction.followup.send.call_args
        self.assertIn('<#9>',args.args[0])
        self.assertIn('prefilled as evidence',args.args[0])
        modal = args.kwargs['view'].factory()
        self.assertEqual(modal.inputs['evidence'].default,MESSAGE)
        self.assertEqual(modal.inputs['related'].default,MESSAGE)
        modal.inputs['summary']._value = 'What happened'
        modal.inputs['action']._value = 'Gave a reminder'
        await modal.on_submit(self.interaction(iid=101))
        self.assertEqual(len(self.records()),1)

    async def test_failed_save_keeps_draft_and_allows_correcting_reference(self):
        modal, interaction = await self.submit_form(related='bad-reference')
        self.assertEqual(self.records(),[])
        response = interaction.followup.send.call_args
        self.assertIn('draft is preserved',response.args[0])
        retry = response.kwargs['view'].factory()
        self.assertEqual(retry.inputs['summary'].default,'What happened')
        self.assertEqual(retry.inputs['action'].default,'Gave a reminder')
        self.assertEqual(retry.values['members'],'jojo')
        retry.inputs['related']._value = MESSAGE
        await retry.on_submit(self.interaction(iid=102))
        self.assertEqual(len(self.records()),1)
        self.assertEqual(len(self.jobs()),1)

    async def test_ambiguous_confirmation_retry_cannot_create_duplicate_activity(self):
        with patch.object(self.cog,'send_saved',AsyncMock(side_effect=RuntimeError('Reply lost'))):
            modal, interaction = await self.submit_form()
        self.assertEqual(len(self.records()),1)
        retry = interaction.followup.send.call_args.kwargs['view'].factory()
        await retry.on_submit(self.interaction(iid=103))
        self.assertEqual(len(self.records()),1)
        self.assertEqual(len(self.jobs()),1)

    async def test_delivery_receipt_shows_failure_and_later_success(self):
        modal, interaction = await self.submit_form()
        view = interaction.followup.send.call_args.kwargs['view']
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='failed',error='Missing Send Messages permission.'")
        check = self.interaction(iid=104)
        await view.check.callback(check)
        text = check.edit_original_response.call_args.kwargs['content']
        self.assertIn('Missing Send Messages permission',text)
        self.assertIn('saved record is retained',text)
        self.s.retry_delivery(self.actor,self.jobs()[0]['id'])
        await self.cog.deliver(self.jobs()[0])
        await view.check.callback(check)
        self.assertIn('https://discord.com/channels/1/9/500',check.edit_original_response.call_args.kwargs['content'])

    async def test_delivery_status_requires_same_user_and_current_permissions(self):
        modal, interaction = await self.submit_form()
        view = interaction.followup.send.call_args.kwargs['view']
        other = self.interaction(uid=11)
        await view.check.callback(other)
        other.response.send_message.assert_awaited_once()
        other.edit_original_response.assert_not_awaited()
        self.cog.actor.return_value = Actor('1','10',frozenset())
        revoked = self.interaction()
        await view.check.callback(revoked)
        revoked.edit_original_response.assert_not_awaited()
        revoked.followup.send.assert_awaited_once()

    async def test_edit_and_escalation_accept_copied_scout_card_links(self):
        parent = self.s.manual(self.actor,self.values())
        await self.cog.deliver(self.jobs()[0])
        interaction = self.interaction()
        await self.cog.edit_activity.callback(self.cog,interaction,record='https://discord.com/channels/1/9/500')
        self.assertEqual(interaction.followup.send.call_args.kwargs['view'].factory().rid,parent['id'])
        escalation = self.s.escalate(self.actor,'Need a Ranger',MESSAGE)
        self.assertEqual(escalation['data']['evidence'],MESSAGE)
        await self.cog.deliver(self.jobs()[-1])
        sent = self.destination.send.call_args
        self.assertEqual(sent.args[0],'<@&6>')
        self.assertIn(MESSAGE,sent.kwargs['embed'].description)

    async def test_slow_audit_reconciliation_cannot_block_saved_manual_delivery(self):
        self.guild.id = 1
        self.s.save_settings(self.actor,dict(self.s.settings('1'),automatic_enabled=True))
        record = self.s.manual(self.actor,self.values(),MESSAGE)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_audit(*args):
            entered.set()
            await release.wait()
        self.cog.audit_poll = slow_audit
        self.cog.heartbeat = AsyncMock()
        auditing = asyncio.create_task(self.cog.audit_worker.coro(self.cog))
        try:
            await asyncio.wait_for(entered.wait(),2)
            await asyncio.wait_for(self.cog.worker.coro(self.cog),2)
            self.assertFalse(auditing.done())
            self.assertEqual(self.jobs()[0]['status'],'sent')
            self.destination.send.assert_awaited_once()
        finally:
            release.set()
            await auditing

    async def test_both_background_workers_start_and_stop_with_the_cog(self):
        with patch.object(self.cog.worker,'start') as delivery_start, \
             patch.object(self.cog.audit_worker,'start') as audit_start, \
             patch.object(self.cog.worker,'cancel') as delivery_cancel, \
             patch.object(self.cog.audit_worker,'cancel') as audit_cancel:
            await self.cog.cog_load()
            delivery_start.assert_called_once()
            audit_start.assert_called_once()
            self.cog.cog_unload()
            delivery_cancel.assert_called_once()
            audit_cancel.assert_called_once()
