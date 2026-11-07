"""Public short references must preserve scope, ownership, privacy, and old records."""
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dashboard.rbac import PERMISSION_KEYS
from utils.scouts import Actor, Scouts, DEFAULTS, SCHEMA, short_record_id


class ScoutShortIDTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.db'
        self.s = Scouts(self.path)
        self.s.initialize()
        self.admin = Actor('1','10',frozenset(PERMISSION_KEYS))
        self.s.save_settings(self.admin,dict(DEFAULTS,enabled=True,scout_roles=['5'],activity_channel='9'))

    def tearDown(self):
        self.tmp.cleanup()

    def manual(self, **kw):
        return self.s.manual(self.admin,dict(type='other',summary='Context',action='Reminder'),**kw)

    def test_short_ids_are_sequential_not_uuid_fragments_and_never_reused(self):
        first = self.manual()
        self.assertEqual(first['short_id'],'1')
        self.s.void(self.admin,'1','Mistaken')
        second = self.manual()
        self.assertEqual(second['short_id'],'2')
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(self.s.record(self.admin,'#1')['status'],'void')
        self.assertEqual(self.s.record(self.admin,first['id'])['short_id'],'1')
        self.s.initialize()
        self.assertEqual(self.manual()['short_id'],'3')

    def test_four_character_limit_has_collision_free_capacity_beyond_9999(self):
        ids = [short_record_id(n) for n in range(1,10100)]
        self.assertEqual(len(set(ids)),len(ids))
        self.assertTrue(all(1<=len(s)<=4 for s in ids))
        self.assertEqual(short_record_id(9999),'9999')
        self.assertEqual(short_record_id(10000),'A000')
        maximum = 9999 + 26 * 36**3
        self.assertEqual(short_record_id(maximum),'ZZZZ')
        with self.assertRaises(ValueError):
            short_record_id(maximum+1)

    def test_concurrent_creation_allocates_unique_references_transactionally(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda _: self.manual(),range(32)))
        self.assertEqual({int(r['short_id']) for r in records},set(range(1,33)))
        self.assertEqual(len({r['id'] for r in records}),32)
        self.assertEqual(len(self.s.rows('SELECT * FROM scout_jobs')),32)

    def test_duplicate_submissions_keep_the_same_reference(self):
        a = self.manual(source='same-submission')
        b = self.manual(source='same-submission')
        self.assertEqual((a['id'],a['short_id']),(b['id'],b['short_id']))
        self.assertEqual(self.manual()['short_id'],'2')

    def test_short_references_are_guild_scoped_and_do_not_grant_read_access(self):
        first = self.manual()
        other = Actor('2','20',frozenset(PERMISSION_KEYS))
        self.s.save_settings(other,dict(DEFAULTS,enabled=True,scout_roles=['6'],activity_channel='8'))
        second = self.s.manual(other,dict(type='other',summary='Other server',action='Reminder'))
        self.assertEqual((first['short_id'],second['short_id']),('1','1'))
        self.assertEqual(self.s.record(other,'1')['id'],second['id'])
        with self.assertRaises(ValueError):
            self.s.record(other,first['id'])
        with self.assertRaises(PermissionError):
            self.s.record(Actor('1','30',frozenset()),'1')

    def test_edit_followup_and_reference_urls_use_short_ids_without_rewriting_relations(self):
        parent = self.manual()
        self.s.edit(self.admin,'1',dict(type='other',summary='Updated',action='Reminder'),1)
        child = self.manual(related='#1')
        self.assertEqual(child['data']['related'],parent['id'])
        self.assertEqual(child['related_short_id'],'1')
        ref = self.s.resolve_reference(self.admin,'/scouts/records/1')
        self.assertEqual((ref['record_id'],ref['short_id']),(parent['id'],'1'))
        self.assertEqual(self.s.resolve_reference(self.admin,parent['id'])['short_id'],'1')
        self.assertEqual(self.s.listing(self.admin,'manual',query='#2')[0]['id'],child['id'])

    def test_report_identity_and_outcomes_work_by_short_id_with_independent_permissions(self):
        report = self.s.report('1','80',dict(message_id='100',channel_id='2',content='Context'),'Other','PRIVATE','submission')
        sid = report['short_id']
        restricted = Actor('1','10',self.admin.permissions-{'scouts.reports.identity.view'})
        with self.assertRaises(PermissionError):
            self.s.reporter(restricted,sid)
        self.assertEqual(self.s.reporter(self.admin,sid)['explanation'],'PRIVATE')
        self.assertEqual(self.s.handle(self.admin,sid,'Reviewed')['status'],'handled')
        self.assertNotIn('PRIVATE',json.dumps(self.s.record(restricted,sid)))

    def test_checkin_answers_use_canonical_storage_keys_and_keep_private_boundaries(self):
        request = self.s.request_checkins(self.admin,['10'])[0]
        sid = request['short_id']
        self.s.answer(self.admin,sid,['SECRET']*3)
        self.assertEqual(self.s.answers(self.admin,sid)['answers'],['SECRET']*3)
        stored = self.s.rows('SELECT checkin_id FROM scout_checkin_answers')[0]['checkin_id']
        self.assertEqual(stored,request['id'])
        requester = Actor('1','20',frozenset({'scouts.checkins.request'}))
        with self.assertRaises(PermissionError):
            self.s.answers(requester,sid)
        self.assertEqual(self.s.record(requester,sid)['status'],'answered')
        self.assertEqual(self.s.rows('PRAGMA foreign_key_check'),[])

    def test_ai_cites_short_source_ids_and_short_parent_references(self):
        parent = self.manual()
        child = self.manual(related=parent['short_id'])
        sources = self.s.ai_sources(self.admin,'10')
        self.assertEqual({s['id'] for s in sources},{'1','2'})
        self.assertNotIn(parent['id'],json.dumps(sources))
        self.assertNotIn(child['id'],json.dumps(sources))


class ScoutIDMigrationTests(unittest.TestCase):
    def test_existing_records_receipts_revisions_and_primary_keys_survive_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'old.db'
            # SCHEMA deliberately retains the original record columns; the additive
            # migration installs the short-ID column after opening an older database.
            with sqlite3.connect(path) as c:
                c.executescript(SCHEMA)
                for rid,guild,created in [('a'*16,'1',20),('b'*16,'1',10),('c'*16,'2',5)]:
                    c.execute('INSERT INTO scout_records(id,guild_id,kind,author_id,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (rid,guild,'manual','10',json.dumps(dict(type='other',summary='Original',action='Reminder')),created,created))
                c.execute("INSERT INTO scout_jobs(id,guild_id,record_id,version,destination,message_id,status,created_at) VALUES('delivery','1',?,1,'9','500','sent',20)",('a'*16,))
                c.execute("INSERT INTO scout_revisions(guild_id,record_id,actor_id,action,data,created_at) VALUES('1',?,'10','created','original snapshot',20)",('a'*16,))
                original_jobs = c.execute('SELECT * FROM scout_jobs').fetchall()
                original_revisions = c.execute('SELECT * FROM scout_revisions').fetchall()
            service = Scouts(path)
            service.initialize()
            actor = Actor('1','10',frozenset(PERMISSION_KEYS))
            self.assertEqual(service.record(actor,'1')['id'],'b'*16)
            self.assertEqual(service.record(actor,'2')['id'],'a'*16)
            self.assertEqual(service.record(actor,'a'*16)['short_id'],'2')
            self.assertEqual(service.record(Actor('2','10',actor.permissions),'1')['id'],'c'*16)
            with sqlite3.connect(path) as c:
                self.assertEqual(c.execute('SELECT * FROM scout_jobs').fetchall(),original_jobs)
                self.assertEqual(c.execute('SELECT * FROM scout_revisions').fetchall(),original_revisions)
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
            before = service.rows('SELECT id,guild_id,short_id FROM scout_records ORDER BY id')
            service.initialize()
            self.assertEqual(service.rows('SELECT id,guild_id,short_id FROM scout_records ORDER BY id'),before)
            self.assertEqual(service.rows('SELECT last_number FROM scout_record_counters WHERE guild_id=?',('1',))[0]['last_number'],2)


class ScoutNotificationDesignTests(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_card_is_compact_purple_and_uses_readable_references(self):
        from cogs.scouts import card_embed
        record = dict(id='0123456789abcdef',short_id='24',guild_id='1',kind='system',status='active',created_at=time.time(),
            data=dict(action_type='message_delete',moderator_id='804507544898109450',target_id='1404276031149314090',
                      channel_id='1278258790676037684',confirmed_scout=True,count=1,reason='',
                      attribution='audit actor confirmed; grouped entry may omit exact affected members/messages'))
        embed = card_embed(record,'Scout delivery receipt')
        self.assertEqual(embed.title,'Message deleted')
        self.assertEqual(embed.colour.value,0x9B59B6)
        self.assertLessEqual(len(embed.description.splitlines()),5)
        self.assertNotIn('\n\n',embed.description)
        self.assertIn('• **Scout:** <@804507544898109450>',embed.description)
        self.assertIn('<@1404276031149314090>',embed.description)
        self.assertIn('<#1278258790676037684>',embed.description)
        self.assertIn('Not captured.',embed.description)
        self.assertNotIn('Attribution',embed.description)
        self.assertNotIn('None supplied',embed.description)
        self.assertNotIn('Grouped audit count',embed.description)
        self.assertEqual(embed.footer.text,'#24 · Scout Operations')
        self.assertNotIn(record['id'],str(embed.to_dict()))
        self.assertNotIn('Scout delivery',embed.footer.text)

    async def test_short_persistent_controls_and_legacy_controls_restore(self):
        from cogs.scouts import ScoutControl
        for rid in ('1','24','9999','A000','abcdef0123456789'):
            item = ScoutControl('add',rid,'1')
            match = ScoutControl.__discord_ui_compiled_template__.fullmatch(item.custom_id)
            self.assertIsNotNone(match)
            restored = await ScoutControl.from_custom_id(None,item,match)
            self.assertEqual(restored.rid,rid)
