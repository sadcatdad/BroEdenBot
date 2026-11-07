import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from dashboard.app import app
from dashboard.rbac import PERMISSION_KEYS, initialize_rbac_schema, list_roles, replace_discord_role_mappings, set_user_permission_override
from dashboard.users import initialize_dashboard_users, hash_password
from utils.scouts import Scouts, Actor, DEFAULTS
from utils.settings import initialize_settings_from_env
from utils.scout_access import member_actor


class ScoutDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'data.db'
        self.env=patch.dict(os.environ,dict(DATABASE_PATH=str(self.path),DASHBOARD_ENABLED='true',DASHBOARD_USERNAME='owner',DASHBOARD_PASSWORD='owner-password',DASHBOARD_SECRET_KEY='scout-test-key',DASHBOARD_AUTH_MODE='password',GUILD_ID='1',BOT_OWNER_USER_IDS=''))
        self.env.start()
        initialize_settings_from_env()
        initialize_dashboard_users()
        initialize_rbac_schema()
        self.s=Scouts(self.path)
        self.s.initialize()
        self.admin=Actor('1','99',frozenset(PERMISSION_KEYS))
        self.s.save_settings(self.admin,dict(DEFAULTS,enabled=True,scout_roles=['5'],activity_channel='9'))
        self.scout_role=next(r['id'] for r in list_roles() if r['role_key']=='scout')
        replace_discord_role_mappings(['5'],self.scout_role,changed_by='test')
        with sqlite3.connect(self.path) as c:
            self.uid=c.execute("INSERT INTO dashboard_users(username,password_hash,discord_user_id,role,status,auth_provider,access_source) VALUES(?,?,?,'viewer','active','password','discord_role')",('scout',hash_password('scout-password'),'10')).lastrowid
        from dashboard.rbac import sync_discord_role_assignments
        sync_discord_role_assignments(self.uid,['5'])
        self.member=patch('utils.scout_access.fetch_member',AsyncMock(return_value=dict(roles=['5'],pending=False,user=dict(id='10',bot=False))))
        self.member_mock=self.member.start()
        self.client=TestClient(app)

    def tearDown(self):
        self.client.close()
        self.member.stop()
        self.env.stop()
        self.tmp.cleanup()

    def login(self,name='scout',password='scout-password'):
        page=self.client.get('/login')
        token=re.search('name="csrf" value="([^"]+)"',page.text).group(1)
        return self.client.post('/login',data=dict(username=name,password=password,csrf=token))

    def csrf(self):
        text=self.client.get('/scouts').text
        # Overview need not contain a mutating form; logout always includes a token.
        return re.search('name="csrf" value="([^"]+)"',text).group(1)

    def test_scout_only_landing_navigation_and_restricted_sections(self):
        self.login()
        page=self.client.get('/')
        self.assertEqual(page.status_code,200)
        self.assertTrue(page.url.path=='/scouts')
        self.assertIn('<span>Scouts</span>',page.text)
        self.assertNotIn('<span>Bank</span>',page.text)
        self.assertNotIn('<span>Access</span>',page.text)
        self.assertEqual(self.client.get('/scouts/section/settings').status_code,403)
        for path in ('/scouts/section/activity','/scouts/section/reports','/scouts/section/system','/scouts/section/checkins'):
            r=self.client.get(path)
            self.assertEqual(r.status_code,200,r.text)
            self.assertIn('private, no-store',r.headers['cache-control'])

    def test_current_role_removal_and_account_restrictions(self):
        self.login()
        self.member_mock.return_value=dict(roles=[],pending=False,user=dict(id='10',bot=False))
        self.assertEqual(self.client.get('/scouts/section/activity').status_code,403)
        self.member_mock.return_value=dict(roles=['5'],pending=False,user=dict(id='10',bot=False))
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE dashboard_users SET status='disabled' WHERE id=?",(self.uid,))
        r=self.client.get('/scouts',follow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertIn('/login',r.headers['location'])

    def test_reports_no_identity_in_search_history_or_notification_until_granted(self):
        r=self.s.report('1','80',dict(message_id='100',channel_id='2',content='Context',author_id='70',jump_url='https://discord.com/channels/1/2/100',attachments=[]),'Other','VERY PRIVATE EXPLANATION','report-1')
        self.login()
        page=self.client.get('/scouts/records/'+r['id'])
        self.assertEqual(page.status_code,200,page.text)
        self.assertNotIn('VERY PRIVATE',page.text)
        self.assertNotIn('Protected reporter details',page.text)
        self.assertNotIn(r['id'], self.client.get('/scouts/section/reports?q=VERY+PRIVATE').text)
        set_user_permission_override(self.uid,'scouts.reports.identity.view','allow',changed_by='test')
        page=self.client.get('/scouts/records/'+r['id'])
        self.assertIn('VERY PRIVATE EXPLANATION',page.text)
        self.assertIn('Reporter: 80',page.text)
        self.assertNotIn('VERY PRIVATE',self.client.get('/scouts/section/reports').text)

    def test_requester_cannot_read_others_answers(self):
        r=self.s.request_checkins(self.admin,['11'])[0]
        responder=Actor('1','11',frozenset({'scouts.checkins.respond'}))
        self.s.answer(responder,r['id'],['CHECKIN SECRET']*3)
        set_user_permission_override(self.uid,'scouts.checkins.request','allow',changed_by='test')
        self.login()
        page=self.client.get('/scouts/records/'+r['id'])
        self.assertEqual(page.status_code,200,page.text)
        self.assertNotIn('CHECKIN SECRET',page.text)
        self.assertIn('Response received',page.text)
        set_user_permission_override(self.uid,'scouts.checkins.view_all','allow',changed_by='test')
        self.assertIn('CHECKIN SECRET',self.client.get('/scouts/records/'+r['id']).text)

    def test_csrf_server_isolation_and_outcome_required(self):
        r=self.s.report('1','80',dict(message_id='100',channel_id='2',content='Context',author_id='70',jump_url='https://discord.com/channels/1/2/100'),'Other','private','report-1')
        self.login()
        path='/scouts/records/'+r['id']+'/action'
        self.assertEqual(self.client.post(path,data=dict(action_type='handle',outcome='done')).status_code,400)
        token=self.csrf()
        self.assertEqual(self.client.post(path,data=dict(csrf=token,action_type='handle',outcome='')).status_code,400)
        handled=self.client.post(path,data=dict(csrf=token,action_type='handle',outcome='Reviewed'))
        self.assertEqual(handled.status_code,200,handled.text)
        with self.s.connect() as c:
            foreign=self.s._create(c,'2','manual','10',dict(summary='FOREIGN PRIVATE',action='x',type='other'),notify=False)
        response=self.client.get('/scouts/records/'+foreign['id'])
        self.assertEqual(response.status_code,404)
        self.assertNotIn('FOREIGN PRIVATE',response.text)

    def test_password_owner_configuration_and_all_templates_render(self):
        self.login('owner','owner-password')
        for path in ('/scouts','/scouts/section/settings','/scouts/section/activity','/scouts/section/system','/scouts/section/checkins','/scouts/section/reports'):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/settings/access').status_code,200)
        scout=member_actor(self.s,'1','10',['5'])
        self.assertIn('scouts.activity.create',scout.permissions)
        self.assertNotIn('scouts.reports.identity.view',scout.permissions)
        removed=member_actor(self.s,'1','10',[])
        self.assertNotIn('scouts.activity.view',removed.permissions)

    def test_old_admin_roles_receive_new_permissions_without_other_role_grants(self):
        roles={r['role_key']:r for r in list_roles()}
        expected={p for p in PERMISSION_KEYS if p.startswith('scouts.')}
        self.assertTrue(expected <= set(roles['administrator']['permissions']))
        self.assertFalse(expected & set(roles['moderator']['permissions']))
        self.assertNotIn('scouts.reports.identity.view',roles['scout']['permissions'])

    def test_ai_route_failure_preserves_draft_without_saving_it(self):
        set_user_permission_override(self.uid,'scouts.ai.use','allow',changed_by='test')
        self.login()
        token=self.csrf()
        with patch('dashboard.scouts_routes.preview',AsyncMock(side_effect=RuntimeError('Provider unavailable'))):
            response=self.client.post('/scouts/ai',data=dict(csrf=token,mode='draft',draft='MY UNSAVED DRAFT'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('MY UNSAVED DRAFT',response.text)
        self.assertIn('original draft is preserved',response.text)
        self.assertNotIn('Accept and save activity',response.text)
        self.assertEqual(self.s.rows('SELECT * FROM scout_records'),[])

    def test_dashboard_log_accepts_message_link_as_evidence_and_queues_staff_delivery(self):
        self.login()
        token = self.csrf()
        response = self.client.post('/scouts/activity',data=dict(csrf=token,submission='message-link-test',
            type='rule reminder',summary='A concern',action='Gave a reminder',members='jojo',
            channel='2',incident_at='3:30am',related='https://discord.com/channels/1/2/300'))
        self.assertEqual(response.status_code,200,response.text)
        row = self.s.rows("SELECT data FROM scout_records WHERE kind='manual'")[0]
        self.assertEqual(json.loads(row['data'])['evidence'],'https://discord.com/channels/1/2/300')
        job = self.s.rows('SELECT status,destination FROM scout_jobs')[0]
        self.assertEqual((job['status'],job['destination']),('pending','9'))

    def test_dashboard_escalation_validation_preserves_the_draft(self):
        self.login()
        response = self.client.post('/scouts/escalate',data=dict(csrf=self.csrf(),submission='bad-link-test',
            reason='KEEP MY ESCALATION DRAFT',related='bad-reference'))
        self.assertEqual(response.status_code,400,response.text)
        self.assertIn('KEEP MY ESCALATION DRAFT',response.text)
        self.assertIn('bad-reference',response.text)
        self.assertEqual(self.s.rows('SELECT * FROM scout_records'),[])
        self.assertEqual(self.s.rows('SELECT * FROM scout_jobs'),[])
