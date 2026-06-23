from __future__ import annotations

import hmac
import os
import secrets

from fastapi import Request

from dashboard.users import authenticate_password


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
    else:
        request.session.pop(SESSION_DISCORD_USER_ID_KEY, None)


def logout_user(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return bool(
        request.session.get(SESSION_USER_ID_KEY)
        and (
            request.session.get(SESSION_USERNAME_KEY)
            or request.session.get(SESSION_USER_KEY)
        )
        and request.session.get(SESSION_ROLE_KEY) in {"owner", "admin", "viewer"}
    )


def current_user(request: Request) -> dict:
    return {
        "id": request.session.get(SESSION_USER_ID_KEY),
        "username": (
            request.session.get(SESSION_USERNAME_KEY)
            or request.session.get(SESSION_USER_KEY)
        ),
        "role": request.session.get(SESSION_ROLE_KEY),
        "auth_provider": request.session.get(SESSION_PROVIDER_KEY),
        "discord_user_id": request.session.get(SESSION_DISCORD_USER_ID_KEY),
    }


def has_write_access(request: Request) -> bool:
    return request.session.get(SESSION_ROLE_KEY) in {"owner", "admin"}


def is_admin(request: Request) -> bool:
    return has_write_access(request)


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_TOKEN_KEY] = token
    return str(token)


def csrf_is_valid(request: Request, submitted_token: str) -> bool:
    expected_token = str(request.session.get(CSRF_TOKEN_KEY, ""))
    return bool(expected_token) and hmac.compare_digest(expected_token, submitted_token)
