"""Dashboard user storage and password/bootstrap authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from utils.settings import settings_database_path
from utils.sqlite import configure_sync_connection


ALLOWED_ROLES = {"owner", "admin", "viewer"}
WRITABLE_ROLES = {"owner", "admin"}
PBKDF2_ITERATIONS = 600_000


def _connect() -> sqlite3.Connection:
    path = settings_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    return configure_sync_connection(connection)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, expected = str(encoded or "").split(
            "$",
            3,
        )
        iterations = int(raw_iterations)
        salt = bytes.fromhex(raw_salt)
    except (TypeError, ValueError):
        return False
    if algorithm != "pbkdf2_sha256" or iterations < 100_000:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    ).hex()
    return hmac.compare_digest(actual, expected)


def initialize_dashboard_users() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                password_hash TEXT,
                discord_user_id TEXT UNIQUE,
                discord_username TEXT,
                discord_global_name TEXT,
                discord_avatar TEXT,
                role TEXT NOT NULL DEFAULT 'admin',
                status TEXT NOT NULL DEFAULT 'active',
                auth_provider TEXT NOT NULL DEFAULT 'password',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login_at TEXT
            )
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(dashboard_users)"
            ).fetchall()
        }
        additions = (
            ("username", "TEXT"),
            ("password_hash", "TEXT"),
            ("discord_user_id", "TEXT"),
            ("discord_username", "TEXT"),
            ("discord_global_name", "TEXT"),
            ("discord_avatar", "TEXT"),
            ("role", "TEXT NOT NULL DEFAULT 'admin'"),
            ("status", "TEXT NOT NULL DEFAULT 'active'"),
            ("auth_provider", "TEXT NOT NULL DEFAULT 'password'"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
            ("last_login_at", "TEXT"),
            ("discord_guild_id", "TEXT"),
            ("discord_role_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("discord_verified_at", "TEXT"),
            ("discord_verification_status", "TEXT NOT NULL DEFAULT 'not_required'"),
            ("access_source", "TEXT NOT NULL DEFAULT 'legacy'"),
        )
        for name, definition in additions:
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE dashboard_users ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_users_discord
            ON dashboard_users (discord_user_id)
            WHERE discord_user_id IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_users_username
            ON dashboard_users (username)
            WHERE username IS NOT NULL
            """
        )
        count = int(
            connection.execute("SELECT COUNT(*) FROM dashboard_users").fetchone()[0]
        )
        username = os.getenv("DASHBOARD_USERNAME", "admin").strip() or "admin"
        password = os.getenv("DASHBOARD_PASSWORD", "")
        if count == 0 and username and password:
            connection.execute(
                """
                INSERT INTO dashboard_users (
                    username, password_hash, role, status, auth_provider,
                    created_at, updated_at
                ) VALUES (?, ?, 'owner', 'active', 'password',
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (username, hash_password(password)),
            )
        connection.commit()


def _user_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    initialize_dashboard_users()
    with _connect() as connection:
        return _user_dict(
            connection.execute(
                "SELECT * FROM dashboard_users WHERE id = ?",
                (user_id,),
            ).fetchone()
        )


def get_user_by_discord_id(discord_user_id: str) -> dict[str, Any] | None:
    initialize_dashboard_users()
    with _connect() as connection:
        return _user_dict(
            connection.execute(
                "SELECT * FROM dashboard_users WHERE discord_user_id = ?",
                (str(discord_user_id),),
            ).fetchone()
        )


def authenticate_password(username: str, password: str) -> dict[str, Any] | None:
    initialize_dashboard_users()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM dashboard_users
            WHERE username = ? AND password_hash IS NOT NULL
            """,
            (str(username).strip(),),
        ).fetchone()
        if (
            row is None
            or str(row["status"]).casefold() != "active"
            or not verify_password(password, row["password_hash"])
        ):
            return None
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            UPDATE dashboard_users
            SET last_login_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, row["id"]),
        )
        connection.commit()
        return _user_dict(
            connection.execute(
                "SELECT * FROM dashboard_users WHERE id = ?",
                (row["id"],),
            ).fetchone()
        )


def default_discord_role() -> str:
    configured = os.getenv("DASHBOARD_DISCORD_DEFAULT_ROLE", "admin").strip().casefold()
    return configured if configured in {"admin", "viewer"} else "admin"


def parse_allowed_discord_user_ids() -> set[str]:
    return {
        item
        for item in (
            value.strip()
            for value in os.getenv(
                "DASHBOARD_DISCORD_ALLOWED_USER_IDS",
                "",
            ).replace(",", " ").split()
        )
        if item.isdigit() and 17 <= len(item) <= 20
    }


def parse_allowed_discord_role_ids() -> set[str]:
    return {
        item
        for item in (
            value.strip()
            for value in os.getenv(
                "DASHBOARD_DISCORD_ALLOWED_ROLE_IDS",
                "",
            ).replace(",", " ").split()
        )
        if item.isdigit() and 17 <= len(item) <= 20
    }


def upsert_discord_user(identity: dict[str, Any]) -> dict[str, Any]:
    initialize_dashboard_users()
    discord_user_id = str(identity.get("id", "")).strip()
    if not discord_user_id.isdigit():
        raise ValueError("Discord returned an invalid user identity.")
    guild_id = str(identity.get("_guild_id") or os.getenv("GUILD_ID", "")).strip()
    member = identity.get("_guild_member")
    if not guild_id or not isinstance(member, dict):
        raise PermissionError(
            "Your Discord server membership could not be verified. Please try again."
        )
    if bool(member.get("pending")):
        raise PermissionError(
            "Complete this server's membership screening before using the dashboard."
        )
    member_user_id = str((member.get("user") or {}).get("id") or discord_user_id).strip()
    if member_user_id != discord_user_id:
        raise PermissionError("Discord returned a mismatched server membership.")
    role_ids = sorted(
        {
            str(value).strip()
            for value in (member.get("roles") or [])
            if str(value).strip().isdigit()
        }
    )
    from dashboard.rbac import has_mapped_discord_role, sync_discord_role_assignments

    direct_allowed = discord_user_id in parse_allowed_discord_user_ids()
    legacy_role_allowed = bool(set(role_ids) & parse_allowed_discord_role_ids())
    mapped_role_allowed = has_mapped_discord_role(role_ids)
    username = str(identity.get("username", "")).strip()[:100]
    global_name = str(identity.get("global_name") or "").strip()[:100] or None
    avatar = str(identity.get("avatar") or "").strip()[:200] or None
    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM dashboard_users WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["status"]).casefold() != "active":
                raise PermissionError("This dashboard user is disabled.")
            access_source = str(existing["access_source"] or "legacy").casefold()
            if access_source in {"discord_role", "direct"} and not (
                direct_allowed or mapped_role_allowed or legacy_role_allowed
            ):
                sync_discord_role_assignments(int(existing["id"]), role_ids)
                raise PermissionError(
                    "Your current Discord account and roles no longer grant dashboard access."
                )
            if direct_allowed:
                next_source = "direct"
                next_role = default_discord_role()
            elif mapped_role_allowed or legacy_role_allowed:
                next_source = "discord_role"
                next_role = "viewer"
            else:
                next_source = access_source
                next_role = str(existing["role"] or "viewer")
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                UPDATE dashboard_users
                SET discord_username = ?, discord_global_name = ?,
                    discord_avatar = ?, auth_provider = 'discord',
                    last_login_at = ?, updated_at = ?, discord_guild_id = ?,
                    discord_role_ids_json = ?, discord_verified_at = ?,
                    discord_verification_status = 'verified',
                    access_source = ?, role = ?
                WHERE id = ?
                """,
                (
                    username, global_name, avatar, now, now, guild_id,
                    json.dumps(role_ids), now,
                    next_source, next_role,
                    existing["id"],
                ),
            )
            connection.commit()
            sync_discord_role_assignments(int(existing["id"]), role_ids)
            return dict(
                connection.execute(
                    "SELECT * FROM dashboard_users WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
            )
        if not (direct_allowed or mapped_role_allowed or legacy_role_allowed):
            raise PermissionError(
                "Your Discord account is not approved for dashboard access."
            )
        role = default_discord_role() if direct_allowed else "viewer"
        now = datetime.now(timezone.utc).isoformat()
        cursor = connection.execute(
            """
            INSERT INTO dashboard_users (
                username, discord_user_id, discord_username,
                discord_global_name, discord_avatar, role, status,
                auth_provider, created_at, updated_at, last_login_at,
                discord_guild_id, discord_role_ids_json, discord_verified_at,
                discord_verification_status, access_source
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 'discord', ?, ?, ?, ?, ?, ?,
                      'verified', ?)
            """,
            (
                f"discord:{discord_user_id}",
                discord_user_id,
                username,
                global_name,
                avatar,
                role,
                now,
                now,
                now,
                guild_id,
                json.dumps(role_ids),
                now,
                "direct" if direct_allowed else "discord_role",
            ),
        )
        connection.commit()
        user = dict(
            connection.execute(
                "SELECT * FROM dashboard_users WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        )
        sync_discord_role_assignments(int(user["id"]), role_ids)
        return user


def list_dashboard_users() -> list[dict[str, Any]]:
    initialize_dashboard_users()
    with _connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, username, discord_user_id, discord_username,
                       discord_global_name, role, status, auth_provider,
                       created_at, updated_at, last_login_at,
                       discord_guild_id, discord_role_ids_json,
                       discord_verified_at, discord_verification_status,
                       access_source
                FROM dashboard_users
                ORDER BY CASE role
                    WHEN 'owner' THEN 0
                    WHEN 'admin' THEN 1
                    ELSE 2
                END, id
                """
            ).fetchall()
        ]
