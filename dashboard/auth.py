from __future__ import annotations

import hmac
import os
import secrets

from fastapi import Request


SESSION_USER_KEY = "dashboard_user"
CSRF_TOKEN_KEY = "csrf_token"


def configured_username() -> str:
    return os.getenv("DASHBOARD_USERNAME", "admin")


def credentials_are_valid(username: str, password: str) -> bool:
    expected_username = configured_username()
    expected_password = os.getenv("DASHBOARD_PASSWORD", "")
    if not expected_password:
        return False
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password, expected_password
    )


def login_user(request: Request) -> None:
    request.session[SESSION_USER_KEY] = configured_username()


def logout_user(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return hmac.compare_digest(
        str(request.session.get(SESSION_USER_KEY, "")),
        configured_username(),
    )


def csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_TOKEN_KEY] = token
    return str(token)


def csrf_is_valid(request: Request, submitted_token: str) -> bool:
    expected_token = str(request.session.get(CSRF_TOKEN_KEY, ""))
    return bool(expected_token) and hmac.compare_digest(expected_token, submitted_token)
