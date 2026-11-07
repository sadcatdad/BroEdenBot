import json
import unittest
from tests import test_scouts as fixtures
from utils.scouts import Actor,status_key,status_label,record_title


class ScoutWorkflowTests(unittest.TestCase):
    setUp=fixtures.ScoutStorageTests.setUp
    tearDown=fixtures.ScoutStorageTests.tearDown

    def activity(self,actor=None,source=None):
        return self.s.manual(actor or self.scout,dict(type='other',summary='Context',action='Reminder',outcome='Waiting'),source=source)

    def test_defaults_and_recorded_system_evidence_have_clear_labels(self):
        r=self.activity();e=self.s.escalate(self.scout,'Need help')
        self.assertEqual((r['status'],status_label(r)),('logged','Logged'))
        self.assertEqual((e['status'],status_label(e)),('open','Open'))
        system=self.s.system('1','audit:100',fixtures.confirmed_action())
        self.assertEqual(status_label(system),'Recorded')
        self.assertEqual(record_title(system),'Message deleted')
        self.assertFalse(self.s.can_change_status(self.admin,system))

    def test_own_activity_transitions_preserve_content_history_and_delivery(self):
        r=self.activity();original=dict(r['data'])
        for status in ('needs_follow_up','in_progress','resolved','logged'):
            r=self.s.change_status(self.scout,r['short_id'],status,r['version'],'Status context')
            self.assertEqual(r['status'],status)
            self.assertEqual({k:v for k,v in r['data'].items() if k!='status_update'},original)
            self.assertEqual(r['data']['status_update']['actor_id'],'10')
        revisions=self.s.revisions(self.admin,r['id'])
        self.assertEqual(len(revisions),5)
        self.assertEqual([rev['action'] for rev in revisions[1:]],['status_changed']*4)
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_jobs')),5)
        again=self.s.change_status(self.scout,r['id'],'logged',r['version']-1)
        self.assertEqual(again['version'],r['version'])
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_jobs')),5)
        self.s.edit(self.scout,r['id'],dict(original,summary='Updated context'),r['version'])
        self.assertEqual(self.s.record(self.admin,r['id'])['data']['status_update']['note'],'Status context')

    def test_cross_author_status_permission_is_independent_of_review_and_creation(self):
        r=self.activity(self.admin)
        with self.assertRaises(PermissionError):self.s.change_status(self.scout,r['id'],'resolved',1)
        reviewer=Actor('1','11',frozenset({'scouts.activity.view','scouts.activity.review'}))
        with self.assertRaises(PermissionError):self.s.change_status(reviewer,r['id'],'resolved',1)
        manager=Actor('1','11',frozenset({'scouts.activity.view','scouts.activity.status.manage'}))
        saved=self.s.change_status(manager,r['id'],'resolved',1)
        self.assertEqual(saved['data']['status_update']['actor_id'],'11')
        with self.assertRaises(PermissionError):self.s.change_status(Actor('1','11',manager.permissions,False),r['id'],'logged',2)
        with self.assertRaises(PermissionError):self.s.change_status(Actor('1','11',frozenset({'scouts.activity.status.manage'})),r['id'],'logged',2)

    def test_escalation_resolve_cancel_reopen_never_closes_parent_report(self):
        report=self.s.report('1','80',dict(message_id='100',channel_id='2',content='Context'),'Other','Private','r')
        e=self.s.escalate(self.scout,'Need review',report['short_id'])
        for status in ('in_progress','resolved','open','cancelled','open'):
            e=self.s.change_status(self.scout,e['id'],status,e['version'])
        self.assertEqual(self.s.record(self.admin,report['id'])['status'],'new')
        self.assertEqual(e['data']['related'],report['id'])

    def test_invalid_stale_foreign_void_and_immutable_record_changes_are_rejected(self):
        r=self.activity()
        with self.assertRaises(ValueError):self.s.change_status(self.scout,r['id'],'cancelled',1)
        self.s.change_status(self.scout,r['id'],'in_progress',1)
        with self.assertRaisesRegex(ValueError,'Reload'):self.s.change_status(self.scout,r['id'],'resolved',1)
        with self.assertRaises(ValueError):self.s.change_status(Actor('2','10',self.scout.permissions),r['id'],'resolved',2)
        self.s.void(self.admin,r['id'],'Mistake')
        with self.assertRaises(PermissionError):self.s.change_status(self.admin,r['id'],'logged',3)
        system=self.s.system('1','audit:100',fixtures.confirmed_action())
        with self.assertRaises(ValueError):self.s.change_status(self.admin,system['id'],'resolved',1)

    def test_legacy_statuses_display_and_filter_without_rewriting_evidence(self):
        r=self.activity();e=self.s.escalate(self.scout,'Legacy help')
        with self.s.connect() as c:
            c.execute("UPDATE scout_records SET status='active' WHERE id=?",(r['id'],))
            c.execute("UPDATE scout_records SET status='escalated' WHERE id=?",(e['id'],))
        self.assertEqual(status_label(self.s.record(self.admin,r['id'])),'Logged')
        self.assertEqual(status_key(self.s.record(self.admin,e['id'])),'open')
        self.assertEqual(self.s.listing(self.scout,'manual',status='logged')[0]['id'],r['id'])
        self.assertEqual(self.s.listing(self.scout,'escalation',status='open')[0]['id'],e['id'])
        self.s.change_status(self.scout,e['id'],'resolved',1)
        self.assertEqual(self.s.listing(self.scout,'escalation',status='unresolved'),[])
        self.assertEqual(self.s.rows('SELECT status FROM scout_records WHERE id=?',(r['id'],))[0]['status'],'active')

    def test_new_note_on_same_status_is_saved_once_and_stale_notes_are_rejected(self):
        r=self.activity()
        saved=self.s.change_status(self.scout,r['id'],'logged',1,'More context for this record')
        self.assertEqual(saved['version'],2)
        self.assertEqual(self.s.revisions(self.admin,r['id'])[-1]['action'],'status_note_added')
        self.assertEqual(self.s.change_status(self.scout,r['id'],'logged',1,'More context for this record')['version'],2)
        with self.assertRaises(ValueError):self.s.change_status(self.scout,r['id'],'logged',1,'A different note')
