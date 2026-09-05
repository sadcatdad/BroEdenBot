"""Request boundaries and persistent password-login throttling."""

from __future__ import annotations

import hashlib
import math
import time

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from dashboard.users import _connect


LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
MAX_LOGIN_CLIENTS = 1024
FORM_BODY_LIMIT = 1024 * 1024
UPLOAD_BODY_LIMIT = 12 * 1024 * 1024
DROP_BODY_LIMIT = 81 * 1024 * 1024  # Ten 8 MiB images plus form metadata.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' https: http: data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self' https://discord.com",
    )
)


def _client_key(client_host: str) -> str:
    return hashlib.sha256(client_host.encode("utf-8")).hexdigest()


def reserve_login_attempt(client_host: str) -> int:
    """Reserve an attempt atomically across workers; return retry delay or zero."""
    now = time.time()
    with _connect() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_login_attempts (
                client_key TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                attempts INTEGER NOT NULL
            )
        """)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM dashboard_login_attempts WHERE started_at <= ?",
            (now - LOGIN_WINDOW_SECONDS,),
        )
        key = _client_key(client_host)
        row = connection.execute(
            "SELECT started_at, attempts FROM dashboard_login_attempts WHERE client_key = ?",
            (key,),
        ).fetchone()
        if row is not None and row["attempts"] >= LOGIN_ATTEMPTS:
            return max(1, math.ceil(row["started_at"] + LOGIN_WINDOW_SECONDS - now))
        if row is None:
            count, oldest = connection.execute(
                "SELECT COUNT(*), MIN(started_at) FROM dashboard_login_attempts"
            ).fetchone()
            if count >= MAX_LOGIN_CLIENTS:
                return max(1, math.ceil(oldest + LOGIN_WINDOW_SECONDS - now))
            connection.execute(
                "INSERT INTO dashboard_login_attempts VALUES (?, ?, 1)",
                (key, now),
            )
        else:
            connection.execute(
                "UPDATE dashboard_login_attempts SET attempts = attempts + 1 WHERE client_key = ?",
                (key,),
            )
    return 0


def clear_login_attempts(client_host: str) -> None:
    with _connect() as connection:
        connection.execute(
            "DELETE FROM dashboard_login_attempts WHERE client_key = ?",
            (_client_key(client_host),),
        )


class DashboardSecurityMiddleware:
    """Bound streamed bodies before parsing and apply shared response headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = Headers(scope=scope)
        limit = FORM_BODY_LIMIT
        if headers.get("content-type", "").lower().startswith("multipart/form-data"):
            limit = (
                DROP_BODY_LIMIT
                if path.startswith("/events/drops")
                else UPLOAD_BODY_LIMIT
            )

        async def secure_send(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Content-Type-Options"] = "nosniff"
                response_headers["X-Frame-Options"] = "DENY"
                response_headers["Referrer-Policy"] = "no-referrer"
                response_headers["Permissions-Policy"] = (
                    "camera=(), microphone=(), geolocation=()"
                )
                response_headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                if not path.startswith("/static/"):
                    response_headers["Cache-Control"] = "private, no-store"
                    response_headers["Pragma"] = "no-cache"
            await send(message)

        async def reject(status_code, detail):
            await JSONResponse({"detail": detail}, status_code=status_code)(
                scope, receive, secure_send
            )

        raw_length = headers.get("content-length")
        if raw_length is not None:
            if (
                not raw_length.isascii()
                or not raw_length.isdigit()
                or len(raw_length) > 20
            ):
                return await reject(400, "Invalid Content-Length.")
            if int(raw_length) > limit:
                return await reject(413, "Request body is too large.")

        received = 0
        exceeded = False
        rejected = False

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise HTTPException(413, "Request body is too large.")
            return message

        async def limited_send(message):
            nonlocal rejected
            if exceeded:
                # Framework form parsing may translate receive errors into HTTP 400.
                if not rejected:
                    rejected = True
                    await reject(413, "Request body is too large.")
                return
            await secure_send(message)

        await self.app(scope, limited_receive, limited_send)
