import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from utils.scouts import Actor, Scouts, DEFAULTS
from dashboard.rbac import PERMISSION_KEYS


def confirmed_action(**values):
    return dict(dict(action_type='message_delete', event_at=time.time(), count=1,
                     audit_id='100', moderator_id='50', scout_roles=['5'],
                     confirmed_scout=True, moderator_is_bot=False, attribution='confirmed'), **values)


class ScoutStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.db'
        self.s = Scouts(self.path)
        self.s.initialize()
        self.admin = Actor('1','99',frozenset(PERMISSION_KEYS))
        self.scout = Actor('1','10',frozenset('scouts.'+p for p in ('activity.view','activity.create','moderation.view','reports.handle','checkins.respond')))
        self.s.save_settings(self.admin, dict(DEFAULTS,enabled=True,automatic_enabled=True,scout_roles=['5'],activity_channel='9',monitored_channels=['2']))

    def tearDown(self):
        self.tmp.cleanup()

    def manual(self, actor=None, **kw):
        return self.s.manual(actor or self.scout, dict(type='other',summary='Context',action='Reminder'), **kw)

    def report(self, reporter='80', mid='123', source='r1'):
        return self.s.report('1',reporter,dict(message_id=mid,channel_id='2',content='Selected message',author_id='70',jump_url='https://discord.com/channels/1/2/'+mid,attachments=[]),'Other','PROTECTED explanation',source)

    def checkin(self):
        return self.s.request_checkins(self.admin,['10'],source='batch')[0]

    def test_defaults_disabled_and_idempotent_migration(self):
        self.assertFalse(self.s.settings('2')['enabled'])
        before=self.s.rows('SELECT * FROM scout_schema')
        self.s.initialize()
        self.assertEqual(before,self.s.rows('SELECT * FROM scout_schema'))

    def test_revision_ownership_and_cross_guild(self):
        r=self.manual()
        with self.assertRaises(PermissionError):
            self.s.edit(self.admin,r['id'],r['data'],1)
        with self.assertRaises(ValueError):
            self.s.record(Actor('2','10',self.scout.permissions),r['id'])
        self.s.edit(self.scout,r['id'],dict(r['data'],summary='Changed'),1)
        with self.assertRaises(ValueError):
            self.s.edit(self.scout,r['id'],r['data'],1)
        self.s.void(self.admin,r['id'],'Mistaken entry')
        self.assertEqual(len(self.s.revisions(self.admin,r['id'])),3)
        with self.assertRaises(PermissionError):
            self.s.edit(self.scout,r['id'],r['data'],3)

    def test_capabilities_are_independent_and_revoked_eligibility_is_denied(self):
        r=self.manual()
        for key, fn in [('activity.create',lambda a:self.manual(a)),('activity.view',lambda a:self.s.record(a,r['id'])),('activity.review',lambda a:self.s.revisions(a,r['id'])),('activity.void',lambda a:self.s.void(a,r['id'],'x'))]:
            with self.assertRaises(PermissionError):
                fn(Actor('1','10',frozenset(PERMISSION_KEYS-{'scouts.'+key})))
            with self.assertRaises(PermissionError):
                fn(Actor('1','10',frozenset(PERMISSION_KEYS),False))
        self.assertFalse('scouts.reports.identity.view' in self.scout.permissions)

    def test_report_privacy_all_public_paths_and_no_scout_authorship(self):
        r=self.report()
        public=[r,self.s.record(self.scout,r['id']),self.s.listing(self.scout,'report'),self.s.revisions(self.admin,r['id']),self.s.rows('SELECT * FROM scout_jobs'),self.s.ai_sources(self.admin,'10')]
        for obj in public:
            text=json.dumps(obj)
            self.assertNotIn('PROTECTED',text)
            self.assertNotIn('reporter_id',text)
        self.assertEqual(r['author_id'],'')
        with self.assertRaises(PermissionError):
            self.s.reporter(self.scout,r['id'])
        only_identity=Actor('1','99',frozenset({'scouts.reports.identity.view'}))
        self.assertEqual(self.s.reporter(only_identity,r['id'])['reporter_id'],'80')
        with self.assertRaises(ValueError):
            self.s.reporter(Actor('2','99',only_identity.permissions),r['id'])

    def test_reports_same_message_preserved_but_pings_cooled(self):
        a=self.report()
        b=self.report('81',source='r2')
        self.assertNotEqual(a['id'],b['id'])
        self.assertTrue(a['data']['notify_roles'])
        self.assertFalse(b['data']['notify_roles'])
        self.assertEqual(self.report(source='duplicate')['id'],a['id'])
        with self.assertRaises(ValueError):
            self.report(mid='456',source='another')

    def test_concurrent_duplicate_reports_are_atomic(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows=list(pool.map(lambda _:self.report(),range(12)))
        self.assertEqual(len({r['id'] for r in rows}),1)
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_jobs')),1)

    def test_handling_requires_outcome_and_escalation_does_not_close(self):
        r=self.report()
        with self.assertRaises(ValueError):
            self.s.handle(self.scout,r['id'],'')
        follow=self.manual(related=r['id'])
        e=self.s.escalate(self.scout,'Need a Ranger',r['id'])
        self.assertEqual(self.s.record(self.scout,r['id'])['status'],'new')
        handled=self.s.handle(self.scout,r['id'],'Reviewed with no further action')
        self.assertEqual(handled['status'],'handled')
        self.assertEqual(follow['data']['related'],r['id'])
        self.assertEqual(e['status'],'open')
        with self.assertRaises(ValueError):
            self.s.handle(self.scout,r['id'],'Duplicate')

    def test_checkin_requester_sees_status_without_answers(self):
        r=self.checkin()
        self.assertEqual(self.s.listing(self.admin,'checkin',scout='10')[0]['id'],r['id'])
        self.assertEqual(self.s.listing(self.admin,'checkin',scout='11'),[])
        requester=Actor('1','20',frozenset({'scouts.checkins.request'}))
        self.s.answer(self.scout,r['id'],['PRIVATE A','PRIVATE B','PRIVATE C'])
        self.assertEqual(self.s.record(requester,r['id'])['status'],'answered')
        with self.assertRaises(PermissionError):
            self.s.answers(requester,r['id'])
        self.assertEqual(self.s.answers(self.scout,r['id'])['answers'][0],'PRIVATE A')
        with self.assertRaises(PermissionError):
            self.s.answers(Actor('1','11',self.scout.permissions),r['id'])
        public=[self.s.record(requester,r['id']),self.s.revisions(self.admin,r['id']),self.s.rows('SELECT * FROM scout_jobs')]
        self.assertNotIn('PRIVATE',json.dumps(public))
        with self.assertRaises(ValueError):
            self.s.answer(self.scout,r['id'],['Again']*3)

    def test_checkin_cancel_nudge_restart_and_duplicate_request(self):
        r=self.checkin()
        self.assertEqual(self.checkin()['id'],r['id'])
        self.s.checkin_action(self.admin,r['id'],'nudge')
        with self.assertRaises(ValueError):
            self.s.checkin_action(self.admin,r['id'],'nudge')
        self.s.checkin_action(self.admin,r['id'],'cancel')
        restarted=Scouts(self.path)
        restarted.initialize()
        with self.assertRaises(ValueError):
            restarted.answer(self.scout,r['id'],['a']*3)
        with self.assertRaises(PermissionError):
            restarted.answer(Actor('1','10',self.scout.permissions,False),r['id'],['a']*3)

    def test_buffer_scope_edits_retention_missing_and_immutable_system(self):
        m=dict(message_id='1',channel_id='2',content='Before',author_id='3',attachments=[])
        self.s.buffer('1',m)
        self.s.buffer('1',dict(m,content='After'))
        self.s.buffer('1',dict(m,message_id='2',channel_id='9'))
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_buffer')),1)
        r=self.s.system('1','audit:200',confirmed_action(action_type='message_bulk_delete',audit_id='200',message_ids=['1','2']))
        self.assertEqual(r['data']['messages'][0]['content'],'After')
        self.assertEqual(r['data']['messages'][0]['edits'][0]['content'],'Before')
        self.assertIsNone(r['data']['messages'][1]['content'])
        with self.assertRaises(PermissionError):
            self.s.edit(self.scout,r['id'],dict(type='other',summary='x',action='y'),1)
        with self.assertRaises(ValueError):
            self.s.void(self.admin,r['id'],'no')
        with self.s.connect() as c:
            c.execute('UPDATE scout_buffer SET observed_at=?',(time.time()-8*86400,))
        self.s.prune('1')
        self.assertEqual(self.s.rows('SELECT * FROM scout_buffer'),[])
        self.assertEqual(self.s.record(self.admin,r['id'])['data']['messages'][0]['content'],'After')

    def test_duplicate_events_and_incrementing_audit_entries(self):
        data=confirmed_action()
        r=self.s.system('1','audit:100',data)
        self.assertEqual(self.s.system('1','audit:100',data)['id'],r['id'])
        self.s.system('1','audit:100',dict(data,count=3))
        revisions=self.s.revisions(self.admin,r['id'])
        self.assertEqual(len(revisions),2)
        self.assertEqual(json.loads(revisions[0]['data'])['data']['count'],1)
        self.assertEqual(self.s.record(self.admin,r['id'])['data']['count'],3)

    def test_concurrent_moderators_never_guessed_for_deleted_message(self):
        now=time.time()
        for i in (1,2):
            self.s.system('1','audit:'+str(i),confirmed_action(event_at=now,audit_id=str(i),moderator_id=str(i),channel_id='2'))
        r=self.s.system('1','gateway:1',dict(action_type='message_delete',event_at=now,channel_id='2',message_ids=['111'],attribution='unattributed'))
        self.s.reconcile_links('1')
        self.assertIsNone(r)
        d=json.loads(self.s.rows("SELECT data FROM scout_observations WHERE source_key='gateway:1'")[0]['data'])
        self.assertEqual(len(d['audit_candidates']),2)
        self.assertNotIn('moderator_id',d)
        self.assertTrue(d['attribution'].startswith('uncertain'))

    def test_ai_sources_exclude_private_report_details_and_check_permissions(self):
        self.report()
        self.manual()
        r=self.checkin()
        self.s.answer(self.scout,r['id'],['PRIVATE']*3)
        ai=Actor('1','20',frozenset({'scouts.ai.use','scouts.activity.view','scouts.checkins.request'}))
        sources=self.s.ai_sources(ai,'10')
        self.assertNotIn('PRIVATE',json.dumps(sources))
        self.assertNotIn('PROTECTED',json.dumps(self.s.ai_sources(self.admin,'10')))
        self.assertIn('PRIVATE',json.dumps(self.s.ai_sources(self.admin,'10')))
        self.assertNotIn('report', [s['kind'] for s in sources])

    def test_failed_only_retry_never_resends_uncertain_jobs(self):
        self.manual()
        j=self.s.rows('SELECT * FROM scout_jobs')[0]
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='uncertain'")
        self.s.retry_delivery(self.admin,j['id'])
        self.assertEqual(self.s.rows('SELECT status FROM scout_jobs')[0]['status'],'uncertain')
        with self.s.connect() as c:
            c.execute("UPDATE scout_jobs SET status='failed'")
        self.s.retry_delivery(self.admin,j['id'])
        self.assertEqual(self.s.rows('SELECT status FROM scout_jobs')[0]['status'],'pending')


class ScoutAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_controls_roundtrip_after_restart(self):
        from cogs.scouts import ScoutControl
        rid='1234567890abcdef'
        item=ScoutControl('answer',rid,'1')
        match=ScoutControl.__discord_ui_compiled_template__.match(item.custom_id)
        restored=await ScoutControl.from_custom_id(None,item,match)
        self.assertEqual((restored.action,restored.rid,restored.guild),('answer',rid,'1'))

    async def test_card_never_contains_report_identity_or_explanation(self):
        from cogs.scouts import card_embed
        r=dict(id='a'*16,guild_id='1',kind='report',status='new',created_at=time.time(),data=dict(category='Other',message=dict(message_id='2',channel_id='3',content='Context',author_id='4'),reporter_id='SECRET',explanation='PRIVATE'))
        output=card_embed(r,'marker').to_dict()
        self.assertNotIn('SECRET',json.dumps(output))
        self.assertNotIn('PRIVATE',json.dumps(output))

    async def test_voluntary_voice_changes_have_no_listener(self):
        from cogs.scouts import ScoutsCog
        self.assertNotIn('on_voice_state_update',[name for name,_ in ScoutsCog.__cog_listeners__])

    async def test_dm_response_confirmation_does_not_echo_answers(self):
        from cogs.scouts import ScoutModal
        cog=SimpleNamespace(actor=AsyncMock(return_value=Actor('1','10',frozenset({'scouts.checkins.respond'}))),service=MagicMock())
        cog.service.answer.return_value=dict(id='a'*16)
        async def call(fn,*a):return fn(*a)
        cog.call=call
        m=ScoutModal(cog,'1','answer',rid='a'*16,questions=['One?'])
        m.inputs['answer0']._value='SECRET ANSWER'
        interaction=SimpleNamespace(user=SimpleNamespace(id=10),response=SimpleNamespace(defer=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))
        await m.on_submit(interaction)
        text=interaction.followup.send.call_args.args[0]
        self.assertNotIn('SECRET',text)
        self.assertIn('only in The Garden',text)

class ScoutAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from cogs.scouts import ScoutsCog
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Scouts(Path(self.tmp.name)/'test.db')
        self.s.initialize()
        self.admin=Actor('1','99',frozenset(PERMISSION_KEYS))
        self.s.save_settings(self.admin,dict(DEFAULTS,enabled=True,automatic_enabled=True,scout_roles=['5'],activity_channel='9',monitored_channels=['2']))
        self.cog=ScoutsCog(MagicMock())
        self.cog.service=self.s
        self.guild=MagicMock(id=1)
        self.channel=SimpleNamespace(id=2,category_id=None,parent_id=None)
        self.guild.get_channel_or_thread.return_value=self.channel
        self.guild.fetch_member=AsyncMock(return_value=SimpleNamespace(id=10,bot=False,roles=[SimpleNamespace(id=5)]))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def entry(self,action,**kwargs):
        return SimpleNamespace(action=action,id=123,user=SimpleNamespace(id=10),guild=self.guild,
            created_at=datetime_now(),target=SimpleNamespace(id=20),extra=SimpleNamespace(channel=self.channel,count=1),
            before=SimpleNamespace(),after=SimpleNamespace(),reason='Reason',**kwargs)

    async def test_bot_reason_cannot_attribute_action_to_human_scout(self):
        self.guild.fetch_member.return_value=SimpleNamespace(id=10,bot=True,roles=[SimpleNamespace(id=5)])
        e=self.entry(discord.AuditLogAction.message_delete)
        e.reason='Action requested by Scout 1234'
        await self.cog.ingest_audit(self.guild,e)
        self.assertEqual(self.s.rows('SELECT * FROM scout_records'),[])
        self.assertEqual(self.s.rows('SELECT * FROM scout_jobs'),[])
        d=json.loads(self.s.rows('SELECT data FROM scout_observations')[0]['data'])
        self.assertEqual(d['moderator_id'],'10')
        self.assertFalse(d['confirmed_scout'])

    async def test_delayed_audit_links_to_pending_evidence_without_guessing(self):
        r=self.s.system('1','gateway:delayed',dict(action_type='message_delete',event_at=time.time(),message_ids=['1'],channel_id='2',attribution='unattributed'))
        self.s.reconcile_links('1')
        self.assertIsNone(r)
        self.assertNotIn('audit_candidates',json.loads(self.s.rows("SELECT data FROM scout_observations WHERE source_key='gateway:delayed'")[0]['data']))
        await self.cog.ingest_audit(self.guild,self.entry(discord.AuditLogAction.message_delete))
        self.s.reconcile_links('1')
        d=json.loads(self.s.rows("SELECT data FROM scout_observations WHERE source_key='gateway:delayed'")[0]['data'])
        self.assertEqual(len(d['audit_candidates']),1)
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_records')),1)
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_jobs')),1)
        self.assertNotIn('moderator_id',d)

    async def test_timeout_expiry_differs_from_manual_removal(self):
        from datetime import datetime,timezone,timedelta
        past=datetime.now(timezone.utc)-timedelta(seconds=10)
        before=SimpleNamespace(id=30,guild=self.guild,timed_out_until=past)
        after=SimpleNamespace(id=30,guild=self.guild,timed_out_until=None)
        await self.cog.on_member_update(before,after)
        first=json.loads(self.s.rows('SELECT data FROM scout_observations')[0]['data'])
        self.assertEqual(first['timeout_change'],'expiry')
        before.timed_out_until=datetime.now(timezone.utc)+timedelta(hours=1)
        await self.cog.on_member_update(before,after)
        data=[json.loads(r['data']) for r in self.s.rows('SELECT data FROM scout_observations')]
        self.assertIn('removed',[d['timeout_change'] for d in data])
        self.assertTrue(all('moderator_id' not in d for d in data))

    async def test_bulk_channel_target_scope_and_grouped_voice_audit_without_target(self):
        e=self.entry(discord.AuditLogAction.message_bulk_delete)
        e.extra=SimpleNamespace(count=4)
        e.target=SimpleNamespace(id=2)
        await self.cog.ingest_audit(self.guild,e)
        d=json.loads(self.s.rows('SELECT data FROM scout_records')[0]['data'])
        self.assertEqual(d['channel_id'],'2')
        self.assertEqual(d['count'],4)
        e=self.entry(discord.AuditLogAction.member_move)
        e.id=124;e.target=None;e.extra.count=2
        await self.cog.ingest_audit(self.guild,e)
        rows=self.s.rows("SELECT data FROM scout_records WHERE source_key='audit:124'")
        self.assertEqual(json.loads(rows[0]['data'])['target_id'],'')


def datetime_now():
    from datetime import datetime,timezone
    return datetime.now(timezone.utc)

class ScoutAITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ScoutStorageTests.setUp(self)
        self.s.save_settings(self.admin,dict(self.s.settings('1'),ai_drafts=True,ai_summaries=True))

    async def asyncTearDown(self):
        ScoutStorageTests.tearDown(self)

    async def test_draft_preview_uses_shared_service_without_saving_activity(self):
        from utils.scout_ai import preview
        generate=AsyncMock(return_value=SimpleNamespace(ok=True,text='Improved draft'))
        with patch('utils.scout_ai.check_ai_cooldown',AsyncMock(return_value=(True,0))),patch('utils.scout_ai.set_ai_cooldown',AsyncMock()),patch('utils.scout_ai.generate_ai_response',generate):
            text=await preview(self.s,self.admin,'draft',draft='My original draft')
        self.assertEqual(text,'Improved draft')
        self.assertEqual(generate.call_args.kwargs['source_command'],'scouts.dashboard.draft')
        self.assertIsNotNone(generate.call_args.kwargs['db'])
        self.assertEqual(self.s.rows('SELECT * FROM scout_records'),[])

    async def test_ai_failure_keeps_domain_unchanged(self):
        from utils.scout_ai import preview
        with patch('utils.scout_ai.check_ai_cooldown',AsyncMock(return_value=(True,0))),patch('utils.scout_ai.set_ai_cooldown',AsyncMock()),patch('utils.scout_ai.generate_ai_response',AsyncMock(return_value=SimpleNamespace(ok=False,error='Budget reached'))):
            with self.assertRaisesRegex(ValueError,'Budget reached'):
                await preview(self.s,self.admin,'draft',draft='Original remains in the editor')
        self.assertEqual(self.s.rows('SELECT * FROM scout_records'),[])

    async def test_stored_reporter_details_never_enter_gemini_even_for_admin(self):
        from utils.scout_ai import preview
        self.s.report('1','80',dict(message_id='1',channel_id='2',content='Message'),'Other','DO NOT SEND THIS EXPLANATION','ai-report')
        generate=AsyncMock(return_value=SimpleNamespace(ok=True,text='No source records'))
        with patch('utils.scout_ai.check_ai_cooldown',AsyncMock(return_value=(True,0))),patch('utils.scout_ai.set_ai_cooldown',AsyncMock()),patch('utils.scout_ai.generate_ai_response',generate):
            await preview(self.s,self.admin,'summary',scout='10')
        self.assertNotIn('DO NOT SEND',generate.call_args.kwargs['prompt'])
        self.assertNotIn('reporter_id',generate.call_args.kwargs['prompt'])

    async def test_sensitive_gemini_request_never_logs_prompts_or_answers(self):
        from utils.ai_service import generate_ai_response
        response=SimpleNamespace(text='PRIVATE CHECKIN SUMMARY',usage_metadata=None)
        client=MagicMock()
        client.aio.models.generate_content=AsyncMock(return_value=response)
        env=dict(AI_ENABLED='true',GEMINI_API_KEY='test-key',AI_LOG_PROMPTS='true',AI_LOG_RESPONSES='true')
        with patch.dict(os.environ,env),patch('utils.ai_service.genai.Client',return_value=client),patch('utils.ai_service.can_run_ai_request',AsyncMock(return_value=(True,None,0,0,0))),patch('utils.ai_service.logger') as logger:
            result=await generate_ai_response(task_type='scout_summary',prompt='PRIVATE CHECKIN ANSWERS',sensitive_content=True)
        self.assertTrue(result.ok)
        self.assertNotIn('PRIVATE',str(logger.mock_calls))
        logger.info.assert_not_called()
