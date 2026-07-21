"""Capability-based dashboard authorization and append-only audit storage."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from utils.settings import settings_database_path
from utils.sqlite import configure_sync_connection


@dataclass(frozen=True)
class PermissionDefinition:
    key: str
    category: str
    name: str
    description: str


PERMISSIONS = (
    PermissionDefinition("dashboard.view", "Monitor", "View dashboard", "Open the dashboard overview."),
    PermissionDefinition("analytics.view", "Monitor", "View analytics", "Read aggregate activity, member, channel, and voice analytics."),
    PermissionDefinition("analytics.manage", "Monitor", "Manage analytics", "Edit, refresh, archive, and export managed statistics."),
    PermissionDefinition("features.view", "Community", "View features", "Browse supported bot features and their configuration health."),
    PermissionDefinition("features.manage", "Community", "Manage features", "Change supported feature configuration."),
    PermissionDefinition("bumps.view", "Community", "View bump configuration", "Read DISBOARD bump configuration and readiness."),
    PermissionDefinition("polls.view", "Community", "View polls", "View the poll feature and its dashboard readiness."),
    PermissionDefinition("queue.view", "Community", "View karaoke queue", "View the karaoke queue feature and its dashboard readiness."),
    PermissionDefinition("streaks.view", "Community", "View streaks", "Read streak status and recovery history."),
    PermissionDefinition("streaks.manage", "Community", "Manage streaks", "Queue restores and record audited streak adjustments."),
    PermissionDefinition("events.view", "Community", "View events", "View event configuration and upcoming event operations."),
    PermissionDefinition("events.create", "Community", "Create events", "Create server events when the Events dashboard is available."),
    PermissionDefinition("events.edit_own", "Community", "Edit own events", "Edit events created by the current user."),
    PermissionDefinition("events.edit_all", "Community", "Edit all events", "Edit any server event."),
    PermissionDefinition("events.publish", "Community", "Publish events", "Publish or approve server events."),
    PermissionDefinition("events.delete", "Community", "Delete events", "Delete server events."),
    PermissionDefinition("operations.view", "Operations", "View operations", "Read bot, service, database, and task status."),
    PermissionDefinition("operations.manage", "Operations", "Manage operations", "Run narrow maintenance and backup actions."),
    PermissionDefinition("bot.status.view", "Operations", "View bot status", "Read bot and dashboard service health."),
    PermissionDefinition("bot.restart", "Operations", "Restart services", "Restart the bot or dashboard service."),
    PermissionDefinition("reminders.view", "Operations", "View reminders", "Read reminder and delivery status."),
    PermissionDefinition("reminders.manage", "Operations", "Manage reminders", "Queue reminder retries, cancellation, or archival."),
    PermissionDefinition("voice.view", "Monitor", "View voice feature", "Read voice-stat and VC XP feature configuration."),
    PermissionDefinition("imports.view", "Operations", "View imports", "Read import history and storage status."),
    PermissionDefinition("imports.manage", "Operations", "Manage imports", "Run supported import actions when available."),
    PermissionDefinition("bank.view", "Finance", "View bank", "Read bank totals and recent entries."),
    PermissionDefinition("bank.manage", "Finance", "Manage bank", "Manage bank data when supported by the dashboard."),
    PermissionDefinition("content.view", "Content", "View content", "Read content-management surfaces."),
    PermissionDefinition("content.manage", "Content", "Manage content", "Create and update reusable content."),
    PermissionDefinition("message_studio.view", "Content", "View Message Studio", "Read reusable message and embed assets."),
    PermissionDefinition("message_studio.manage", "Content", "Manage Message Studio", "Create, update, and delete message assets."),
    PermissionDefinition("visual.view", "Content", "View Visual Studio", "Read visual templates, assets, themes, and previews."),
    PermissionDefinition("visual.manage", "Content", "Manage Visual Studio", "Create, publish, import, and delete visual configuration."),
    PermissionDefinition("knowledge.view", "Content", "View knowledge", "Read configured public and staff knowledge sources."),
    PermissionDefinition("knowledge.manage", "Content", "Manage knowledge", "Edit, synchronize, and reindex knowledge sources."),
    PermissionDefinition("ai.view", "Content", "View AI", "Read AI configuration, health, and aggregate usage."),
    PermissionDefinition("ai.manage", "Content", "Manage AI", "Manage AI knowledge sources and AI-specific settings."),
    PermissionDefinition("ask.view", "Content", "View Ask configuration", "Read public Ask feature configuration and readiness."),
    PermissionDefinition("staff_tools.view", "Content", "View staff tools", "Read staff and moderation feature configuration."),
    PermissionDefinition("checklists.view", "Content", "View checklists", "View staff checklist feature readiness."),
    PermissionDefinition("rulecards.view", "Content", "View rule cards", "View rule-card feature readiness."),
    PermissionDefinition("settings.view", "System", "View settings", "Read system-wide configuration and its effective source."),
    PermissionDefinition("settings.manage", "System", "Manage settings", "Change allowlisted non-secret system configuration."),
    PermissionDefinition("discord_metadata.view", "System", "View Discord connection", "Read synchronized server roles, channels, categories, and emoji."),
    PermissionDefinition("discord_metadata.refresh", "System", "Refresh Discord metadata", "Queue a live Discord metadata refresh."),
    PermissionDefinition("access.manage", "System", "Manage dashboard access", "Manage dashboard roles, mappings, assignments, and user access."),
    PermissionDefinition("audit_log.view", "System", "View audit log", "Review dashboard authentication and administrator actions."),
)

PERMISSION_KEYS = frozenset(item.key for item in PERMISSIONS)

SYSTEM_ROLES = {
    "owner": {
        "name": "Owner",
        "description": "Full dashboard access and emergency recovery authority.",
        "permissions": set(PERMISSION_KEYS),
    },
    "administrator": {
        "name": "Administrator",
        "description": "Broad operational, content, and configuration access without dashboard-access administration.",
        "permissions": set(PERMISSION_KEYS) - {"access.manage"},
    },
    "moderator": {
        "name": "Moderator",
        "description": "Moderation-adjacent analytics, knowledge, reminder, and event visibility.",
        "permissions": {
            "dashboard.view", "analytics.view", "features.view", "events.view",
            "operations.view", "bot.status.view", "reminders.view",
            "content.view", "knowledge.view", "ai.view", "ask.view",
            "staff_tools.view", "checklists.view", "rulecards.view", "voice.view",
        },
    },
    "party_captain": {
        "name": "Party Captain",
        "description": "Limited event planning access, ready for the Events dashboard module.",
        "permissions": {
            "dashboard.view", "features.view", "events.view", "events.create",
            "events.edit_own",
        },
    },
    "viewer": {
        "name": "Analyst / Viewer",
        "description": "Read-only overview and aggregate analytics access.",
        "permissions": {"dashboard.view", "analytics.view", "bot.status.view"},
    },
}

LEGACY_ROLE_MAP = {
    "owner": "owner",
    "admin": "administrator",
    "administrator": "administrator",
    "moderator": "moderator",
    "party_captain": "party_captain",
    "viewer": "viewer",
    "analyst": "viewer",
}

_INITIALIZED_PATHS: set[str] = set()


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    return configure_sync_connection(connection)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_rbac_schema() -> None:
    """Create additive RBAC/audit tables and seed stable system definitions."""
    database_key = str(settings_database_path())
    if database_key in _INITIALIZED_PATHS:
        return
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS dashboard_permissions (
                permission_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dashboard_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                is_system INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS dashboard_role_permissions (
                role_id INTEGER NOT NULL,
                permission_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (role_id, permission_key),
                FOREIGN KEY (role_id) REFERENCES dashboard_roles(id) ON DELETE CASCADE,
                FOREIGN KEY (permission_key) REFERENCES dashboard_permissions(permission_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dashboard_user_role_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'direct',
                source_reference TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, role_id, source, source_reference),
                FOREIGN KEY (user_id) REFERENCES dashboard_users(id) ON DELETE CASCADE,
                FOREIGN KEY (role_id) REFERENCES dashboard_roles(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dashboard_discord_role_mappings (
                discord_role_id TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (discord_role_id, role_id),
                FOREIGN KEY (role_id) REFERENCES dashboard_roles(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dashboard_user_permission_overrides (
                user_id INTEGER NOT NULL,
                permission_key TEXT NOT NULL,
                allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
                changed_by TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, permission_key),
                FOREIGN KEY (user_id) REFERENCES dashboard_users(id) ON DELETE CASCADE,
                FOREIGN KEY (permission_key) REFERENCES dashboard_permissions(permission_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dashboard_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                actor_user_id INTEGER,
                actor_label TEXT NOT NULL DEFAULT 'anonymous',
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                before_json TEXT,
                after_json TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_dashboard_role_assignments_user
            ON dashboard_user_role_assignments(user_id);
            CREATE INDEX IF NOT EXISTS idx_dashboard_role_mappings_discord
            ON dashboard_discord_role_mappings(discord_role_id);
            CREATE INDEX IF NOT EXISTS idx_dashboard_audit_created
            ON dashboard_audit_log(created_at DESC, id DESC);

            CREATE TRIGGER IF NOT EXISTS dashboard_audit_log_no_update
            BEFORE UPDATE ON dashboard_audit_log
            BEGIN
                SELECT RAISE(ABORT, 'dashboard audit log is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS dashboard_audit_log_no_delete
            BEFORE DELETE ON dashboard_audit_log
            BEGIN
                SELECT RAISE(ABORT, 'dashboard audit log is append-only');
            END;
            """
        )
        now = _utc_now()
        for permission in PERMISSIONS:
            connection.execute(
                """
                INSERT INTO dashboard_permissions (
                    permission_key, category, name, description, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(permission_key) DO UPDATE SET
                    category = excluded.category,
                    name = excluded.name,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (permission.key, permission.category, permission.name, permission.description, now),
            )
        for role_key, definition in SYSTEM_ROLES.items():
            connection.execute(
                """
                INSERT INTO dashboard_roles (
                    role_key, name, description, is_system, updated_at
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(role_key) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    is_system = 1,
                    updated_at = excluded.updated_at
                """,
                (role_key, definition["name"], definition["description"], now),
            )
            role_id = int(
                connection.execute(
                    "SELECT id FROM dashboard_roles WHERE role_key = ?", (role_key,)
                ).fetchone()[0]
            )
            desired = set(definition["permissions"])
            connection.execute(
                "DELETE FROM dashboard_role_permissions WHERE role_id = ?", (role_id,)
            )
            connection.executemany(
                "INSERT INTO dashboard_role_permissions(role_id, permission_key) VALUES (?, ?)",
                [(role_id, key) for key in sorted(desired)],
            )
        if _table_exists(connection, "dashboard_users"):
            for row in connection.execute("SELECT id, role FROM dashboard_users"):
                role_key = LEGACY_ROLE_MAP.get(str(row["role"] or "viewer").casefold(), "viewer")
                role_id = int(
                    connection.execute(
                        "SELECT id FROM dashboard_roles WHERE role_key = ?", (role_key,)
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO dashboard_user_role_assignments (
                        user_id, role_id, source, source_reference
                    ) VALUES (?, ?, 'legacy', ?)
                    """,
                    (int(row["id"]), role_id, role_key),
                )
        connection.commit()
    _INITIALIZED_PATHS.add(database_key)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def permissions_for_user(user_id: int) -> set[str]:
    initialize_rbac_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT role, status FROM dashboard_users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None or str(row["status"]).casefold() != "active":
            return set()
        role_keys = {
            str(item["role_key"])
            for item in connection.execute(
                """
                SELECT DISTINCT r.role_key
                FROM dashboard_user_role_assignments a
                JOIN dashboard_roles r ON r.id = a.role_id
                WHERE a.user_id = ?
                """,
                (int(user_id),),
            )
        }
        legacy_key = LEGACY_ROLE_MAP.get(str(row["role"] or "viewer").casefold(), "viewer")
        role_keys.add(legacy_key)
        if "owner" in role_keys:
            return set(PERMISSION_KEYS)
        permissions = {
            str(item["permission_key"])
            for item in connection.execute(
                """
                SELECT DISTINCT rp.permission_key
                FROM dashboard_user_role_assignments a
                JOIN dashboard_role_permissions rp ON rp.role_id = a.role_id
                WHERE a.user_id = ?
                """,
                (int(user_id),),
            )
        }
        legacy_role_id = connection.execute(
            "SELECT id FROM dashboard_roles WHERE role_key = ?", (legacy_key,)
        ).fetchone()
        if legacy_role_id:
            permissions.update(
                str(item["permission_key"])
                for item in connection.execute(
                    "SELECT permission_key FROM dashboard_role_permissions WHERE role_id = ?",
                    (int(legacy_role_id[0]),),
                )
            )
        for override in connection.execute(
            "SELECT permission_key, allowed FROM dashboard_user_permission_overrides WHERE user_id = ?",
            (int(user_id),),
        ):
            key = str(override["permission_key"])
            if int(override["allowed"]):
                permissions.add(key)
            else:
                permissions.discard(key)
        return permissions


def role_names_for_user(user_id: int) -> list[str]:
    initialize_rbac_schema()
    with _connect() as connection:
        return [
            str(row["name"])
            for row in connection.execute(
                """
                SELECT DISTINCT r.name, r.is_system, r.id
                FROM dashboard_user_role_assignments a
                JOIN dashboard_roles r ON r.id = a.role_id
                WHERE a.user_id = ?
                ORDER BY r.is_system DESC, r.id
                """,
                (int(user_id),),
            )
        ]


def sync_discord_role_assignments(user_id: int, discord_role_ids: Iterable[str]) -> list[str]:
    """Replace mapped Discord-derived assignments and return applied role names."""
    initialize_rbac_schema()
    role_ids = {str(value).strip() for value in discord_role_ids if str(value).strip().isdigit()}
    with _connect() as connection:
        connection.execute(
            "DELETE FROM dashboard_user_role_assignments WHERE user_id = ? AND source = 'discord'",
            (int(user_id),),
        )
        if role_ids:
            placeholders = ",".join("?" for _ in role_ids)
            rows = connection.execute(
                f"""
                SELECT m.discord_role_id, m.role_id, r.name
                FROM dashboard_discord_role_mappings m
                JOIN dashboard_roles r ON r.id = m.role_id
                WHERE m.discord_role_id IN ({placeholders})
                """,
                tuple(sorted(role_ids)),
            ).fetchall()
        else:
            rows = []
        for row in rows:
            connection.execute(
                """
                INSERT OR IGNORE INTO dashboard_user_role_assignments (
                    user_id, role_id, source, source_reference
                ) VALUES (?, ?, 'discord', ?)
                """,
                (int(user_id), int(row["role_id"]), str(row["discord_role_id"])),
            )
        connection.commit()
        return sorted({str(row["name"]) for row in rows})


def has_mapped_discord_role(discord_role_ids: Iterable[str]) -> bool:
    initialize_rbac_schema()
    role_ids = {str(value).strip() for value in discord_role_ids if str(value).strip().isdigit()}
    if not role_ids:
        return False
    placeholders = ",".join("?" for _ in role_ids)
    with _connect() as connection:
        return connection.execute(
            f"SELECT 1 FROM dashboard_discord_role_mappings WHERE discord_role_id IN ({placeholders}) LIMIT 1",
            tuple(sorted(role_ids)),
        ).fetchone() is not None


def list_roles() -> list[dict[str, Any]]:
    initialize_rbac_schema()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT r.*, COUNT(DISTINCT rp.permission_key) AS permission_count,
                   COUNT(DISTINCT a.user_id) AS user_count,
                   COUNT(DISTINCT m.discord_role_id) AS mapping_count
            FROM dashboard_roles r
            LEFT JOIN dashboard_role_permissions rp ON rp.role_id = r.id
            LEFT JOIN dashboard_user_role_assignments a ON a.role_id = r.id
            LEFT JOIN dashboard_discord_role_mappings m ON m.role_id = r.id
            GROUP BY r.id
            ORDER BY r.is_system DESC, r.name COLLATE NOCASE
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["permissions"] = [
                str(value["permission_key"])
                for value in connection.execute(
                    "SELECT permission_key FROM dashboard_role_permissions WHERE role_id = ? ORDER BY permission_key",
                    (int(row["id"]),),
                )
            ]
            result.append(item)
        return result


def permission_catalog() -> list[dict[str, str]]:
    return [item.__dict__.copy() for item in PERMISSIONS]


def save_custom_role(
    *, role_id: Optional[int], name: str, description: str,
    permissions: Iterable[str], changed_by: str,
) -> int:
    initialize_rbac_schema()
    clean_name = " ".join(str(name or "").split())[:80]
    if not clean_name:
        raise ValueError("Role name is required.")
    selected = sorted(set(permissions) & set(PERMISSION_KEYS))
    role_key = "custom_" + uuid.uuid4().hex[:16]
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if role_id is None:
            cursor = connection.execute(
                """
                INSERT INTO dashboard_roles(role_key, name, description, is_system, updated_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (role_key, clean_name, str(description or "").strip()[:300], _utc_now()),
            )
            role_id = int(cursor.lastrowid)
        else:
            existing = connection.execute(
                "SELECT is_system FROM dashboard_roles WHERE id = ?", (int(role_id),)
            ).fetchone()
            if existing is None:
                raise ValueError("Dashboard role was not found.")
            if int(existing["is_system"]):
                raise ValueError("System roles cannot be edited.")
            connection.execute(
                "UPDATE dashboard_roles SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                (clean_name, str(description or "").strip()[:300], _utc_now(), int(role_id)),
            )
            connection.execute("DELETE FROM dashboard_role_permissions WHERE role_id = ?", (int(role_id),))
        connection.executemany(
            "INSERT INTO dashboard_role_permissions(role_id, permission_key) VALUES (?, ?)",
            [(int(role_id), key) for key in selected],
        )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.role.saved", target_type="dashboard_role",
        target_id=str(role_id), after={"name": clean_name, "permissions": selected},
    )
    return int(role_id)


def replace_discord_role_mappings(
    discord_role_ids: Iterable[str], dashboard_role_id: int, *, changed_by: str,
) -> None:
    initialize_rbac_schema()
    role_ids = sorted({str(value).strip() for value in discord_role_ids if str(value).strip().isdigit()})
    with _connect() as connection:
        role = connection.execute(
            "SELECT name FROM dashboard_roles WHERE id = ?", (int(dashboard_role_id),)
        ).fetchone()
        if role is None:
            raise ValueError("Dashboard role was not found.")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM dashboard_discord_role_mappings WHERE role_id = ?",
            (int(dashboard_role_id),),
        )
        for discord_role_id in role_ids:
            connection.execute(
                """
                INSERT OR IGNORE INTO dashboard_discord_role_mappings(
                    discord_role_id, role_id, created_by
                ) VALUES (?, ?, ?)
                """,
                (discord_role_id, int(dashboard_role_id), changed_by),
            )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.discord_mapping.saved",
        target_type="dashboard_role", target_id=str(dashboard_role_id),
        after={"discord_role_ids": role_ids},
    )


def remove_discord_role_mapping(
    discord_role_id: str, dashboard_role_id: int, *, changed_by: str,
) -> None:
    initialize_rbac_schema()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM dashboard_discord_role_mappings WHERE discord_role_id = ? AND role_id = ?",
            (str(discord_role_id), int(dashboard_role_id)),
        )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.discord_mapping.removed",
        target_type="discord_role", target_id=str(discord_role_id),
        before={"dashboard_role_id": int(dashboard_role_id)},
    )


def assign_direct_role(user_id: int, role_id: int, *, changed_by: str) -> None:
    initialize_rbac_schema()
    with _connect() as connection:
        user = connection.execute("SELECT id FROM dashboard_users WHERE id = ?", (int(user_id),)).fetchone()
        role = connection.execute("SELECT id FROM dashboard_roles WHERE id = ?", (int(role_id),)).fetchone()
        if user is None or role is None:
            raise ValueError("User or role was not found.")
        connection.execute(
            """
            INSERT OR IGNORE INTO dashboard_user_role_assignments(
                user_id, role_id, source, source_reference
            ) VALUES (?, ?, 'direct', '')
            """,
            (int(user_id), int(role_id)),
        )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.user_role.assigned",
        target_type="dashboard_user", target_id=str(user_id),
        after={"role_id": int(role_id)},
    )


def remove_direct_role(user_id: int, role_id: int, *, changed_by: str) -> None:
    initialize_rbac_schema()
    with _connect() as connection:
        role = connection.execute(
            "SELECT role_key FROM dashboard_roles WHERE id = ?", (int(role_id),)
        ).fetchone()
        if role and str(role["role_key"]) == "owner" and _active_owner_count(connection) <= 1:
            raise ValueError("The final active owner cannot be removed.")
        connection.execute(
            """
            DELETE FROM dashboard_user_role_assignments
            WHERE user_id = ? AND role_id = ? AND source = 'direct'
            """,
            (int(user_id), int(role_id)),
        )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.user_role.removed",
        target_type="dashboard_user", target_id=str(user_id),
        before={"role_id": int(role_id)},
    )


def set_user_status(user_id: int, status: str, *, changed_by: str) -> None:
    initialize_rbac_schema()
    normalized = str(status).strip().casefold()
    if normalized not in {"active", "disabled"}:
        raise ValueError("User status must be active or disabled.")
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, status FROM dashboard_users WHERE id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            raise ValueError("Dashboard user was not found.")
        if normalized == "disabled":
            is_owner = connection.execute(
                """
                SELECT 1 FROM dashboard_user_role_assignments a
                JOIN dashboard_roles r ON r.id = a.role_id
                WHERE a.user_id = ? AND r.role_key = 'owner' LIMIT 1
                """,
                (int(user_id),),
            ).fetchone() is not None
            if is_owner and _active_owner_count(connection) <= 1:
                raise ValueError("The final active owner cannot be disabled.")
        connection.execute(
            "UPDATE dashboard_users SET status = ?, updated_at = ? WHERE id = ?",
            (normalized, _utc_now(), int(user_id)),
        )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.user_status.changed",
        target_type="dashboard_user", target_id=str(user_id),
        before={"status": str(row["status"])}, after={"status": normalized},
    )


def set_user_permission_override(
    user_id: int, permission_key: str, mode: str, *, changed_by: str,
) -> None:
    initialize_rbac_schema()
    key = str(permission_key).strip()
    normalized_mode = str(mode).strip().casefold()
    if key not in PERMISSION_KEYS:
        raise ValueError("Permission was not found.")
    if normalized_mode not in {"allow", "deny", "inherit"}:
        raise ValueError("Permission override must allow, deny, or inherit.")
    with _connect() as connection:
        role = connection.execute(
            """
            SELECT 1 FROM dashboard_user_role_assignments a
            JOIN dashboard_roles r ON r.id = a.role_id
            WHERE a.user_id = ? AND r.role_key = 'owner' LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if role is not None and normalized_mode == "deny":
            raise ValueError("Owner permissions cannot be denied.")
        if normalized_mode == "inherit":
            connection.execute(
                "DELETE FROM dashboard_user_permission_overrides WHERE user_id = ? AND permission_key = ?",
                (int(user_id), key),
            )
        else:
            connection.execute(
                """
                INSERT INTO dashboard_user_permission_overrides(
                    user_id, permission_key, allowed, changed_by, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, permission_key) DO UPDATE SET
                    allowed = excluded.allowed,
                    changed_by = excluded.changed_by,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), key, 1 if normalized_mode == "allow" else 0, changed_by, _utc_now()),
            )
        connection.commit()
    record_audit(
        actor_label=changed_by, action="access.user_permission.changed",
        target_type="dashboard_user", target_id=str(user_id),
        after={"permission": key, "mode": normalized_mode},
    )


def _active_owner_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT u.id)
            FROM dashboard_users u
            JOIN dashboard_user_role_assignments a ON a.user_id = u.id
            JOIN dashboard_roles r ON r.id = a.role_id
            WHERE u.status = 'active' AND r.role_key = 'owner'
            """
        ).fetchone()[0]
    )


def access_overview(users: list[dict[str, Any]]) -> dict[str, Any]:
    initialize_rbac_schema()
    roles = list_roles()
    with _connect() as connection:
        mappings = [
            dict(row)
            for row in connection.execute(
                """
                SELECT m.discord_role_id, m.role_id, r.name AS dashboard_role_name,
                       dr.name AS discord_role_name, dr.color AS discord_role_color,
                       m.created_by, m.created_at
                FROM dashboard_discord_role_mappings m
                JOIN dashboard_roles r ON r.id = m.role_id
                LEFT JOIN dashboard_discord_roles dr ON dr.id = m.discord_role_id
                ORDER BY r.name, COALESCE(dr.position, 0) DESC
                """
            )
        ] if _table_exists(connection, "dashboard_discord_roles") else []
        assignments = [dict(row) for row in connection.execute(
            """
            SELECT a.user_id, a.role_id, a.source, a.source_reference, r.name AS role_name
            FROM dashboard_user_role_assignments a
            JOIN dashboard_roles r ON r.id = a.role_id
            ORDER BY a.user_id, r.name
            """
        )]
        overrides = [dict(row) for row in connection.execute(
            """
            SELECT user_id, permission_key, allowed, changed_by, updated_at
            FROM dashboard_user_permission_overrides
            ORDER BY user_id, permission_key
            """
        )]
    by_user: dict[int, list[dict[str, Any]]] = {}
    for assignment in assignments:
        by_user.setdefault(int(assignment["user_id"]), []).append(assignment)
    enriched_users = []
    for user in users:
        item = dict(user)
        item["assignments"] = by_user.get(int(item["id"]), [])
        item["permissions"] = sorted(permissions_for_user(int(item["id"])))
        item["overrides"] = [
            override for override in overrides
            if int(override["user_id"]) == int(item["id"])
        ]
        enriched_users.append(item)
    return {
        "roles": roles,
        "permissions": permission_catalog(),
        "mappings": mappings,
        "users": enriched_users,
    }


def record_audit(
    *, action: str, actor_user_id: Optional[int] = None, actor_label: str = "anonymous",
    target_type: str = "", target_id: str = "", before: Any = None,
    after: Any = None, success: bool = True, error: Optional[str] = None,
    request_id: Optional[str] = None,
) -> str:
    initialize_rbac_schema()
    correlation_id = request_id or uuid.uuid4().hex
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO dashboard_audit_log(
                request_id, actor_user_id, actor_label, action, target_type,
                target_id, before_json, after_json, success, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correlation_id, actor_user_id, str(actor_label or "anonymous")[:120],
                str(action)[:120], str(target_type)[:80], str(target_id)[:160],
                _safe_json(before), _safe_json(after), 1 if success else 0,
                str(error or "")[:500] or None, _utc_now(),
            ),
        )
        connection.commit()
    return correlation_id


def _safe_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    redacted = _redact(value)
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)[:20_000]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).upper()
            if any(marker in normalized for marker in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")):
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    return value


def list_audit_events(*, limit: int = 100, action: str = "", actor: str = "") -> list[dict[str, Any]]:
    initialize_rbac_schema()
    clauses = []
    values: list[Any] = []
    if action:
        clauses.append("action LIKE ?")
        values.append(f"%{action[:80]}%")
    if actor:
        clauses.append("actor_label LIKE ?")
        values.append(f"%{actor[:80]}%")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    values.append(max(1, min(int(limit), 500)))
    with _connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, request_id, actor_user_id, actor_label, action,
                       target_type, target_id, before_json, after_json,
                       success, error, created_at
                FROM dashboard_audit_log{where}
                ORDER BY id DESC LIMIT ?
                """,
                tuple(values),
            )
        ]
