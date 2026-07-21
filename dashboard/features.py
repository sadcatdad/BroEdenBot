"""Small, explicit registry for dashboard feature discovery and ownership."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from utils.settings import SETTING_DEFINITIONS, get_setting


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    name: str
    description: str
    category: str
    permission: str
    dashboard_path: Optional[str]
    module_keys: tuple[str, ...] = ()
    required_settings: tuple[str, ...] = ()
    support_status: str = "supported"


FEATURES = (
    FeatureDefinition("streaks", "Activity Streaks", "Daily message streaks, milestones, leaderboards, and recovery.", "Community & engagement", "streaks.view", "/streaks", ("streaks",), ("STREAK_TIMEZONE",)),
    FeatureDefinition("bumps", "DISBOARD Bumps", "Verified bumps, points, reminders, reward roles, and Bump Legends.", "Community & engagement", "bumps.view", "/features/bumps", ("bumps",), ("DISBOARD_BOT_USER_ID",)),
    FeatureDefinition("reminders", "Reminders", "Personal reminders, scheduled delivery, subscriptions, and operator recovery.", "Reminders & automation", "reminders.view", "/operations/reminders", ("reminders",), ("REMINDER_TIMEZONE",)),
    FeatureDefinition("events", "Events", "Discord event creation and subscription flows; dashboard event authoring is planned.", "Reminders & automation", "events.view", "/features/events", ("reminders",), support_status="experimental"),
    FeatureDefinition("ask", "Ask", "Private, public-knowledge-grounded member answers with channel controls.", "AI & knowledge", "ask.view", "/features/ask", ("ask",), ("ASK_COOLDOWN_SECONDS",)),
    FeatureDefinition("knowledge", "Knowledge", "File, manual AI, and live Discord knowledge sources.", "AI & knowledge", "knowledge.view", "/knowledge", ("knowledge",)),
    FeatureDefinition("staff_tools", "Staff & Moderation Tools", "Private staff AI, ModAI, notes, and message-context permissions.", "Moderation", "staff_tools.view", "/features/staff_tools", ("mod_ai", "staff_ai", "staff_notes", "message_context")),
    FeatureDefinition("analytics", "Analytics & Stats", "Aggregate activity analytics, managed reports, and leaderboards.", "Analytics & leaderboards", "analytics.view", "/analytics", ("stats",)),
    FeatureDefinition("voice", "Voice Stats & XP", "Voice tracking, leaderboards, exclusions, and XP role pulses.", "Analytics & leaderboards", "voice.view", "/features/voice", ("vc_stats", "vc_xp")),
    FeatureDefinition("bank", "Bro Eden Bank", "Contribution ledger and read-only dashboard summary.", "Finance", "bank.view", "/bank", ("bank",)),
    FeatureDefinition("polls", "Polls", "Persistent community polls with button voting and visual results.", "Community & engagement", "polls.view", "/features/polls", ("polls",)),
    FeatureDefinition("checklists", "Staff Checklists", "Persistent staff checklists with posting, recovery, and export.", "Moderation", "checklists.view", "/features/checklists", ("checklists",)),
    FeatureDefinition("queue", "Karaoke Queue", "Voice-channel queue dashboards and staff queue controls.", "Community & engagement", "queue.view", "/features/queue", ("karaoke",)),
    FeatureDefinition("rulecards", "Rule Cards", "AI-assisted staff rule reminder drafts.", "Moderation", "rulecards.view", "/features/rulecards", ("rulecards",)),
    FeatureDefinition("message_studio", "Message Studio", "Reusable message and multi-embed assets used by bot features.", "Content generation", "message_studio.view", "/embeds"),
    FeatureDefinition("visual", "Visual Content Studio", "Shared templates, themes, uploaded assets, previews, and publishing.", "Content generation", "visual.view", "/visual"),
)

FEATURES_BY_KEY = {feature.key: feature for feature in FEATURES}

FEATURE_DEPENDENCIES = {
    "streaks": ("Eligible message channels", "Optional milestone and leaderboard channels"),
    "bumps": ("DISBOARD bot identity", "Optional reward and reminder roles"),
    "reminders": ("Delivery channels", "Command-access roles"),
    "events": ("Event destination channels", "Subscriber access roles"),
    "ask": ("Allowed channels or categories",),
    "knowledge": ("Allowed channels or categories",),
    "staff_tools": ("Staff roles", "Private source channels"),
    "analytics": ("Included/excluded channels and roles",),
    "voice": ("Voice channels", "VC XP trigger role"),
    "bank": ("Bank command roles",),
    "polls": ("Message channel permissions",),
    "checklists": ("Staff roles", "Posting channels"),
    "queue": ("Voice channels", "Queue message channels"),
    "rulecards": ("Staff roles",),
}


def enabled_modules() -> Optional[set[str]]:
    raw = os.getenv("ENABLED_MODULES", "").strip()
    if not raw:
        return None
    return {item.casefold() for item in re.split(r"[\s,]+", raw) if item}


def feature_is_enabled(feature: FeatureDefinition) -> bool:
    configured = enabled_modules()
    return not feature.module_keys or configured is None or bool(set(feature.module_keys) & configured)


def feature_key_for_setting(key: str) -> str:
    upper = str(key).upper()
    lower = str(key).lower()
    if upper.startswith("ASK_") or lower.startswith("ask_command_"):
        return "ask"
    if upper.startswith("BUMP_") or upper.startswith("DISBOARD_"):
        return "bumps"
    if upper.startswith("REMINDER_"):
        return "reminders"
    if upper.startswith("EVENTS_"):
        return "events"
    if upper.startswith("STREAK_"):
        return "streaks"
    if upper.startswith(("VCXP_", "VC_XP_", "VCSTATS_", "VC_EXCLUDED_", "EXCLUDED_VOICE_")):
        return "voice"
    if upper.startswith(("STATS_", "LEADERBOARD_", "ACTIVITY_")) or lower.startswith("analytics_"):
        return "analytics"
    if upper.startswith("BANK_") or lower.startswith("bank_"):
        return "bank"
    if upper.startswith(("MODAI_", "STAFF_", "MESSAGE_CONTEXT_", "BOT_OWNER_", "AUDIT_LOG_")):
        return "staff_tools"
    if lower.startswith(("knowledge_", "message_context_excluded_")):
        return "knowledge"
    if lower.startswith("import_"):
        return "imports"
    return "system"


def setting_keys_for_feature(feature_key: str) -> list[str]:
    return [
        definition.key
        for definition in SETTING_DEFINITIONS
        if definition.visible and feature_key_for_setting(definition.key) == feature_key
    ]


def feature_snapshot(feature: FeatureDefinition) -> dict[str, Any]:
    enabled = feature_is_enabled(feature)
    missing = [
        key for key in feature.required_settings
        if not str(get_setting(key, "") or "").strip()
    ]
    if not enabled:
        health = "disabled"
    elif missing:
        health = "incomplete"
    else:
        health = "healthy"
    return {
        "key": feature.key,
        "name": feature.name,
        "description": feature.description,
        "category": feature.category,
        "permission": feature.permission,
        "dashboard_path": feature.dashboard_path,
        "enabled": enabled,
        "health": health,
        "missing_settings": missing,
        "support_status": feature.support_status,
        "setting_count": len(setting_keys_for_feature(feature.key)),
        "module_keys": list(feature.module_keys),
        "discord_dependencies": list(FEATURE_DEPENDENCIES.get(feature.key, ())),
    }


def feature_inventory() -> list[dict[str, Any]]:
    return [feature_snapshot(feature) for feature in FEATURES]
