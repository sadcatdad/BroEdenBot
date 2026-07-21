from __future__ import annotations

import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request

from dashboard.rbac import permissions_for_user, role_names_for_user
from dashboard.users import authenticate_password, get_user_by_id


SESSION_USER_KEY = "dashboard_user"
SESSION_USERNAME_KEY = "dashboard_username"
SESSION_USER_ID_KEY = "dashboard_user_id"
SESSION_ROLE_KEY = "dashboard_role"
SESSION_PROVIDER_KEY = "auth_provider"
SESSION_DISCORD_USER_ID_KEY = "discord_user_id"
CSRF_TOKEN_KEY = "csrf_token"
OAUTH_STATE_KEY = "discord_oauth_state"


def configured_username() -> str:
    return os.getenv("DASHBOARD_USERNAME", "admin")


def credentials_are_valid(username: str, password: str) -> bool:
    return authenticate_password(username, password) is not None


def login_user(
    request: Request,
    user: dict | None = None,
    *,
    auth_provider: str = "password",
) -> None:
    if user is None:
        user = authenticate_password(
            configured_username(),
            os.getenv("DASHBOARD_PASSWORD", ""),
        )
    if user is None:
        raise ValueError("Dashboard user could not be authenticated.")
    request.session.clear()
    display_name = (
        user.get("discord_global_name")
        or user.get("discord_username")
        or user.get("username")
        or "dashboard"
    )
    request.session[SESSION_USER_ID_KEY] = int(user["id"])
    request.session[SESSION_USER_KEY] = str(display_name)
    request.session[SESSION_USERNAME_KEY] = str(display_name)
    role = str(user.get("role") or "viewer").casefold()
    request.session[SESSION_ROLE_KEY] = (
        role if role in {"owner", "admin", "viewer"} else "viewer"
    )
    request.session[SESSION_PROVIDER_KEY] = auth_provider
    discord_user_id = user.get("discord_user_id")
    if discord_user_id:
        request.session[SESSION_DISCORD_USER_ID_KEY] = str(discord_user_id)
        request.session["discord_verified_at"] = str(
            user.get("discord_verified_at") or ""
        )
    else:
        request.session.pop(SESSION_DISCORD_USER_ID_KEY, None)


def logout_user(request: Request) -> None:
    request.session.clear()


def _database_user(request: Request) -> dict | None:
    cached = getattr(request.state, "dashboard_database_user", None)
    if cached is not None:
        return cached or None
    try:
        user_id = int(request.session.get(SESSION_USER_ID_KEY) or 0)
    except (TypeError, ValueError):
        user_id = 0
    user = get_user_by_id(user_id) if user_id else None
    if user is None or str(user.get("status") or "").casefold() != "active":
        request.session.clear()
        request.state.dashboard_database_user = {}
        return None
    if str(user.get("auth_provider") or "").casefold() == "discord":
        if str(user.get("discord_verification_status") or "").casefold() != "verified":
            request.session.clear()
            request.state.dashboard_database_user = {}
            return None
        raw_verified_at = str(user.get("discord_verified_at") or "")
        try:
            verified_at = datetime.fromisoformat(raw_verified_at.replace("Z", "+00:00"))
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=timezone.utc)
        except ValueError:
            request.session.clear()
            request.state.dashboard_database_user = {}
            return None
        try:
            max_age_minutes = max(
                5,
                min(
                    int(os.getenv("DASHBOARD_DISCORD_REVERIFY_MINUTES", "60")),
                    24 * 60,
                ),
            )
        except ValueError:
            max_age_minutes = 60
        if datetime.now(timezone.utc) - verified_at > timedelta(minutes=max_age_minutes):
            request.session.clear()
            request.state.dashboard_database_user = {}
            return None
    request.state.dashboard_database_user = user
    return user


def is_authenticated(request: Request) -> bool:
    return _database_user(request) is not None


def current_user(request: Request) -> dict:
    database_user = _database_user(request)
    if database_user is None:
        return {}
    user_id = int(database_user["id"])
    display_name = (
        database_user.get("discord_global_name")
        or database_user.get("discord_username")
        or database_user.get("username")
        or "dashboard"
    )
    role_names = role_names_for_user(user_id)
    return {
        "id": user_id,
        "username": str(display_name),
        "role": role_names[0] if role_names else str(database_user.get("role") or "viewer"),
        "roles": role_names,
        "auth_provider": str(database_user.get("auth_provider") or "password"),
        "discord_user_id": database_user.get("discord_user_id"),
        "access_source": str(database_user.get("access_source") or "legacy"),
    }


def has_write_access(request: Request) -> bool:
    return any(
        permission.endswith(".manage")
        or permission in {"bot.restart", "access.manage", "discord_metadata.refresh"}
        for permission in current_permissions(request)
    )


def is_admin(request: Request) -> bool:
    return has_permission(request, "access.manage")


def current_permissions(request: Request) -> set[str]:
    user = _database_user(request)
    if user is None:
        return set()
    cached = getattr(request.state, "dashboard_permissions", None)
    if cached is None:
        cached = permissions_for_user(int(user["id"]))
        request.state.dashboard_permissions = cached
    return set(cached)


def has_permission(request: Request, permission: str) -> bool:
    return str(permission) in current_permissions(request)


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_TOKEN_KEY] = token
    return str(token)


def csrf_is_valid(request: Request, submitted_token: str) -> bool:
    expected_token = str(request.session.get(CSRF_TOKEN_KEY, ""))
    return bool(expected_token) and hmac.compare_digest(expected_token, submitted_token)
