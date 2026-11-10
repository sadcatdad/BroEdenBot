"""Incident summaries stay scoped, reviewable, optional and permission checked."""
import asyncio
from datetime import datetime, timezone
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.scouts import ScoutModal, card_embed
from dashboard.rbac import PERMISSION_KEYS
from utils.message_context import MESSAGE_CONTEXT_TABLE_SQL
from utils.scouts import Actor, Scouts, DEFAULTS
from utils.scout_context import (anchor_time, message_reference, archive_messages, buffer_messages,
    sample_messages, summarize_context, clean_context_metadata, MAX_CHARS)
from tests import test_scout_log_workflow as workflow

NOW=1794250000.0

def row(mid='100',channel='2',at=NOW,**kw):
    return dict(message_id=mid,channel_id=channel,created_at=at,author_id='90',author_name='Member',content='A message',**kw)


def sample():
    return sample_messages([row(),row('101',at=NOW-1)],guild='1',channel='2',anchor=NOW,minutes=10,source='message_archive',now=NOW)


def metadata():
    s=sample()
    return dict({k:v for k,v in s.items() if k!='messages'},source_ids=['100'],caveats=[],model='gemini-2.5-flash',generated_at=NOW)


class ContextStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'archive.db'
        with sqlite3.connect(self.path) as c:c.execute(MESSAGE_CONTEXT_TABLE_SQL)
        self.s=Scouts(Path(self.tmp.name)/'scouts.db');self.s.initialize()
        self.actor=Actor('1','10',frozenset(PERMISSION_KEYS))
        self.s.save_settings(self.actor,dict(DEFAULTS,enabled=True,automatic_enabled=True,scout_roles=['5'],activity_channel='9',monitored_channels=['2']))

    def tearDown(self):self.tmp.cleanup()

    def archive(self,mid,**kw):
        iso=lambda v:datetime.fromtimestamp(v,timezone.utc).isoformat()
        data=dict(guild_id='1',channel_id='2',message_id=str(mid),author_id='90',timestamp=iso(NOW),stored_at=iso(NOW),content='Hello')
        data.update(kw)
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO message_context_messages ('+','.join(data)+') VALUES ('+','.join('?' for _ in data)+')',tuple(data.values()))

    def read(self,excluded=()):
        return archive_messages(self.path,'1','2',NOW-600,NOW,NOW,excluded)

    def test_archive_exact_guild_channel_and_no_private_thread_aggregation(self):
        self.archive(1);self.archive(2,channel_id='3',parent_channel_id='2',content='PRIVATE THREAD')
        self.archive(3,guild_id='4',content='FOREIGN');self.archive(4,channel_id='5',content='OTHER CHANNEL')
        self.assertEqual([r['message_id'] for r in self.read()],['1'])
        self.assertFalse(self.read(['2']))
        self.archive(5,parent_channel_id='8')
        self.assertEqual([r['message_id'] for r in self.read(['8'])],['1'])

    def test_archive_excludes_deleted_bots_webhooks_late_edits_and_outside_time(self):
        self.archive(1)
        for mid,kw in enumerate([dict(is_deleted=1),dict(is_bot=1),dict(is_webhook=1),
            dict(edited_at=datetime.fromtimestamp(NOW+2,timezone.utc).isoformat()),
            dict(timestamp=datetime.fromtimestamp(NOW-601,timezone.utc).isoformat()),dict(content='')],2):self.archive(mid,**kw)
        self.assertEqual([r['message_id'] for r in self.read()],['1'])
        self.assertFalse(archive_messages(self.path,'9','2',NOW-600,NOW,NOW))

    def test_buffer_respects_time_channel_and_retention(self):
        current=time.time()
        self.s.buffer('1',row(at=current-100))
        self.s.buffer('1',row('101',at=current-2000))
        self.s.buffer('1',row('102',at=current-100,is_bot=True))
        self.s.buffer('1',row('103',at=current-100,edited_at=current+1))
        with self.s.connect() as c:c.execute("UPDATE scout_buffer SET observed_at=? WHERE message_id='100'",(current-8*86400,))
        self.assertFalse(buffer_messages(self.s,'1','2',current-600,current,current,7))
        self.assertFalse(buffer_messages(self.s,'1','3',current-600,current,current,90))
        self.assertEqual([m['message_id'] for m in buffer_messages(self.s,'1','2',current-600,current,current,90)],['100'])

    def test_closest_bounded_sample_deduplication_and_redaction(self):
        rows=[row(str(100+i),at=NOW-i) for i in range(100)]
        rows[0]['content']='API_KEY=super-secret hello'
        rows += [row('999',channel='3'),row('998',at=NOW+1),row('997',is_bot=True)]
        s=sample_messages(rows,guild='1',channel='2',anchor=NOW,minutes=10,source='message_archive',selected=rows[0],now=NOW)
        self.assertEqual(s['message_count'],80);self.assertTrue(s['limited'])
        self.assertIn('100',[m['id'] for m in s['messages']]);self.assertNotIn('180',[m['id'] for m in s['messages']])
        self.assertNotIn('super-secret',json.dumps(s));self.assertIn('[REDACTED]',json.dumps(s))
        long=[dict(row(str(i)),content='x'*4000) for i in range(80)]
        bounded=sample_messages(long,guild='1',channel='2',anchor=NOW,minutes=10,source='scout_capture',now=NOW)
        self.assertLessEqual(sum(len(m['text']) for m in bounded['messages']),MAX_CHARS)
        self.assertTrue(bounded['limited'])

    def test_anchor_rejects_ambiguous_future_and_foreign_message_links(self):
        self.assertEqual(anchor_time('2026-11-09T15:30:00-06:00',now=1800000000),1794259800)
        self.assertEqual(anchor_time('<t:1794250000:t>',now=NOW),NOW)
        self.assertEqual(anchor_time('3:30am',message_at=NOW-10,now=NOW),NOW-10)
        for value in ['3:30am','2026-11-09T15:30:00','2100-01-01T00:00:00Z']:
            with self.assertRaises(ValueError):anchor_time(value,now=NOW)
        with self.assertRaises(ValueError):message_reference('https://discord.com/channels/99/2/123','1')
        mid=str((int(NOW*1000)-1420070400000)<<22)
        self.assertEqual(message_reference('https://discord.com/channels/1/2/'+mid,'1')['at'],NOW)

    def test_only_adapter_provenance_is_saved_and_revisions_retain_it(self):
        values=dict(type='other',channel='2',summary='Reviewed context',action='Gave a reminder',ai_context=metadata())
        self.assertNotIn('ai_context',self.s.manual(self.actor,values)['data'])
        record=self.s.manual(self.actor,values,source='same',ai_context=metadata())
        self.assertEqual(self.s.manual(self.actor,values,source='same',ai_context=metadata())['id'],record['id'])
        data=record['data']['ai_context'];self.assertEqual(data['accepted_by'],'10');self.assertNotIn('messages',data)
        changed=self.s.edit(self.actor,record['id'],dict(values,summary='Corrected by Scout'),record['version'])
        self.assertEqual(changed['data']['ai_context'],data)
        self.assertIn('Gemini-assisted',card_embed(changed,'receipt').description)
        self.assertIn('ai_context',json.dumps(self.s.revisions(self.actor,record['id'])))
        for bad in [dict(metadata(),channel_id='9'),dict(metadata(),message_count='bad'),dict(metadata(),generated_at=None)]:
            with self.assertRaises(ValueError):clean_context_metadata(bad,'1','2')

    def test_settings_inherit_only_existing_ai_opt_in_and_validate_window(self):
        old=dict(DEFAULTS,scout_roles=['5'],activity_channel='9',ai_drafts=True,enabled=True);old.pop('ai_context_summaries')
        with self.s.connect() as c:c.execute('UPDATE scout_settings SET data=? WHERE guild_id=?',(json.dumps(old),'1'))
        self.assertTrue(self.s.settings('1')['ai_context_summaries'])
        self.s.save_settings(self.actor,dict(self.s.settings('1'),ai_context_summaries=False))
        self.assertFalse(self.s.settings('1')['ai_context_summaries'])
        for minutes in (0,31,'oops'):
            with self.assertRaises(ValueError):self.s.save_settings(self.actor,dict(self.s.settings('1'),context_minutes=minutes))


class ContextModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.service=Scouts(Path(self.tmp.name)/'ai.db')
        self.actor=Actor('1','10',frozenset(PERMISSION_KEYS))
        self.cooldown=patch('utils.ai_service.check_ai_cooldown',AsyncMock(return_value=(True,0)));self.cooldown.start()
        self.setcooldown=patch('utils.ai_service.set_ai_cooldown',AsyncMock());self.setcooldown.start()

    async def asyncTearDown(self):self.setcooldown.stop();self.cooldown.stop();self.tmp.cleanup()

    async def test_default_model_schema_sensitive_logging_and_source_citations(self):
        result=SimpleNamespace(ok=True,text=json.dumps(dict(summary='Members discussed a reminder.',source_ids=['100'],caveats=[])),model_used='gemini-2.5-flash')
        with patch('utils.ai_service.generate_ai_response',AsyncMock(return_value=result)) as generate:
            draft=await summarize_context(self.service,self.actor,sample())
        args=generate.call_args.kwargs
        self.assertEqual(args['requested_tier'],'default');self.assertTrue(args['sensitive_content']);self.assertIsNotNone(args['response_schema'])
        self.assertIn('untrusted',args['system_instruction']);self.assertNotIn('messages',draft['metadata'])
        self.assertEqual(draft['metadata']['source_ids'],['100'])
        with patch('utils.ai_service.generate_ai_response',AsyncMock(return_value=result)) as generate:
            await summarize_context(self.service,self.actor,sample(),advanced=True)
        self.assertEqual(generate.call_args.kwargs['requested_tier'],'advanced')

    async def test_provider_failure_malformed_or_invented_citations_fail_without_save(self):
        for value in [None,'not JSON',json.dumps(dict(summary='Made up',source_ids=['999'],caveats=[])),json.dumps(dict(summary=['bad'],source_ids=['100'],caveats=[]))]:
            result=SimpleNamespace(ok=value is not None,text=value,model_used='test')
            with patch('utils.ai_service.generate_ai_response',AsyncMock(return_value=result)):
                with self.assertRaises(ValueError):await summarize_context(self.service,self.actor,sample())
        with patch('utils.ai_service.generate_ai_response',AsyncMock()) as generate:
            with self.assertRaises(ValueError):await summarize_context(self.service,self.actor,dict(sample(),messages=[]))
            with self.assertRaises(PermissionError):await summarize_context(self.service,Actor('1','10',frozenset()),sample())
            generate.assert_not_awaited()


class ContextWorkflowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=workflow.ScoutLogWorkflowTests.asyncSetUp
    asyncTearDown=workflow.ScoutLogWorkflowTests.asyncTearDown
    interaction=workflow.ScoutLogWorkflowTests.interaction

    async def setup_context(self):
        self.s.save_settings(self.actor,dict(self.s.settings('1'),ai_context_summaries=True))
        self.cfg=self.s.settings('1')
        self.channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),category_id=7,parent_id=None)
        self.bot.get_cog.return_value=None
        self.cog.report_member=AsyncMock()
        self.cog.context_scope=AsyncMock(return_value=(None,set()))
        self.s.save_settings(self.actor,dict(self.cfg,automatic_enabled=True,monitored_channels=['2']))
        self.cfg=self.s.settings('1')
        self.s.buffer('1',row(at=time.time()-2));self.s.buffer('1',row('101',at=time.time()-3))

    async def test_command_defaults_to_invocation_channel_and_preview_never_saves(self):
        await self.setup_context()
        it=self.interaction();it.channel=self.channel
        with patch('cogs.scouts.get_ai_config',return_value=SimpleNamespace(available=True)),patch('cogs.scouts.summarize_context',AsyncMock(return_value=dict(summary='A neutral context draft',metadata=metadata()))):
            await self.cog.log_activity.callback(self.cog,it)
        response=it.followup.send.return_value
        preview=response.edit.call_args.kwargs
        self.assertEqual(preview['view'].factory().values['summary'],'A neutral context draft')
        self.assertEqual(preview['view'].factory().values['channel'],'2')
        self.assertFalse(self.s.rows('SELECT * FROM scout_records'))
        self.assertIn('Nothing is saved',preview['content'])
        self.assertEqual(preview['embed'].color.value,0x9B59B6)
        self.assertGreaterEqual(self.cog.report_member.await_count,3)

    async def test_right_click_anchors_to_selected_message_and_keeps_evidence(self):
        await self.setup_context();it=self.interaction()
        message=SimpleNamespace(id=100,channel=self.channel,author=SimpleNamespace(id=90,display_name='Member',bot=False),
            content='Original message',created_at=datetime.fromtimestamp(time.time()-2,timezone.utc),
            edited_at=None,webhook_id=None,attachments=[],jump_url='https://discord.com/channels/1/2/100')
        with patch('cogs.scouts.get_ai_config',return_value=SimpleNamespace(available=True)),patch('cogs.scouts.summarize_context',AsyncMock(return_value=dict(summary='Surrounding discussion',metadata=metadata()))) as generate:
            await self.cog.log_message(it,message)
        self.assertEqual(generate.call_args.args[2]['anchor_at'],message.created_at.timestamp())
        form=it.followup.send.return_value.edit.call_args.kwargs['view'].factory()
        self.assertEqual(form.values['evidence'],message.jump_url);self.assertEqual(form.values['summary'],'Surrounding discussion')
        self.assertNotIn('action',form.values);self.assertNotIn('outcome',form.values)

    async def test_foreign_message_link_skips_summary_without_blocking_manual_log(self):
        await self.setup_context();it=self.interaction();it.channel=self.channel
        with patch('cogs.scouts.get_ai_config',return_value=SimpleNamespace(available=True)),patch('cogs.scouts.summarize_context',AsyncMock()) as generate:
            await self.cog.log_activity.callback(self.cog,it,evidence_link='https://discord.com/channels/999/2/100')
        result=it.followup.send.return_value.edit.call_args.kwargs
        self.assertIn('this server',result['content']);self.assertEqual(result['view'].factory().mode,'manual')
        generate.assert_not_awaited()

    async def test_manual_open_while_waiting_preserves_form_and_duplicate_submission_key(self):
        await self.setup_context();it=self.interaction();it.channel=self.channel
        async def generate(*args,**kwargs):
            self.original=it.followup.send.call_args.kwargs['view'].factory()
            return dict(summary='Do not replace the open form',metadata=metadata())
        with patch('cogs.scouts.get_ai_config',return_value=SimpleNamespace(available=True)),patch('cogs.scouts.summarize_context',side_effect=generate):
            await self.cog.log_activity.callback(self.cog,it)
        result=it.followup.send.return_value.edit.call_args.kwargs
        self.assertNotIn('embed',result);self.assertNotIn('_ai_context',self.original.values)
        self.assertEqual(self.original.source,str(it.id))

    async def test_failure_still_opens_manual_form(self):
        await self.setup_context();it=self.interaction();it.channel=self.channel
        with patch('cogs.scouts.get_ai_config',return_value=SimpleNamespace(available=True)),patch('cogs.scouts.summarize_context',AsyncMock(side_effect=ValueError('Budget unavailable'))):
            await self.cog.log_activity.callback(self.cog,it)
        result=it.followup.send.return_value.edit.call_args.kwargs
        self.assertIn('Budget unavailable',result['content']);self.assertEqual(result['view'].factory().mode,'manual')
        self.assertFalse(self.s.rows('SELECT * FROM scout_records'))

    async def test_access_revocation_after_generation_discards_draft(self):
        await self.setup_context();it=self.interaction()
        async def generate(*args,**kwargs):
            self.cog.actor.return_value=Actor('1','10',frozenset())
            return dict(summary='PRIVATE DRAFT',metadata=metadata())
        with patch('cogs.scouts.summarize_context',side_effect=generate):
            with self.assertRaises(PermissionError):await self.cog.context_draft(it,self.actor,self.channel,self.cfg)
        self.assertFalse(self.s.rows('SELECT * FROM scout_records'))

    async def test_acceptance_rechecks_visibility_before_saving(self):
        await self.setup_context();self.cog.report_member.side_effect=PermissionError('Access revoked')
        self.guild.get_channel_or_thread.return_value=self.channel
        values=dict(type='other',summary='Reviewed',action='Reminder',channel='2',_ai_context=metadata())
        modal=ScoutModal(self.cog,'1','manual',values=values)
        it=self.interaction();await modal.on_submit(it)
        self.assertFalse(self.s.rows('SELECT * FROM scout_records'))
        self.assertIn('Access revoked',it.followup.send.call_args.args[0])

    async def test_scope_excludes_parent_category_destinations_and_archive_exclusions(self):
        # Use the actual policy implementation rather than the mocked retrieval fixture.
        self.bot.get_cog.return_value=None
        cfg=self.s.settings('1');channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),category_id=7,parent_id=8)
        with patch('cogs.scouts.get_json_ids_setting',return_value=[]),patch.dict('os.environ',MESSAGE_CONTEXT_EXCLUDED_CHANNEL_IDS=''):
            await self.cog.context_scope(channel,cfg)
            for changed in [dict(cfg,excluded_channels=['8']),dict(cfg,excluded_categories=['7']),dict(cfg,activity_channel='8')]:
                with self.assertRaises(PermissionError):await self.cog.context_scope(channel,changed)
        with patch('cogs.scouts.get_json_ids_setting',return_value=[2]):
            with self.assertRaises(PermissionError):await self.cog.context_scope(channel,cfg)

    async def test_buffer_summary_requires_moderation_access(self):
        await self.setup_context()
        restricted=Actor('1','10',frozenset({'scouts.activity.create'}))
        with patch('cogs.scouts.summarize_context',AsyncMock()) as generate:
            with self.assertRaises(PermissionError):await self.cog.context_draft(self.interaction(),restricted,self.channel,self.cfg)
            generate.assert_not_awaited()


from tests import test_scout_dashboard as dashboard_workflow

class ContextDashboardTests(unittest.TestCase):
    setUp=dashboard_workflow.ScoutDashboardTests.setUp
    tearDown=dashboard_workflow.ScoutDashboardTests.tearDown
    login=dashboard_workflow.ScoutDashboardTests.login
    csrf=dashboard_workflow.ScoutDashboardTests.csrf

    def test_context_panel_sources_and_edit_retains_provenance(self):
        record=self.s.manual(self.admin,dict(type='other',summary='Reviewed conversation',action='Gave a reminder',channel='2'),ai_context=metadata())
        self.login()
        page=self.client.get('/scouts/records/'+record['short_id'])
        self.assertEqual(page.status_code,200)
        self.assertIn('Conversation context',page.text);self.assertIn('Gemini-assisted',page.text)
        self.assertIn('https://discord.com/channels/1/2/100',page.text)
        self.assertNotIn("{'guild_id'",page.text)

    def test_settings_save_and_invalid_window_preserves_values(self):
        self.login('owner','owner-password')
        data=dict(self.s.settings('1'),csrf=self.csrf(),ai_context_summaries='on',context_minutes='15',ai_context_advanced='on')
        data.pop('forms');data['questions']='First question'
        response=self.client.post('/scouts/settings',data=data)
        self.assertEqual(response.status_code,200)
        cfg=self.s.settings('1');self.assertEqual(cfg['context_minutes'],15);self.assertTrue(cfg['ai_context_summaries']);self.assertTrue(cfg['ai_context_advanced'])
        data['context_minutes']='45';response=self.client.post('/scouts/settings',data=data)
        self.assertEqual(response.status_code,400);self.assertIn('between 1 and 30',response.text)
        self.assertEqual(self.s.settings('1')['context_minutes'],15)
        self.assertIn('value="45"',response.text)
