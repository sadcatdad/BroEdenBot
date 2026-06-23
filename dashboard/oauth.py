"""Minimal Discord OAuth identify flow for the dashboard."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import httpx


DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"


class DiscordOAuthError(RuntimeError):
    pass


def discord_oauth_configured() -> bool:
    mode = os.getenv("DASHBOARD_AUTH_MODE", "").strip().casefold()
    return mode in {"discord", "hybrid"} and all(
        os.getenv(name, "").strip()
        for name in (
            "DISCORD_OAUTH_CLIENT_ID",
            "DISCORD_OAUTH_CLIENT_SECRET",
            "DISCORD_OAUTH_REDIRECT_URI",
        )
    )


def discord_authorize_url(state: str) -> str:
    if not discord_oauth_configured():
        raise DiscordOAuthError("Discord login is not configured.")
    parameters = {
        "client_id": os.getenv("DISCORD_OAUTH_CLIENT_ID", "").strip(),
        "redirect_uri": os.getenv("DISCORD_OAUTH_REDIRECT_URI", "").strip(),
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    return f"{DISCORD_AUTHORIZE_URL}?{urlencode(parameters)}"


async def fetch_discord_identity(code: str) -> dict[str, Any]:
    if not discord_oauth_configured():
        raise DiscordOAuthError("Discord login is not configured.")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_response = await client.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": os.getenv("DISCORD_OAUTH_CLIENT_ID", "").strip(),
                    "client_secret": os.getenv(
                        "DISCORD_OAUTH_CLIENT_SECRET",
                        "",
                    ).strip(),
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": os.getenv(
                        "DISCORD_OAUTH_REDIRECT_URI",
                        "",
                    ).strip(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_response.raise_for_status()
            access_token = str(token_response.json().get("access_token", "")).strip()
            if not access_token:
                raise DiscordOAuthError("Discord token exchange failed.")
            identity_response = await client.get(
                DISCORD_USER_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            identity_response.raise_for_status()
            identity = identity_response.json()
    except DiscordOAuthError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise DiscordOAuthError("Discord login could not be completed.") from exc
    if not isinstance(identity, dict) or not str(identity.get("id", "")).isdigit():
        raise DiscordOAuthError("Discord identity could not be verified.")
    return identity
