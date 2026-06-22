from __future__ import annotations

import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dashboard.auth import (
    credentials_are_valid,
    csrf_is_valid,
    csrf_token,
    is_authenticated,
    login_user,
    logout_user,
)
from dashboard.db import (
    bank_overview,
    database_status,
    find_bank_database_path,
    find_database_path,
    import_history,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

SAFE_SETTING_NAMES = (
    "GUILD_ID",
    "MODAI_MODEL",
    "MODAI_FALLBACK_MODEL",
    "ASK_MODEL",
    "ASK_FALLBACK_MODEL",
    "ASK_ALLOWED_CHANNEL_IDS",
    "ASK_COOLDOWN_SECONDS",
    "MODAI_ALLOWED_ROLE_IDS",
    "STAFF_NOTES_ALLOWED_ROLE_IDS",
    "STATS_ALLOWED_ROLE_IDS",
    "VCSTATS_ALLOWED_ROLE_IDS",
    "BANK_ALLOWED_ROLE_IDS",
    "VCXP_ENABLED",
    "VCXP_TRIGGER_ROLE_ID",
    "VCXP_MINUTES_PER_PULSE",
    "VCXP_ROLE_REMOVE_DELAY_SECONDS",
    "VCXP_DAILY_PULSE_CAP",
    "VCXP_WEEKLY_PULSE_CAP",
)
SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def safe_setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    looks_like_secret = bool(
        re.fullmatch(
            r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}",
            value,
        )
        or re.match(r"^(?:sk|ghp|github_pat|xox)[-_]", value, re.IGNORECASE)
    )
    if any(marker in name.upper() for marker in SECRET_MARKERS) or looks_like_secret:
        return "configured" if value else "missing"
    return value or "Not configured"


def dashboard_enabled() -> bool:
    return env_flag("DASHBOARD_ENABLED", default=False)


app = FastAPI(
    title="BroEdenBot Local Dashboard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("DASHBOARD_SECRET_KEY", "").strip()
    or secrets.token_urlsafe(48),
    session_cookie="broeden_dashboard_session",
    max_age=60 * 60 * 12,
    same_site="strict",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")


def template_context(request: Request, **values: Any) -> dict[str, Any]:
    return {
        "request": request,
        "current_path": request.url.path,
        "authenticated": is_authenticated(request),
        "csrf_token": csrf_token(request),
        **values,
    }


def login_redirect(request: Request) -> RedirectResponse | None:
    if not dashboard_enabled() or not is_authenticated(request):
        return RedirectResponse(
            url=request.url_for("login_page"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return None


@app.get("/login", response_class=HTMLResponse, name="login_page")
async def login_page(request: Request) -> HTMLResponse:
    if is_authenticated(request) and dashboard_enabled():
        return RedirectResponse(
            url=request.url_for("home"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=template_context(
            request,
            error=None,
            dashboard_enabled=dashboard_enabled(),
        ),
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
) -> HTMLResponse:
    if not dashboard_enabled():
        error = "The dashboard is disabled. Set DASHBOARD_ENABLED=true to use it."
    elif not csrf_is_valid(request, csrf):
        error = "Your login session expired. Please try again."
    elif credentials_are_valid(username, password):
        login_user(request)
        return RedirectResponse(
            url=request.url_for("home"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    else:
        error = "Invalid username or password."
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=template_context(
            request,
            error=error,
            dashboard_enabled=dashboard_enabled(),
        ),
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.post("/logout")
async def logout(request: Request, csrf: str = Form(...)) -> RedirectResponse:
    if csrf_is_valid(request, csrf):
        logout_user(request)
    return RedirectResponse(
        url=request.url_for("login_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/", response_class=HTMLResponse, name="home")
async def home(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    database = database_status(find_database_path())
    bank_database = database_status(find_bank_database_path())
    model_names = (
        "MODAI_MODEL",
        "MODAI_FALLBACK_MODEL",
        "ASK_MODEL",
        "ASK_FALLBACK_MODEL",
    )
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=template_context(
            request,
            page_title="Overview",
            dashboard_status="Online" if dashboard_enabled() else "Disabled",
            bot_name=os.getenv("BOT_NAME", "").strip() or "BroEdenBot",
            bot_configuration_status=(
                "Configured"
                if os.getenv("DISCORD_TOKEN", "").strip()
                and os.getenv("GUILD_ID", "").strip()
                else "Incomplete"
            ),
            guild_id=safe_setting("GUILD_ID"),
            database=database,
            bank_database=bank_database,
            discord_token_status=safe_setting("DISCORD_TOKEN"),
            gemini_key_status=safe_setting("GEMINI_API_KEY"),
            gemini_models_configured=all(os.getenv(name, "").strip() for name in model_names),
            current_time=datetime.now().astimezone(),
        ),
    )


@app.get("/settings", response_class=HTMLResponse, name="settings")
async def settings(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    values = [{"name": name, "value": safe_setting(name)} for name in SAFE_SETTING_NAMES]
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=template_context(request, page_title="Settings Viewer", settings=values),
    )


@app.get("/bank", response_class=HTMLResponse, name="bank")
async def bank(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="bank.html",
        context=template_context(
            request,
            page_title="Bank Overview",
            bank=bank_overview(),
        ),
    )


@app.get("/imports", response_class=HTMLResponse, name="imports")
async def imports(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="imports.html",
        context=template_context(
            request,
            page_title="Import History",
            history=import_history(),
        ),
    )


@app.get("/health", response_class=JSONResponse)
async def health() -> JSONResponse:
    database = database_status(find_database_path())
    return JSONResponse(
        {
            "status": "ok" if dashboard_enabled() else "disabled",
            "dashboard_enabled": dashboard_enabled(),
            "database": {
                "exists": database["exists"],
                "readable": database["readable"],
            },
            "time": datetime.now().astimezone().isoformat(),
        }
    )


def main() -> None:
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "3000"))
    uvicorn.run("dashboard.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
