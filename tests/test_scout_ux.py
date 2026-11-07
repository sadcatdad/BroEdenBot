import json
import time
import unittest
from unittest.mock import patch
from tests import test_scouts as fixtures
from utils.scout_forms import validate_forms, FORM_DEFAULTS
import copy
confirmed_action=fixtures.confirmed_action

class ScoutUXStorageTests(unittest.TestCase):
    setUp=fixtures.ScoutStorageTests.setUp
    tearDown=fixtures.ScoutStorageTests.tearDown

    def test_custom_questions_are_validated_and_snapshotted(self):
        cfg=self.s.settings('1'); forms=copy.deepcopy(cfg['forms'])
        forms['activity'][-1].update(enabled=True,required=True,label='What support is needed?',placeholder='Tell us what would help.')
        self.s.save_settings(self.admin,dict(cfg,forms=forms))
        with self.assertRaisesRegex(ValueError,'support'):
            self.s.manual(self.scout,dict(type='other',summary='Situation',action='Reminder'))
        record=self.s.manual(self.scout,dict(type='other',summary='Situation',action='Reminder',custom_1='Please review it.'))
        self.assertEqual(record['data']['form_answers'][0]['label'],'What support is needed?')
        forms['activity'][-1]['label']='A different label'
        self.s.save_settings(self.admin,dict(cfg,forms=forms))
        self.assertEqual(self.s.record(self.admin,record['id'])['data']['form_answers'][0]['label'],'What support is needed?')

    def test_form_limits_and_core_requirements_cannot_be_bypassed(self):
        forms=copy.deepcopy(FORM_DEFAULTS)
        forms['activity'][0].update(enabled=False,required=False)
        clean=validate_forms(forms)
        self.assertTrue(clean['activity'][0]['enabled'])
        self.assertTrue(clean['activity'][0]['required'])
        forms['activity'][0]['label']='x'*46
        with self.assertRaises(ValueError):validate_forms(forms)

    def test_record_picker_is_scoped_and_permission_filtered(self):
        r=self.s.manual(self.scout,dict(type='concern',summary='A helpful summary',action='Reminder'))
        choices=self.s.reference_choices(self.scout,'helpful')
        self.assertEqual(choices[0]['value'],r['short_id'])
        self.assertIn('A helpful summary',choices[0]['name'])
        self.assertNotIn(r['id'],json.dumps(choices))
        from utils.scouts import Actor
        with self.assertRaises(PermissionError):self.s.reference_choices(Actor('1','99',frozenset()))
        self.assertEqual(self.s.reference_choices(Actor('2','99',self.scout.permissions)),[])

    def test_unique_complete_deletion_batch_attaches_buffered_text_as_correlated_context(self):
        now=time.time()
        self.s.buffer('1',dict(message_id='100',channel_id='2',author_id='20',content='Captured text',attachments=[]))
        self.s.system('1','gateway:one',dict(action_type='message_delete',event_at=now,channel_id='2',message_ids=['100']))
        r=self.s.system('1','audit:100',confirmed_action(target_id='20',channel_id='2',event_at=now))
        with patch('utils.scouts.time.time',return_value=now+3):self.s.reconcile_links('1')
        saved=self.s.record(self.admin,r['id'])
        self.assertEqual(saved['data']['messages'][0]['content'],'Captured text')
        self.assertTrue(saved['data']['message_context_attribution'].startswith('correlated:'))
        version=saved['version']
        with patch('utils.scouts.time.time',return_value=now+4):self.s.reconcile_links('1')
        self.assertEqual(self.s.record(self.admin,r['id'])['version'],version)
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_records')),1)

    def test_competing_audits_and_wrong_author_never_attach_deleted_text(self):
        now=time.time()
        self.s.buffer('1',dict(message_id='100',channel_id='2',author_id='20',content='Do not guess',attachments=[]))
        self.s.system('1','gateway:one',dict(action_type='message_delete',event_at=now,channel_id='2',message_ids=['100']))
        r=self.s.system('1','audit:100',confirmed_action(target_id='20',channel_id='2',event_at=now))
        self.s.system('1','audit:101',confirmed_action(audit_id='101',target_id='20',channel_id='2',event_at=now,moderator_id='90',confirmed_scout=False,scout_roles=[]))
        with patch('utils.scouts.time.time',return_value=now+3):self.s.reconcile_links('1')
        self.assertNotIn('messages',self.s.record(self.admin,r['id'])['data'])

    def test_custom_member_report_answers_stay_in_protected_storage(self):
        cfg=self.s.settings('1'); forms=copy.deepcopy(cfg['forms'])
        forms['report'][1].update(enabled=True,label='Private additional context',required=True)
        self.s.save_settings(self.admin,dict(cfg,forms=forms))
        r=self.s.report('1','90',dict(message_id='9',channel_id='2',content='Public context'),'Other','Private explanation','r',{'custom_1':'SECRET EXTRA'})
        self.assertNotIn('SECRET EXTRA',json.dumps(self.s.record(self.scout,r['id'])))
        self.assertIn('SECRET EXTRA',self.s.reporter(self.admin,r['id'])['explanation'])

    def deletion(self,key,mid,now,author='20',channel='2',content='Captured'):
        self.s.buffer('1',dict(message_id=mid,channel_id=channel,author_id=author,content=content,attachments=[]))
        self.s.system('1','gateway:'+key,dict(action_type='message_delete',event_at=now,channel_id=channel,message_ids=[mid]))

    def test_grouped_count_growth_adds_only_new_context_even_after_observation_pruning(self):
        now=time.time()
        self.deletion('one','100',now)
        r=self.s.system('1','audit:100',confirmed_action(target_id='20',channel_id='2',event_at=now))
        with patch('utils.scouts.time.time',return_value=now+3):self.s.reconcile_links('1')
        with self.s.connect() as c:c.execute('DELETE FROM scout_observations')
        with patch('utils.scouts.time.time',return_value=now+400):
            self.deletion('two','101',now+400,content='Second message')
            self.s.system('1','audit:100',confirmed_action(target_id='20',channel_id='2',event_at=now,count=2))
        with patch('utils.scouts.time.time',return_value=now+403):self.s.reconcile_links('1')
        saved=self.s.record(self.admin,r['id'])
        self.assertEqual([m['message_id'] for m in saved['data']['messages']],['100','101'])
        self.assertEqual(saved['data']['count'],2)

    def test_incomplete_or_wrong_author_batches_remain_unattached(self):
        now=time.time();self.deletion('wrong','100',now,author='21')
        r=self.s.system('1','audit:100',confirmed_action(target_id='20',channel_id='2',event_at=now,count=2))
        with patch('utils.scouts.time.time',return_value=now+3):self.s.reconcile_links('1')
        self.assertNotIn('messages',self.s.record(self.admin,r['id'])['data'])
        self.deletion('onlyone','101',now)
        with patch('utils.scouts.time.time',return_value=now+3):self.s.reconcile_links('1')
        self.assertNotIn('messages',self.s.record(self.admin,r['id'])['data'])

    def test_bulk_deletion_uses_cached_snapshots_and_retains_missing_content(self):
        now=time.time()
        cached=dict(message_id='100',channel_id='2',author_id='20',content='Cached by Discord client',attachments=[])
        self.s.system('1','gateway:bulk',dict(action_type='message_bulk_delete',event_at=now,channel_id='2',message_ids=['100','101'],cached_messages=[cached]))
        r=self.s.system('1','audit:100',confirmed_action(action_type='message_bulk_delete',channel_id='2',target_id='2',event_at=now,count=2))
        with patch('utils.scouts.time.time',return_value=now+3):self.s.reconcile_links('1')
        saved=self.s.record(self.admin,r['id'])
        self.assertEqual(saved['data']['messages'][0]['content'],'Cached by Discord client')
        self.assertIsNone(saved['data']['messages'][1]['content'])

    def test_edit_picker_only_offers_owned_nonvoid_manual_entries(self):
        mine=self.s.manual(self.scout,dict(type='other',summary='My note',action='Reminder'))
        other=self.s.manual(self.admin,dict(type='other',summary='Other note',action='Reminder'))
        self.s.escalate(self.scout,'Need support')
        self.assertEqual([c['value'] for c in self.s.reference_choices(self.scout,owned=True)],[mine['short_id']])
        self.s.void(self.admin,mine['id'],'Mistake')
        self.assertEqual(self.s.reference_choices(self.scout,owned=True),[])
