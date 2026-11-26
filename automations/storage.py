"""Guild-scoped rule revisions, run claims and cooldowns in the shared database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
import time
from typing import Optional

import aiosqlite

from automations.evaluator import conditions_in, cooldown_key, evaluate
from automations.models import MAX_RULES, Rule, is_id
from utils.settings import get_setting, settings_database_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_rules (
    id INTEGER PRIMARY KEY,
    guild_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    definition TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    last_triggered_at REAL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS automation_rules_guild ON automation_rules(guild_id, archived, enabled);
CREATE TABLE IF NOT EXISTS automation_revisions (
    rule_id INTEGER NOT NULL REFERENCES automation_rules(id),
    revision INTEGER NOT NULL,
    definition TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    operation TEXT NOT NULL,
    changed_at REAL NOT NULL,
    PRIMARY KEY(rule_id, revision)
);
CREATE TABLE IF NOT EXISTS automation_cooldowns (
    rule_id INTEGER NOT NULL REFERENCES automation_rules(id),
    scope TEXT NOT NULL,
    entity TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    last_at REAL NOT NULL,
    PRIMARY KEY(rule_id, scope, entity, seconds)
);
CREATE TABLE IF NOT EXISTS automation_runs (
    id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL REFERENCES automation_rules(id),
    revision INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    started_at REAL NOT NULL,
    status TEXT NOT NULL,
    results TEXT NOT NULL DEFAULT '[]',
    UNIQUE(rule_id, event_key)
);
CREATE INDEX IF NOT EXISTS automation_runs_rule_time ON automation_runs(rule_id, started_at);
CREATE INDEX IF NOT EXISTS automation_runs_time ON automation_runs(started_at);
CREATE TABLE IF NOT EXISTS automation_seeded (guild_id TEXT PRIMARY KEY);
"""
RUN_WINDOW = 60
RUN_LIMIT = 30
RUN_RETENTION = 30 * 86400


def configured_guild():
    value = str(get_setting("GUILD_ID", "") or os.getenv("GUILD_ID", "")).strip()
    return value if is_id(value) else None


async def evaluate_event(rule, event, **options):
    if any(node.type == "KEYWORD_MATCH" for node in conditions_in(rule.conditions)):
        return await asyncio.to_thread(evaluate, rule, event, **options)
    return evaluate(rule, event, **options)


class MissingRule(Exception):
    pass


class RevisionConflict(Exception):
    pass


@dataclass
class StoredRule:
    id: int
    guild_id: str
    revision: int
    rule: Rule
    updated_at: float
    last_triggered_at: Optional[float]
    archived: bool

    def public(self):
        return {
            "id": self.id,
            "revision": self.revision,
            "rule": self.rule.model_dump(exclude_none=True),
            "updated_at": self.updated_at,
            "last_triggered_at": self.last_triggered_at,
            "archived": self.archived,
        }


class Store:
    def __init__(self, connection):
        self.db = connection
        self.lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(cls, path=None):
        async with aiosqlite.connect(path or settings_database_path(), timeout=5, isolation_level=None) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            yield cls(db)

    async def initialize(self):
        await self.db.executescript(SCHEMA)

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def rows(self, sql, params=()):
        async with self.db.execute(sql, params) as cursor:
            return await cursor.fetchall()

    @staticmethod
    def unpack(row):
        return StoredRule(
            row["id"], row["guild_id"], row["revision"], Rule.model_validate_json(row["definition"]),
            row["updated_at"], row["last_triggered_at"], bool(row["archived"]),
        )

    async def _get(self, guild_id, rule_id, *, archived=False):
        rows = await self.rows(
            "SELECT * FROM automation_rules WHERE guild_id=? AND id=? AND (archived=0 OR ?)",
            (guild_id, rule_id, archived),
        )
        if not rows:
            raise MissingRule("Automation rule was not found.")
        return self.unpack(rows[0])

    async def get(self, guild_id, rule_id, *, archived=False):
        async with self.lock:
            return await self._get(guild_id, rule_id, archived=archived)

    async def list(self, guild_id, *, enabled_only=False, archive=False):
        async with self.lock:
            rows = await self.rows(
                """SELECT * FROM automation_rules WHERE guild_id=? AND archived=? AND (enabled=1 OR ?)
                   ORDER BY id DESC LIMIT ?""",
                (guild_id, archive, not enabled_only, MAX_RULES),
            )
        return [self.unpack(row) for row in rows]

    async def _revision(self, rule_id, revision, rule, by, operation, now):
        await self.db.execute(
            "INSERT INTO automation_revisions VALUES (?,?,?,?,?,?)",
            (rule_id, revision, rule.model_dump_json(exclude_none=True), by, operation, now),
        )

    async def _create(self, guild_id, rule, by):
        if (await self.rows("SELECT count(*) FROM automation_rules WHERE guild_id=? AND archived=0", (guild_id,)))[0][0] >= MAX_RULES:
            raise ValueError(f"A server can have at most {MAX_RULES} rules.")
        rule = rule.model_copy(update={"enabled": False})
        now = time.time()
        async with self.db.execute(
            "INSERT INTO automation_rules (guild_id, revision, definition, enabled, updated_at) VALUES (?,1,?,0,?)",
            (guild_id, rule.model_dump_json(exclude_none=True), now),
        ) as cursor:
            rule_id = cursor.lastrowid
        await self._revision(rule_id, 1, rule, by, "create", now)
        return await self._get(guild_id, rule_id)

    async def create(self, guild_id, rule, by):
        async with self.transaction():
            return await self._create(guild_id, rule, by)

    async def seed_once(self, guild_id, rules):
        async with self.transaction():
            async with self.db.execute("INSERT OR IGNORE INTO automation_seeded VALUES (?)", (guild_id,)) as cursor:
                first = cursor.rowcount
            if first and not await self.rows("SELECT 1 FROM automation_rules WHERE guild_id=? LIMIT 1", (guild_id,)):
                for rule in rules:
                    await self._create(guild_id, rule, "seed")

    async def update(self, guild_id, rule_id, revision, by, *, rule=None, enabled=None, archive=False):
        async with self.transaction():
            old = await self._get(guild_id, rule_id)
            if old.revision != revision:
                raise RevisionConflict("This rule changed. Reload it before saving again.")
            if archive:
                updated = old.rule.model_copy(update={"enabled": False})
                operation = "archive"
            elif rule is None:
                updated = old.rule.model_copy(update={"enabled": enabled})
                operation = "enable" if enabled else "disable"
            else:
                # Status changes have their own deliberate activation path.
                updated = rule.model_copy(update={"enabled": old.rule.enabled})
                operation = "edit"
            if updated == old.rule and not archive:
                return old
            next_revision = revision + 1
            now = time.time()
            await self.db.execute(
                """UPDATE automation_rules SET definition=?, revision=?, enabled=?, updated_at=?, archived=?
                   WHERE id=? AND guild_id=?""",
                (updated.model_dump_json(exclude_none=True), next_revision, updated.enabled, now, archive, rule_id, guild_id),
            )
            await self._revision(rule_id, next_revision, updated, by, operation, now)
            return await self._get(guild_id, rule_id, archived=archive)

    async def history(self, guild_id, rule_id):
        async with self.lock:
            await self._get(guild_id, rule_id, archived=True)
            versions = await self.rows(
                """SELECT revision, definition, changed_by, operation, changed_at FROM automation_revisions
                   WHERE rule_id=? ORDER BY revision DESC LIMIT 50""", (rule_id,),
            )
            runs = await self.rows(
                """SELECT revision, started_at, status, results FROM automation_runs
                   WHERE rule_id=? ORDER BY started_at DESC LIMIT 25""", (rule_id,),
            )
        return {
            "revisions": [{**dict(row), "definition": json.loads(row["definition"])} for row in versions],
            "runs": [{**dict(row), "results": json.loads(row["results"])} for row in runs],
        }

    async def _cooldowns(self, rule_id, rule, event):
        keys = {
            cooldown_key(condition.value, event)
            for condition in conditions_in(rule.conditions) if condition.type == "COOLDOWN_READY"
        } - {None}
        if not keys or rule_id is None:
            return {}
        placeholders = ", ".join("(?,?,?)" for _ in keys)
        params = tuple(part for key in keys for part in key)
        rows = await self.rows(
            f"SELECT scope, entity, seconds, last_at FROM automation_cooldowns WHERE rule_id=? AND (scope, entity, seconds) IN (VALUES {placeholders})",
            (rule_id, *params),
        )
        return {(row["scope"], row["entity"], row["seconds"]): row["last_at"] for row in rows}

    async def preview(self, rule, event, rule_id=None, *, bot_id=None):
        async with self.lock:
            times = await self._cooldowns(rule_id, rule, event)
        return await evaluate_event(rule, event, now=time.time(), timestamps=times, trace=True, bot_id=bot_id)

    async def claim(self, stored, event, event_key, *, bot_id, now=None):
        now = time.time() if now is None else now
        uses_cooldowns = any(c.type == "COOLDOWN_READY" for c in conditions_in(stored.rule.conditions))
        if not uses_cooldowns:
            matched, _, _ = await evaluate_event(stored.rule, event, now=now, bot_id=bot_id)
            if not matched:
                return None
        async with self.transaction():
            rows = await self.rows(
                "SELECT revision FROM automation_rules WHERE id=? AND guild_id=? AND enabled=1 AND archived=0",
                (stored.id, stored.guild_id),
            )
            if not rows or rows[0]["revision"] != stored.revision:
                return None
            keys = set()
            if uses_cooldowns:
                times = await self._cooldowns(stored.id, stored.rule, event)
                matched, keys, _ = await evaluate_event(stored.rule, event, now=now, timestamps=times, bot_id=bot_id)
                if not matched:
                    return None
            total = (await self.rows(
                "SELECT count(*) FROM automation_runs WHERE rule_id=? AND started_at>?",
                (stored.id, now - RUN_WINDOW),
            ))[0][0]
            if total >= RUN_LIMIT:
                return None
            async with self.db.execute(
                """INSERT OR IGNORE INTO automation_runs (rule_id,revision,event_key,started_at,status)
                   VALUES (?,?,?,?,'running')""", (stored.id, stored.revision, event_key, now),
            ) as cursor:
                if not cursor.rowcount:
                    return None
                run_id = cursor.lastrowid
            await self.db.executemany(
                """INSERT INTO automation_cooldowns VALUES (?,?,?,?,?)
                   ON CONFLICT(rule_id,scope,entity,seconds) DO UPDATE SET last_at=excluded.last_at""",
                [(stored.id, *key, now) for key in keys],
            )
            await self.db.execute(
                "UPDATE automation_rules SET last_triggered_at=max(coalesce(last_triggered_at,0),?) WHERE id=?",
                (now, stored.id),
            )
            return run_id

    async def finish(self, run_id, status, results):
        async with self.lock:
            await self.db.execute(
                "UPDATE automation_runs SET status=?, results=? WHERE id=? AND status='running'",
                (status, json.dumps(results), run_id),
            )

    async def prune(self):
        now = time.time()
        async with self.transaction():
            # Interrupted runs are kept for inspection and are never replayed.
            await self.db.execute(
                "UPDATE automation_runs SET status='interrupted' WHERE status='running' AND started_at<?",
                (now - 900,),
            )
            await self.db.execute("DELETE FROM automation_runs WHERE started_at<?", (now - RUN_RETENTION,))
            await self.db.execute("DELETE FROM automation_cooldowns WHERE last_at<?-seconds", (now,))
