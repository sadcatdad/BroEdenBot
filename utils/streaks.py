"""Shared streak schema and calculations for the bot and local dashboard."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable


FIXED_STREAK_MILESTONES = {7, 14, 30, 45, 60}


STREAK_SCHEMA = """
CREATE TABLE IF NOT EXISTS streak_days (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    activity_date TEXT NOT NULL,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, activity_date),
    UNIQUE (guild_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_streak_days_message
    ON streak_days (guild_id, message_id);
CREATE INDEX IF NOT EXISTS idx_streak_days_hash
    ON streak_days (guild_id, user_id, message_hash, activity_date);
CREATE TABLE IF NOT EXISTS member_streaks (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_qualified_date TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_member_streaks_current
    ON member_streaks (guild_id, current_streak DESC);
CREATE TABLE IF NOT EXISTS streak_milestones (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    milestone_days INTEGER NOT NULL,
    source_message_id TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    seen_at TEXT,
    PRIMARY KEY (guild_id, user_id, milestone_days)
);
CREATE INDEX IF NOT EXISTS idx_streak_milestones_unread
    ON streak_milestones (guild_id, user_id, seen_at, milestone_days DESC);
CREATE TABLE IF NOT EXISTS streak_weekly_posts (
    guild_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    last_week_key TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streak_runtime_state (
    guild_id TEXT PRIMARY KEY,
    last_heartbeat_at TEXT NOT NULL,
    last_started_at TEXT,
    last_restore_at TEXT,
    last_restore_status TEXT,
    last_restore_detail TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS streak_restore_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    start_at_utc TEXT NOT NULL,
    end_at_utc TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    request_source TEXT NOT NULL DEFAULT 'dashboard'
        CHECK (request_source IN ('dashboard', 'automatic')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    messages_scanned INTEGER NOT NULL DEFAULT 0,
    days_restored INTEGER NOT NULL DEFAULT 0,
    members_restored INTEGER NOT NULL DEFAULT 0,
    channels_failed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_streak_restore_requests_status
    ON streak_restore_requests (status, created_at);
CREATE TABLE IF NOT EXISTS streak_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    activity_date TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    reason TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    source_message_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_streak_adjustments_member
    ON streak_adjustments (guild_id, user_id, created_at DESC);
"""


def is_streak_milestone(days: int) -> bool:
    return days in FIXED_STREAK_MILESTONES or (days >= 100 and days % 50 == 0)


def compute_streaks(days: Iterable[date], today: date) -> tuple[int, int]:
    ordered = sorted(set(days))
    if not ordered:
        return 0, 0
    longest = 1
    run = 1
    for previous, current in zip(ordered, ordered[1:]):
        if current == previous + timedelta(days=1):
            run += 1
        else:
            run = 1
        longest = max(longest, run)
    latest = ordered[-1]
    if latest not in {today, today - timedelta(days=1)}:
        return 0, longest
    current = 1
    cursor = latest
    available = set(ordered)
    while cursor - timedelta(days=1) in available:
        cursor -= timedelta(days=1)
        current += 1
    return current, longest
