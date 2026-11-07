"""Scout Operations domain/storage. Private submissions never enter public records/jobs.

Adapters construct Actors from current membership and RBAC. All public methods scope
IDs to a guild and enforce independent capabilities. SQLite transactions atomically
save workflow changes, revisions and outbox jobs; no Discord I/O occurs here.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from utils.settings import get_setting, settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection
from utils.scout_forms import FORM_DEFAULTS, validate_forms, answers_for

ACTIVITY_TYPES = ('rule reminder', 'de-escalation', 'message removal', 'timeout', 'concern', 'other')
ACTION_TYPES = ('message_delete', 'message_bulk_delete', 'timeout', 'member_move', 'member_disconnect')
CORRELATION_RETENTION_SECONDS = 300
CARD_LAYOUT_VERSION = 'compact-purple-v3-statuses'
WORKFLOW_STATUSES = {
    'manual': {'logged':'Logged', 'needs_follow_up':'Needs follow-up', 'in_progress':'In progress', 'resolved':'Resolved'},
    'escalation': {'open':'Open', 'in_progress':'In progress', 'resolved':'Resolved', 'cancelled':'Cancelled'},
}
STATUS_HELP = {
    'logged':'Saved activity; no follow-up has been flagged.',
    'needs_follow_up':'More attention or a response is needed.',
    'in_progress':'Someone is actively working on this.',
    'resolved':'No further action is needed. Reopen it if the situation changes.',
    'open':'Higher staff have been asked for help; review is still needed.',
    'cancelled':'This request for help is no longer needed.',
    'recorded':'An automatic evidence record, not an open task.',
    'void':'Marked as a mistaken entry; its history is retained.',
}


def status_key(record):
    kind, value = record['kind'], record['status']
    if kind == 'system':
        return 'recorded'
    return {('manual','active'):'logged', ('escalation','escalated'):'open'}.get((kind,value),value)


def status_label(record):
    key = status_key(record)
    return WORKFLOW_STATUSES.get(record['kind'],{}).get(key, {'void':'Voided','recorded':'Recorded'}.get(key,key.replace('_',' ').title()))


def record_title(record):
    data = record['data']
    if record['kind'] != 'system':
        return data.get('summary') or data.get('category') or 'Requested check-in'
    action = data.get('action_type','')
    count = max(1,int(data.get('count') or 1))
    if action in ('message_delete','message_bulk_delete'):
        return 'Message deleted' if count == 1 else f'{count} messages deleted'
    if action == 'timeout':
        return 'Timeout removed' if not data.get('after') else 'Timeout updated' if data.get('before') else 'Timeout applied'
    if action in ('member_move','member_disconnect'):
        verb = 'moved in voice' if action=='member_move' else 'disconnected from voice'
        return 'Member '+verb if count==1 else f'{count} members '+verb
    return action.replace('_',' ').capitalize()
REPORT_CATEGORIES = ('Harassment', 'Hate or discrimination', 'Spam', 'Unsafe content', 'Rule concern', 'Other')
DEFAULTS = dict(
    enabled=False, automatic_enabled=False, scout_roles=[], activity_channel='', moderation_channel='',
    report_roles=[], escalation_roles=[], action_types=list(ACTION_TYPES), monitored_channels=[],
    monitored_categories=[], excluded_channels=[], excluded_categories=[], retention_days=7,
    questions=['What have you handled since your last check-in? “Nothing to report” is acceptable.',
               'Have you encountered difficulties or concerns?', 'What help or feedback would be useful?'],
    ai_drafts=False, ai_summaries=False, report_cooldown=60, notification_cooldown=300,
    forms=copy.deepcopy(FORM_DEFAULTS),
)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS scout_schema(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scout_record_counters(guild_id TEXT PRIMARY KEY, last_number INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scout_settings(guild_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scout_records(
 id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, kind TEXT NOT NULL, author_id TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active', data TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, source_key TEXT, UNIQUE(guild_id,source_key));
CREATE INDEX IF NOT EXISTS scout_records_scope ON scout_records(guild_id,kind,created_at);
CREATE TABLE IF NOT EXISTS scout_revisions(
 id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, record_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 action TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scout_report_private(
 report_id TEXT PRIMARY KEY REFERENCES scout_records(id), guild_id TEXT NOT NULL,
 reporter_id TEXT NOT NULL, explanation TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS scout_report_rate ON scout_report_private(guild_id,reporter_id);
CREATE TABLE IF NOT EXISTS scout_checkin_answers(
 checkin_id TEXT PRIMARY KEY REFERENCES scout_records(id), guild_id TEXT NOT NULL,
 recipient_id TEXT NOT NULL, answers TEXT NOT NULL, submitted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scout_buffer(
 guild_id TEXT NOT NULL, message_id TEXT NOT NULL, channel_id TEXT NOT NULL, data TEXT NOT NULL,
 observed_at REAL NOT NULL, PRIMARY KEY(guild_id,message_id));
CREATE INDEX IF NOT EXISTS scout_buffer_expiry ON scout_buffer(observed_at);
CREATE TABLE IF NOT EXISTS scout_observations(
 guild_id TEXT NOT NULL, source_key TEXT NOT NULL, data TEXT NOT NULL, observed_at REAL NOT NULL,
 PRIMARY KEY(guild_id,source_key));
CREATE INDEX IF NOT EXISTS scout_observations_expiry ON scout_observations(observed_at);
CREATE TABLE IF NOT EXISTS scout_jobs(
 id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, record_id TEXT NOT NULL REFERENCES scout_records(id),
 version INTEGER NOT NULL, purpose TEXT NOT NULL DEFAULT 'card', status TEXT NOT NULL DEFAULT 'pending',
 destination TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
 attempted_at REAL, checked_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', UNIQUE(record_id,version,purpose));
CREATE INDEX IF NOT EXISTS scout_jobs_queue ON scout_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS scout_jobs_receipt ON scout_jobs(guild_id,destination,message_id);
CREATE TABLE IF NOT EXISTS scout_previews(token TEXT PRIMARY KEY, guild_id TEXT NOT NULL, actor_id TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scout_runtime(guild_id TEXT PRIMARY KEY, heartbeat REAL NOT NULL, data TEXT NOT NULL);
'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def required(text, label, limit=4000):
    value = str(text or '').strip()
    if not value or len(value) > limit:
        raise ValueError(f'{label} is required and must be at most {limit} characters.')
    return value


def short_record_id(number):
    """Sequential public references: 1..9999, then A000..ZZZZ. Never truncate/reuse."""
    if 1 <= number <= 9999:
        return str(number)
    prefix, suffix = divmod(number - 10000, 36 ** 3)
    if not 0 <= prefix < 26:
        raise ValueError('This server has exhausted its four-character Scout record references.')
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    digits = ''
    for _ in range(3):
        suffix, digit = divmod(suffix, 36)
        digits = alphabet[digit] + digits
    return chr(ord('A') + prefix) + digits


@dataclass(frozen=True)
class Actor:
    guild_id: str
    user_id: str
    permissions: frozenset[str]
    eligible: bool = True

    def require(self, key):
        if not self.eligible or 'scouts.' + key not in self.permissions:
            raise PermissionError('You do not have the required Scout capability or current eligibility.')


class Scouts:
    def __init__(self, path=None):
        self.path = Path(path) if path else settings_database_path()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=15, factory=AutoClosingSQLiteConnection)
        c.row_factory = sqlite3.Row
        configure_sync_connection(c)
        c.execute('PRAGMA foreign_keys=ON')
        return c

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)
            c.execute('BEGIN IMMEDIATE')
            if 'short_id' not in {r['name'] for r in c.execute('PRAGMA table_info(scout_records)')}:
                c.execute('ALTER TABLE scout_records ADD COLUMN short_id TEXT CHECK(short_id IS NULL OR length(short_id) BETWEEN 1 AND 4)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS scout_record_short_id ON scout_records(guild_id,short_id)')
            c.execute('CREATE INDEX IF NOT EXISTS scout_record_missing_short_id ON scout_records(guild_id,created_at,id) WHERE short_id IS NULL')
            # Add labels without rewriting primary keys, links, revisions or receipts.
            # Repeat for any rows created by an older bot during a rolling update.
            for row in c.execute('SELECT id,guild_id FROM scout_records WHERE short_id IS NULL ORDER BY guild_id,created_at,id').fetchall():
                c.execute('UPDATE scout_records SET short_id=? WHERE id=?', (self._next_short_id(c, row['guild_id']), row['id']))
            if 'checked_at' not in {r['name'] for r in c.execute('PRAGMA table_info(scout_jobs)')}:
                c.execute('ALTER TABLE scout_jobs ADD COLUMN checked_at REAL NOT NULL DEFAULT 0')
            c.execute('INSERT OR IGNORE INTO scout_schema VALUES(1,?)', (time.time(),))
            c.execute('INSERT OR IGNORE INTO scout_schema VALUES(2,?)', (time.time(),))
            c.execute('INSERT OR IGNORE INTO scout_schema VALUES(3,?)', (time.time(),))
            c.execute('INSERT OR IGNORE INTO scout_schema VALUES(4,?)', (time.time(),))

    @staticmethod
    def _next_short_id(c, guild):
        c.execute('INSERT OR IGNORE INTO scout_record_counters VALUES(?,0)', (str(guild),))
        c.execute('UPDATE scout_record_counters SET last_number=last_number+1 WHERE guild_id=?', (str(guild),))
        number = c.execute('SELECT last_number FROM scout_record_counters WHERE guild_id=?', (str(guild),)).fetchone()[0]
        return short_record_id(number)

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def settings(self, guild):
        rows = self.rows('SELECT data FROM scout_settings WHERE guild_id=?', (str(guild),))
        return dict(copy.deepcopy(DEFAULTS), **(json.loads(rows[0]['data']) if rows else {}))

    def save_settings(self, actor, values):
        actor.require('settings.manage')
        result = self.settings(actor.guild_id)
        for key, default in DEFAULTS.items():
            value = values.get(key, default)
            if key == 'forms':
                result[key] = validate_forms(values.get(key, result[key]))
                continue
            if isinstance(default, bool):
                result[key] = value is True or value in ('on', 'true', '1')
            elif isinstance(default, int):
                try:
                    number = int(value)
                except (ValueError, TypeError):
                    raise ValueError(f'Enter a whole number for {key}.') from None
                if not 0 <= number <= (90 if key == 'retention_days' else 86400) or (key == 'retention_days' and number == 0):
                    raise ValueError(f'{key} is outside its supported range.')
                result[key] = number
            elif isinstance(default, list):
                if not isinstance(value, list):
                    raise ValueError(f'Select values for {key}.')
                if key == 'questions':
                    if not 1 <= len(value) <= 3:
                        raise ValueError('Provide one to three check-in questions.')
                    result[key] = [required(q, 'Question', 200) for q in value]
                elif key == 'action_types':
                    if set(value) - set(ACTION_TYPES):
                        raise ValueError('Unknown automatic action type.')
                    result[key] = list(dict.fromkeys(value))
                else:
                    if any(not str(v).isdigit() for v in value):
                        raise ValueError(f'Choose valid Discord IDs for {key}.')
                    result[key] = list(dict.fromkeys(str(v) for v in value))
            else:
                if value and not str(value).isdigit():
                    raise ValueError('Choose a valid destination channel.')
                result[key] = str(value or '')
        if result['enabled'] and (not result['scout_roles'] or not result['activity_channel']):
            raise ValueError('Choose Scout eligibility roles and a shared activity channel before enabling.')
        if result['enabled'] and (not self.settings(actor.guild_id)['enabled'] or not self.settings(actor.guild_id)['automatic_enabled'] and result['automatic_enabled']):
            result['capture_since'] = time.time()
        with self.connect() as c:
            c.execute('INSERT OR REPLACE INTO scout_settings VALUES(?,?)', (actor.guild_id, encode(result)))
        return result

    def enabled(self, guild):
        if not self.settings(guild)['enabled']:
            raise ValueError('Scout Operations is disabled. Configure it in The Garden first.')

    @staticmethod
    def unpack(row):
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def _record(self, c, guild, record_id):
        reference = str(record_id).strip().lstrip('#')
        column = 'short_id' if re.fullmatch(r'[A-Za-z0-9]{1,4}', reference) else 'id'
        value = reference.upper() if column == 'short_id' else reference.lower()
        r = c.execute(f'SELECT * FROM scout_records WHERE guild_id=? AND {column}=?', (str(guild), value)).fetchone()
        if r is None:
            raise ValueError('Record not found in this server.')
        record = self.unpack(r)
        if not record['short_id']:
            if not c.in_transaction:
                c.execute('BEGIN IMMEDIATE')
            saved = c.execute('SELECT short_id FROM scout_records WHERE id=?', (record['id'],)).fetchone()[0]
            record['short_id'] = saved or self._next_short_id(c, guild)
            c.execute('UPDATE scout_records SET short_id=? WHERE id=? AND short_id IS NULL', (record['short_id'], record['id']))
        related = record['data'].get('related')
        parent = c.execute('SELECT short_id FROM scout_records WHERE guild_id=? AND id=?', (str(guild), related)).fetchone() if related else None
        record['related_short_id'] = parent['short_id'] if parent else ''
        return record

    def _reference(self, c, actor, value):
        """Resolve IDs/card links locally; ordinary message links become evidence.

        Never fetch arbitrary URLs or use a message link as an unscoped record ID.
        A receipt lookup verifies the guild, destination and exact message together.
        """
        value = str(value or '').strip()
        if value.startswith('<') and value.endswith('>'):
            value = value[1:-1].strip()
        if not value:
            return dict(record_id='', short_id='', evidence='')
        if len(value) > 500:
            raise ValueError('The related record or message link must be at most 500 characters.')
        raw_id = value.lstrip('#')
        if re.fullmatch(r'(?:[a-fA-F0-9]{16}|[A-Za-z0-9]{1,4})', raw_id):
            record = self._record(c, actor.guild_id, raw_id)
            self.read_allowed(actor, record)
            if record['kind'] == 'checkin':
                raise ValueError('Private check-ins cannot be linked into shared activity.')
            return dict(record_id=record['id'], short_id=record['short_id'], evidence='')
        try:
            url = urlsplit(value)
        except ValueError:
            raise ValueError('Use a Scout record ID, a Garden record link, or a Discord message link.') from None
        if url.scheme in ('https', 'http') and url.netloc in (
            'discord.com', 'www.discord.com', 'canary.discord.com', 'ptb.discord.com',
            'discordapp.com', 'www.discordapp.com',
        ):
            match = re.fullmatch(r'/channels/(\d+)/(\d+)/(\d+)/?', url.path)
            if not match:
                raise ValueError('Paste a full Discord message link, including its server, channel, and message IDs.')
            guild, channel, message = match.groups()
            if guild != str(actor.guild_id):
                raise ValueError('The message link belongs to another server. Use a message from this server.')
            evidence = f'https://discord.com/channels/{guild}/{channel}/{message}'
            row = c.execute('''SELECT r.* FROM scout_records r JOIN scout_jobs j ON j.record_id=r.id
                WHERE r.guild_id=? AND j.guild_id=? AND j.destination=? AND j.message_id=?
                AND j.status='sent' ORDER BY j.created_at DESC LIMIT 1''',
                (actor.guild_id, actor.guild_id, channel, message)).fetchone()
            if row:
                record = self._record(c, actor.guild_id, row['id'])
                self.read_allowed(actor, record)
                if record['kind'] == 'checkin':
                    raise ValueError('Private check-ins cannot be linked into shared activity.')
                return dict(record_id=record['id'], short_id=record['short_id'], evidence=evidence)
            return dict(record_id='', short_id='', evidence=evidence)
        public_url = str(get_setting('DASHBOARD_PUBLIC_URL', '') or os.getenv('DASHBOARD_PUBLIC_URL', '')).rstrip('/')
        public = urlsplit(public_url)
        path = url.path
        if url.scheme or url.netloc:
            if not public.netloc or url.scheme != public.scheme or url.netloc != public.netloc:
                raise ValueError('Use a Scout record ID, this Garden’s record link, or a Discord message link.')
            prefix = public.path.rstrip('/')
            if not path.startswith(prefix + '/'):
                raise ValueError('Use a link to a Scout record in this Garden.')
            path = path[len(prefix):]
        match = re.fullmatch(r'/scouts/records/([a-fA-F0-9]{16}|[A-Za-z0-9]{1,4})/?', path)
        if match:
            return self._reference(c, actor, match[1])
        raise ValueError('Use a Scout record ID, a Garden record link, or a Discord message link. Other evidence links belong in Evidence links.')

    def resolve_reference(self, actor, value):
        actor.require('activity.create')
        with self.connect() as c:
            return self._reference(c, actor, value)

    @staticmethod
    def _add_evidence(data, link):
        if link and link not in data.get('evidence', '').splitlines():
            data['evidence'] = '\n'.join(v for v in (data.get('evidence', ''), link) if v)
            if len(data['evidence']) > 2000:
                raise ValueError('Evidence links must fit within 2,000 characters.')

    def _revision(self, c, record, actor, action):
        c.execute('INSERT INTO scout_revisions(guild_id,record_id,actor_id,action,data,created_at) VALUES(?,?,?,?,?,?)',
                  (record['guild_id'], record['id'], str(actor), action, encode(record), time.time()))

    def _job(self, c, record, purpose='card'):
        if record['kind'] == 'system' and not self.confirmed_scout_action(record['data'], record['source_key']):
            if not purpose.startswith('layout:') or not c.execute(
                "SELECT 1 FROM scout_jobs WHERE record_id=? AND status='sent' AND message_id!=''",
                (record['id'],),
            ).fetchone():
                return
        config = self.settings(record['guild_id'])
        destination = (record['data']['recipient_id'] if record['kind'] == 'checkin' else
                       config['moderation_channel'] or config['activity_channel'] if record['kind'] == 'system' else config['activity_channel'])
        c.execute('INSERT OR IGNORE INTO scout_jobs(id,guild_id,record_id,version,purpose,destination,created_at) VALUES(?,?,?,?,?,?,?)',
                  (uuid.uuid4().hex, record['guild_id'], record['id'], record['version'], purpose, destination, time.time()))

    def _create(self, c, guild, kind, author, data, status='active', source=None, notify=True):
        if not c.in_transaction:
            c.execute('BEGIN IMMEDIATE')
        now = time.time()
        rid = uuid.uuid4().hex[:16]
        short_id = self._next_short_id(c, guild)
        c.execute('INSERT INTO scout_records(id,guild_id,kind,author_id,status,data,created_at,updated_at,source_key,short_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (rid, str(guild), kind, str(author), status, encode(data), now, now, source, short_id))
        r = self._record(c, guild, rid)
        self._revision(c, r, author, 'created')
        if notify:
            self._job(c, r)
        return r

    def _update(self, c, record, actor, action):
        record['version'] += 1
        record['updated_at'] = time.time()
        c.execute('UPDATE scout_records SET status=?,data=?,version=?,updated_at=? WHERE id=? AND guild_id=?',
                  (record['status'], encode(record['data']), record['version'], record['updated_at'], record['id'], record['guild_id']))
        self._revision(c, record, actor, action)
        self._job(c, record)
        return record

    @staticmethod
    def read_allowed(actor, record):
        kind = record['kind']
        if kind == 'system':
            actor.require('moderation.view')
        elif kind == 'checkin':
            if 'scouts.checkins.request' in actor.permissions or 'scouts.checkins.view_all' in actor.permissions:
                actor.require('checkins.request' if 'scouts.checkins.request' in actor.permissions else 'checkins.view_all')
            else:
                actor.require('checkins.respond')
                if record['data']['recipient_id'] != actor.user_id:
                    raise PermissionError('This check-in belongs to another Scout.')
        else:
            actor.require('activity.view')

    def record(self, actor, record_id):
        with self.connect() as c:
            r = self._record(c, actor.guild_id, record_id)
        self.read_allowed(actor, r)
        r['deliveries'] = self.rows('SELECT id,purpose,status,destination,message_id,error FROM scout_jobs WHERE guild_id=? AND record_id=? ORDER BY created_at', (actor.guild_id, r['id']))
        return r

    def listing(self, actor, kind, *, page=1, query='', status='', scout='', days=30):
        cap = {'manual': 'activity.view', 'report': 'activity.view', 'escalation': 'activity.view', 'system': 'moderation.view', 'checkin': None}.get(kind)
        if kind not in ('manual', 'report', 'escalation', 'system', 'checkin'):
            raise ValueError('Unknown record type.')
        if cap:
            actor.require(cap)
        elif not actor.eligible or not any('scouts.checkins.' + x in actor.permissions for x in ('request', 'respond', 'view_all')):
            raise PermissionError('No check-in access.')
        sql = "SELECT scout_records.*,(SELECT p.short_id FROM scout_records p WHERE p.guild_id=scout_records.guild_id AND p.id=json_extract(scout_records.data,'$.related')) related_short_id FROM scout_records WHERE guild_id=? AND kind=? AND created_at>=?"
        args = [actor.guild_id, kind, time.time() - max(1, min(int(days), 3650)) * 86400]
        if kind == 'system':
            # Also exclude pre-fix gateway/non-Scout history from the active feed
            # and AI sources, without destroying existing evidence or receipts.
            sql += """ AND json_extract(data,'$.confirmed_scout')=1
                AND coalesce(json_extract(data,'$.moderator_is_bot'),0)=0
                AND json_type(data,'$.scout_roles')='array'
                AND json_array_length(data,'$.scout_roles')>0
                AND source_key='audit:' || json_extract(data,'$.audit_id')
                AND author_id=json_extract(data,'$.moderator_id')"""
        if kind == 'checkin' and not ({'scouts.checkins.request', 'scouts.checkins.view_all'} & actor.permissions):
            sql += " AND json_extract(data,'$.recipient_id')=?"
            args.append(actor.user_id)
        if query:
            sql += ' AND (short_id=? OR id LIKE ? OR data LIKE ?)'
            args.extend([query.lstrip('#').upper(), '%' + query[:100] + '%', '%' + query[:100] + '%'])
        if status:
            aliases = (['logged','active'] if kind=='manual' and status=='logged' else
                       ['open','escalated'] if kind=='escalation' and status=='open' else
                       ['open','escalated','in_progress'] if kind=='escalation' and status=='unresolved' else [status])
            sql += ' AND status IN (' + ','.join('?' for _ in aliases) + ')'
            args.extend(aliases)
        if scout:
            sql += " AND json_extract(data,'$.recipient_id')=?" if kind == 'checkin' else ' AND author_id=?'
            args.append(str(scout))
        sql += ' ORDER BY created_at DESC,id DESC LIMIT 26 OFFSET ?'
        args.append((max(1, min(int(page), 100000)) - 1) * 25)
        return [self.unpack(r) for r in self.rows(sql, args)]

    def manual(self, actor, values, related='', source=None):
        actor.require('activity.create')
        self.enabled(actor.guild_id)
        data = self.manual_values(values, self.settings(actor.guild_id)['forms']['activity'])
        source = f'manual:{actor.user_id}:{source}' if source else None
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if source:
                existing = c.execute('SELECT id FROM scout_records WHERE guild_id=? AND source_key=?', (actor.guild_id, source)).fetchone()
                if existing:
                    return self._record(c, actor.guild_id, existing['id'])
            if related:
                reference = self._reference(c, actor, related)
                if reference['record_id']:
                    data['related'] = reference['record_id']
                self._add_evidence(data, reference['evidence'])
            return self._create(c, actor.guild_id, 'manual', actor.user_id, data, status='logged', source=source)

    @staticmethod
    def manual_values(values, fields=None):
        kind = values.get('type', 'other')
        if kind not in ACTIVITY_TYPES:
            raise ValueError('Select an activity type.')
        data = dict(type=kind, summary=required(values.get('summary'), 'What happened'), action=required(values.get('action'), 'Action taken'))
        for key in ('members', 'channel', 'evidence', 'outcome', 'incident_at'):
            data[key] = str(values.get(key) or '').strip()[:4000 if key=='outcome' else 2000]
        fields = fields or FORM_DEFAULTS['activity']
        data['form_answers'] = answers_for(fields, values)
        data['form_labels'] = {f['key']:f['label'] for f in fields if f['enabled']}
        return data

    def edit(self, actor, rid, values, version):
        actor.require('activity.create')
        self.enabled(actor.guild_id)
        data = self.manual_values(values, self.settings(actor.guild_id)['forms']['activity'])
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = self._record(c, actor.guild_id, rid)
            if r['kind'] != 'manual' or r['author_id'] != actor.user_id or r['status'] == 'void':
                raise PermissionError('Only the author can edit a manual entry that has not been voided.')
            if r['version'] != int(version):
                raise ValueError('This record changed. Reload before editing.')
            if r['data'].get('related'):
                data['related'] = r['data']['related']
            if r['data'].get('status_update'):
                data['status_update'] = r['data']['status_update']
            r['data'] = data
            return self._update(c, r, actor.user_id, 'edited')

    def void(self, actor, rid, reason):
        actor.require('activity.void')
        reason = required(reason, 'Void reason')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = self._record(c, actor.guild_id, rid)
            if r['kind'] != 'manual':
                raise ValueError('Only manual activity can be voided.')
            r['status'] = 'void'
            r['data']['void_reason'] = reason
            return self._update(c, r, actor.user_id, 'voided')

    @staticmethod
    def can_change_status(actor, record):
        return bool(actor.eligible and record['kind'] in WORKFLOW_STATUSES and record['status']!='void' and (
            'scouts.activity.status.manage' in actor.permissions or
            (record['author_id']==actor.user_id and 'scouts.activity.create' in actor.permissions)))

    def change_status(self, actor, rid, status, version, note=''):
        self.enabled(actor.guild_id)
        note = str(note or '').strip()
        if len(note)>1000:
            raise ValueError('Status notes must be at most 1,000 characters.')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            record = self._record(c,actor.guild_id,rid)
            self.read_allowed(actor,record)
            if record['kind'] not in WORKFLOW_STATUSES:
                raise ValueError('Only activity and help requests have editable workflow statuses.')
            if not self.can_change_status(actor,record):
                raise PermissionError('Update your own entries, or ask staff with Manage activity statuses permission.')
            if status not in WORKFLOW_STATUSES[record['kind']]:
                raise ValueError('Choose a valid status for this entry.')
            previous = status_key(record)
            if status == previous and (not note or note==record['data'].get('status_update',{}).get('note')):
                return record
            if record['version'] != int(version):
                raise ValueError('This entry changed. Reload it before changing its status.')
            record['status'] = status
            record['data']['status_update'] = dict(previous=previous,status=status,actor_id=actor.user_id,at=time.time(),note=note)
            return self._update(c,record,actor.user_id,'status_changed' if status!=previous else 'status_note_added')

    def revisions(self, actor, rid):
        actor.require('activity.review')
        r = self.record(actor, rid)
        return self.rows('SELECT actor_id,action,data,created_at FROM scout_revisions WHERE guild_id=? AND record_id=? ORDER BY id', (actor.guild_id, r['id']))

    def report(self, guild, reporter, snapshot, category, explanation, source, values=None):
        # Membership/message visibility are checked live by the Discord adapter twice.
        self.enabled(guild)
        if category not in REPORT_CATEGORIES:
            raise ValueError('Select a report category.')
        explanation = required(explanation, 'Explanation')
        config = self.settings(guild)
        private_answers=answers_for(config['forms']['report'],dict(values or {},explanation=explanation))
        if private_answers:
            explanation += '\n\n' + '\n\n'.join(a['label']+'\n'+a['answer'] for a in private_answers)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            duplicate = c.execute('SELECT r.id FROM scout_records r JOIN scout_report_private p ON p.report_id=r.id WHERE r.guild_id=? AND p.reporter_id=? AND (r.source_key=? OR (json_extract(r.data,\'$.message.message_id\')=? AND r.created_at>?))',
                                  (str(guild), str(reporter), source, str(snapshot['message_id']), time.time() - 300)).fetchone()
            if duplicate:
                return self._record(c, guild, duplicate['id'])
            recent = c.execute('SELECT 1 FROM scout_report_private p JOIN scout_records r ON r.id=p.report_id WHERE p.guild_id=? AND p.reporter_id=? AND r.created_at>?',
                               (str(guild), str(reporter), time.time() - config['report_cooldown'])).fetchone()
            if recent:
                raise ValueError('Please wait before submitting another report.')
            ping = c.execute("SELECT 1 FROM scout_records WHERE guild_id=? AND kind='report' AND json_extract(data,'$.message.message_id')=? AND created_at>?",
                             (str(guild), str(snapshot['message_id']), time.time() - config['notification_cooldown'])).fetchone() is None
            r = self._create(c, guild, 'report', '', dict(category=category, message=snapshot, notify_roles=ping), 'new', source)
            c.execute('INSERT INTO scout_report_private VALUES(?,?,?,?)', (r['id'], str(guild), str(reporter), explanation))
            return r

    def reporter(self, actor, rid):
        actor.require('reports.identity.view')
        # Deliberately separate from every public representation, audit and AI builder.
        with self.connect() as c:
            r = self._record(c, actor.guild_id, rid)
            if r['kind'] != 'report':
                raise ValueError('Not a member report.')
            row = c.execute('SELECT reporter_id,explanation FROM scout_report_private WHERE report_id=? AND guild_id=?', (r['id'], actor.guild_id)).fetchone()
            return dict(row) if row else None

    def handle(self, actor, rid, outcome, values=None):
        actor.require('reports.handle')
        self.enabled(actor.guild_id)
        outcome = required(outcome, 'Outcome')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = self._record(c, actor.guild_id, rid)
            if r['kind'] != 'report':
                raise ValueError('Select a member report.')
            if r['status'] == 'handled':
                raise ValueError('This report is already handled.')
            r['status'] = 'handled'
            r['data'].update(outcome=outcome, handler_id=actor.user_id, handled_at=time.time())
            fields=self.settings(actor.guild_id)['forms']['outcome']
            r['data']['outcome_answers']=answers_for(fields,dict(values or {},reason=outcome))
            r['data'].setdefault('form_labels',{})['outcome']=fields[0]['label']
            return self._update(c, r, actor.user_id, 'handled')

    def escalate(self, actor, explanation, related='', source=None, values=None):
        actor.require('activity.create')
        self.enabled(actor.guild_id)
        explanation = required(explanation, 'Help needed')
        source = f'escalation:{actor.user_id}:{source}' if source else None
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if source:
                row = c.execute('SELECT id FROM scout_records WHERE guild_id=? AND source_key=?', (actor.guild_id, source)).fetchone()
                if row:
                    return self._record(c, actor.guild_id, row['id'])
            reference = self._reference(c, actor, related)
            data = dict(summary=explanation, related=reference['record_id'])
            fields=self.settings(actor.guild_id)['forms']['escalation']
            data['form_answers'] = answers_for(fields, dict(values or {}, reason=explanation))
            data['form_labels']={'summary':fields[0]['label']}
            self._add_evidence(data, reference['evidence'])
            return self._create(c, actor.guild_id, 'escalation', actor.user_id, data, 'open', source)

    def reference_choices(self, actor, query='', owned=False):
        actor.require('activity.create')
        rows=[]
        for kind in (['manual'] if owned else ['manual','report','system','escalation']):
            try:
                candidates=self.listing(actor,kind,query=query[:100],days=3650,scout=actor.user_id if owned else '')
            except PermissionError:
                continue
            for r in candidates:
                if owned and r['status']=='void':
                    continue
                label=r['data'].get('summary') or r['data'].get('category') or r['data'].get('action_type','').replace('_',' ') or r['kind']
                kind_label={'manual':'Activity','report':'Member report','system':'Discord action','escalation':'Help request'}[r['kind']]
                rows.append((r['created_at'],dict(name=f"#{r['short_id']} · {kind_label} · {str(label).replace(chr(10),' ')[:65]}",value=r['short_id'])))
        return [r[1] for r in sorted(rows,key=lambda r:r[0],reverse=True)[:25]]

    def request_checkins(self, actor, recipients, questions=None, deadline=None, source=None):
        actor.require('checkins.request')
        self.enabled(actor.guild_id)
        questions = questions or self.settings(actor.guild_id)['questions']
        if not 1 <= len(questions) <= 3:
            raise ValueError('Provide one to three questions.')
        questions = [required(q, 'Question', 200) for q in questions]
        if deadline and float(deadline) <= time.time():
            raise ValueError('The deadline must be in the future.')
        # Adapter previews and revalidates these members before committing.
        recipients = list(dict.fromkeys(str(u) for u in recipients))
        if not recipients or len(recipients) > 500 or any(not u.isdigit() for u in recipients):
            raise ValueError('Select between 1 and 500 current Scouts.')
        result = []
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for uid in recipients:
                key = f'checkin:{actor.user_id}:{source}:{uid}' if source else None
                existing = c.execute('SELECT id FROM scout_records WHERE guild_id=? AND source_key=?', (actor.guild_id, key)).fetchone() if key else None
                result.append(self._record(c, actor.guild_id, existing['id']) if existing else self._create(c, actor.guild_id, 'checkin', actor.user_id, dict(recipient_id=uid, questions=questions, deadline=deadline), 'pending', key))
        return result

    def answer(self, actor, rid, answers):
        actor.require('checkins.respond')
        self.enabled(actor.guild_id)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = self._record(c, actor.guild_id, rid)
            if r['kind'] != 'checkin' or r['data']['recipient_id'] != actor.user_id:
                raise PermissionError('This check-in belongs to another Scout.')
            if r['status'] != 'pending':
                raise ValueError('This check-in is already answered or cancelled.')
            if len(answers) != len(r['data']['questions']):
                raise ValueError('Answer each question.')
            answers = [required(a, 'Answer') for a in answers]
            c.execute('INSERT INTO scout_checkin_answers VALUES(?,?,?,?,?)', (r['id'], actor.guild_id, actor.user_id, encode(answers), time.time()))
            r['status'] = 'answered'
            return self._update(c, r, actor.user_id, 'answered')

    def answers(self, actor, rid):
        r = self.record(actor, rid)
        if r['kind'] != 'checkin':
            raise ValueError('Select a check-in.')
        if r['data']['recipient_id'] == actor.user_id and 'scouts.checkins.respond' in actor.permissions:
            actor.require('checkins.respond')
        else:
            actor.require('checkins.view_all')
        rows = self.rows('SELECT answers,submitted_at FROM scout_checkin_answers WHERE guild_id=? AND checkin_id=?', (actor.guild_id, r['id']))
        return dict(answers=json.loads(rows[0]['answers']), submitted_at=rows[0]['submitted_at']) if rows else None

    def checkin_action(self, actor, rid, action):
        actor.require('checkins.request')
        self.enabled(actor.guild_id)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r = self._record(c, actor.guild_id, rid)
            if r['kind'] != 'checkin' or r['status'] != 'pending':
                raise ValueError('Only pending check-ins can be cancelled or nudged.')
            if action == 'cancel':
                r['status'] = 'cancelled'
            elif action == 'nudge':
                if time.time() - r['data'].get('nudged_at', 0) < 60:
                    raise ValueError('Wait a minute before nudging again.')
                r['data']['nudged_at'] = time.time()
            else:
                raise ValueError('Unknown check-in action.')
            return self._update(c, r, actor.user_id, action)

    def capture_allowed(self, guild, channel, category='', parent=''):
        cfg = self.settings(guild)
        channels = {str(channel), str(parent)}
        destinations = {cfg['activity_channel'], cfg['moderation_channel']} - {''}
        return bool(cfg['enabled'] and cfg['automatic_enabled'] and not channels & (set(cfg['excluded_channels']) | destinations)
                    and str(category) not in cfg['excluded_categories']
                    and (channels & set(cfg['monitored_channels']) or str(category) in cfg['monitored_categories']))

    def buffer(self, guild, snapshot, category='', parent=''):
        if not self.capture_allowed(guild, snapshot['channel_id'], category, parent):
            return
        with self.connect() as c:
            old = c.execute('SELECT data FROM scout_buffer WHERE guild_id=? AND message_id=?', (str(guild), snapshot['message_id'])).fetchone()
            versions = json.loads(old['data']).get('edits', []) if old else []
            if old:
                previous = json.loads(old['data'])
                if previous.get('content') != snapshot.get('content') or previous.get('attachments') != snapshot.get('attachments'):
                    versions.append({k: v for k, v in previous.items() if k != 'edits'})
            cutoff = time.time() - self.settings(guild)['retention_days'] * 86400
            versions = [v for v in versions if v.get('captured_at',time.time()) >= cutoff]
            snapshot = dict(snapshot, captured_at=time.time(), edits=versions)
            c.execute('INSERT OR REPLACE INTO scout_buffer VALUES(?,?,?,?,?)', (str(guild), snapshot['message_id'], snapshot['channel_id'], encode(snapshot), time.time()))

    def prune(self, guild):
        with self.connect() as c:
            c.execute('DELETE FROM scout_buffer WHERE guild_id=? AND observed_at<?', (str(guild), time.time() - self.settings(guild)['retention_days'] * 86400))
            c.execute('DELETE FROM scout_observations WHERE guild_id=? AND observed_at<?', (str(guild), time.time() - CORRELATION_RETENTION_SECONDS))

    @staticmethod
    def confirmed_scout_action(data, key):
        """A raw event, affected member, or message author never identifies a moderator."""
        return bool(
            data.get('confirmed_scout') is True
            and not data.get('moderator_is_bot', False)
            and str(data.get('moderator_id', '')).isdigit()
            and isinstance(data.get('scout_roles'), list) and data['scout_roles']
            and data.get('audit_id')
            and key == 'audit:' + str(data['audit_id'])
        )

    def system(self, guild, key, data):
        cfg = self.settings(guild)
        if not cfg['enabled'] or not cfg['automatic_enabled'] or data['action_type'] not in cfg['action_types']:
            return None
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # Keep raw events only as short-lived, restart-safe correlation inputs.
            # They must never create Scout records, revisions, or delivery jobs.
            if 'message_ids' in data:
                snapshots = []
                cutoff = time.time() - cfg['retention_days'] * 86400
                for mid in data['message_ids']:
                    row = c.execute('SELECT data FROM scout_buffer WHERE guild_id=? AND message_id=? AND observed_at>=?', (str(guild), str(mid), cutoff)).fetchone()
                    fallback=next((m for m in data.get('cached_messages',[]) if str(m['message_id'])==str(mid)),None)
                    snapshots.append(json.loads(row['data']) if row else fallback or dict(message_id=str(mid), content=None))
                data = dict(data, messages=snapshots)
            c.execute('INSERT OR IGNORE INTO scout_observations VALUES(?,?,?,?)', (str(guild), key, encode(data), time.time()))
            observation = c.execute('SELECT data FROM scout_observations WHERE guild_id=? AND source_key=?', (str(guild), key)).fetchone()
            previous=json.loads(observation['data'])
            if key.startswith('audit:') and '_delta_count' not in previous:
                saved=c.execute('SELECT data FROM scout_records WHERE guild_id=? AND source_key=?',(str(guild),key)).fetchone()
                prior_count=json.loads(saved['data']).get('count',1) if saved else 0
                previous.update(_delta_count=max(0,data.get('count',1)-prior_count),
                    _match_at=time.time() if saved else data['event_at'],_match_window=30 if saved else 10)
                c.execute('UPDATE scout_observations SET data=? WHERE guild_id=? AND source_key=?',(encode(previous),str(guild),key))
            if data.get('count', 1) > previous.get('count', 1):
                data.update(_delta_count=data.get('count',1)-previous.get('count',1),_match_at=time.time(),_match_window=30)
                c.execute('UPDATE scout_observations SET data=?,observed_at=? WHERE guild_id=? AND source_key=?', (encode(data),time.time(),str(guild),key))
            if (not self.confirmed_scout_action(data, key)
                    or not set(data['scout_roles']) & set(cfg['scout_roles'])):
                return None
            old = c.execute('SELECT id FROM scout_records WHERE guild_id=? AND source_key=?', (str(guild), key)).fetchone()
            if old:
                r = self._record(c, guild, old['id'])
                # Audit counts may grow. Append the observation; never replace original evidence.
                if data.get('count', 1) > r['data'].get('count', 1):
                    r['data'].setdefault('observations', []).append(data)
                    r['data']['count'] = data['count']
                    return self._update(c, r, 'system', 'audit_count_increased')
                return r
            return self._create(c, guild, 'system', data.get('moderator_id', ''), data, source=key)

    def reconcile_links(self, guild):
        """Attach uniquely correlated context, keeping audit actor and inference separate.

        Discord supplies no exact message association. Require a complete count,
        matching scope/time (and author for individual deletes), no competing
        actor, and no previously consumed event. Raw events never create cards.
        """
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            cutoff = time.time() - CORRELATION_RETENTION_SECONDS
            events = c.execute("SELECT source_key,data FROM scout_observations WHERE guild_id=? AND source_key LIKE 'gateway:%' AND observed_at>?", (str(guild), cutoff)).fetchall()
            audits = [dict(source_key=r['source_key'], data=json.loads(r['data'])) for r in c.execute("SELECT source_key,data FROM scout_observations WHERE guild_id=? AND source_key LIKE 'audit:%' AND observed_at>?", (str(guild), cutoff))]
            for row in events:
                d = json.loads(row['data'])
                if d.get('timeout_change') == 'expiry':
                    continue
                candidates = [a['source_key'] for a in audits if (d['action_type'] != 'timeout' or (a['data'].get('target_id') == d.get('target_id') and a['data'].get('before') == d.get('before') and a['data'].get('after') == d.get('after'))) and a['data']['action_type'] == d['action_type'] and abs(a['data']['event_at'] - d['event_at']) <= 90 and (not a['data'].get('channel_id') or a['data'].get('channel_id') == d.get('channel_id'))]
                if candidates and candidates != d.get('audit_candidates', []):
                    d['audit_candidates'] = candidates
                    d['attribution'] = ('timeout audit evidence linked; inspect the source record' if d['action_type']=='timeout' else 'uncertain: audit candidates do not identify exact messages')
                    c.execute('UPDATE scout_observations SET data=? WHERE guild_id=? AND source_key=?', (encode(d), str(guild), row['source_key']))
            deletion_events=[]
            for row in events:
                d=json.loads(row['data'])
                if d.get('action_type') not in ('message_delete','message_bulk_delete') or d.get('context_record_id'):
                    continue
                observed=c.execute('SELECT observed_at FROM scout_observations WHERE guild_id=? AND source_key=?',(str(guild),row['source_key'])).fetchone()[0]
                if time.time()-observed<2:
                    continue
                matches=[]
                for audit in audits:
                    a=audit['data']
                    if a.get('action_type')!=d['action_type'] or str(a.get('channel_id'))!=str(d.get('channel_id')):
                        continue
                    if abs(a.get('_match_at',a['event_at'])-d['event_at'])>a.get('_match_window',10):
                        continue
                    if d['action_type']=='message_delete' and (not a.get('target_id') or any(str(m.get('author_id',''))!=str(a['target_id']) for m in d.get('messages',[]))):
                        continue
                    matches.append(audit['source_key'])
                if len(matches)==1:
                    deletion_events.append((row['source_key'],d,matches[0]))
            for audit in audits:
                key,a=audit['source_key'],audit['data']
                if not self.confirmed_scout_action(a,key):
                    continue
                saved=c.execute('SELECT id FROM scout_records WHERE guild_id=? AND source_key=?',(str(guild),key)).fetchone()
                if not saved:
                    continue
                record=self._record(c,guild,saved['id'])
                batch=str(a.get('count',1))
                if batch in record['data'].get('message_context_batches',[]):
                    continue
                candidates=[(k,d) for k,d,parent in deletion_events if parent==key and k not in record['data'].get('message_context_events',[])]
                snapshots=[m for _,d in candidates for m in d.get('messages',[])]
                if not snapshots or len(snapshots)!=a.get('_delta_count',a.get('count',1)):
                    continue
                existing={m['message_id'] for m in record['data'].get('messages',[])}
                new=[m for m in snapshots if m['message_id'] not in existing]
                if not new:
                    continue
                record['data'].setdefault('messages',[]).extend(new)
                record['data'].setdefault('message_context_events',[]).extend(k for k,_ in candidates)
                record['data'].setdefault('message_context_batches',[]).append(batch)
                record['data']['message_context_attribution']='correlated: author/channel/time/count match; exact message IDs are not supplied by Discord audit logs'
                self._update(c,record,'system','deleted_context_correlated')
                for event_key,event in candidates:
                    event['context_record_id']=record['id']
                    c.execute('UPDATE scout_observations SET data=? WHERE guild_id=? AND source_key=?',(encode(event),str(guild),event_key))

    def retry_delivery(self, actor, job_id):
        actor.require('settings.manage')
        with self.connect() as c:
            c.execute("UPDATE scout_jobs SET status='pending',error='' WHERE guild_id=? AND id=? AND status='failed'", (actor.guild_id, job_id))

    def queue_card_refresh(self, limit=100):
        """Quietly restyle recent existing channel cards, preserving workflow/history.

        Run from the updated bot, not the dashboard's schema migration: an older
        bot must not consume this migration and mark pink cards as refreshed.
        """
        queued = 0
        purpose = 'layout:' + CARD_LAYOUT_VERSION
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            guilds = c.execute("SELECT guild_id FROM scout_settings WHERE json_extract(data,'$.enabled')=1").fetchall()
            for guild in guilds:
                records = c.execute('''SELECT r.* FROM scout_records r
                    WHERE r.guild_id=? AND r.kind!='checkin'
                    AND EXISTS(SELECT 1 FROM scout_jobs j WHERE j.record_id=r.id AND j.status='sent' AND j.message_id!='')
                    AND NOT EXISTS(SELECT 1 FROM scout_jobs j WHERE j.record_id=r.id AND j.status IN ('pending','sending','uncertain','failed'))
                    ORDER BY r.updated_at DESC,r.id LIMIT ?''', (guild['guild_id'], limit)).fetchall()
                for row in records:
                    record = self._record(c, guild['guild_id'], row['id'])
                    receipt = c.execute("SELECT destination FROM scout_jobs WHERE record_id=? AND status='sent' AND message_id!='' ORDER BY created_at DESC LIMIT 1", (record['id'],)).fetchone()
                    before = c.total_changes
                    self._job(c, record, purpose)
                    if c.total_changes > before:
                        c.execute('UPDATE scout_jobs SET destination=? WHERE record_id=? AND version=? AND purpose=?',
                                  (receipt['destination'], record['id'], record['version'], purpose))
                        queued += 1
        return queued

    def ai_sources(self, actor, scout='', days=30):
        actor.require('ai.use')
        actor.require('activity.view')
        sources = []
        kinds = ['manual', 'escalation']
        if 'scouts.moderation.view' in actor.permissions:
            kinds.append('system')
        for kind in kinds:
            for record in self.listing(actor, kind, scout=scout, days=days)[:25]:
                data = dict(record['data'])
                if data.get('related'):
                    data['related'] = record.get('related_short_id', '')
                sources.append(dict(id=record['short_id'], kind=record['kind'], status=status_label(record),
                                    author_id=record['author_id'], data=data, created_at=record['created_at']))
        if 'scouts.checkins.view_all' in actor.permissions or (scout == actor.user_id and 'scouts.checkins.respond' in actor.permissions):
            for r in self.listing(actor, 'checkin', days=days):
                if scout and r['data']['recipient_id'] != scout:
                    continue
                try:
                    answer = self.answers(actor, r['id'])
                except PermissionError:
                    continue
                if answer:
                    sources.append(dict(id=r['short_id'], kind='checkin', questions=r['data']['questions'], answers=answer['answers']))
        # Reports and their private table never enter AI. Bounded, explicit selection.
        return sources
