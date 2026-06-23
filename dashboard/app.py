from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
from dashboard.operations import (
    backup_database,
    operations_database_status,
    restart_service,
    service_logs,
    service_status,
    system_status,
)
from utils.settings import (
    EDITABLE_SETTING_KEYS,
    initialize_settings_from_env,
    is_forbidden_key,
    recent_setting_changes,
    set_setting,
    settings_for_dashboard,
)
from utils.stats_manager import (
    archive_stat,
    export_stat_csv,
    get_stat,
    initialize_stats_manager_schema,
    list_stats,
    queue_stat_refresh,
    update_stat,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

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


def validate_dashboard_config() -> None:
    if not dashboard_enabled():
        return
    for name in ("DASHBOARD_SECRET_KEY", "DASHBOARD_PASSWORD"):
        if not os.getenv(name, "").strip():
            raise RuntimeError(
                f"{name} is required when DASHBOARD_ENABLED=true. "
                "Set it in the project .env file before starting the dashboard."
            )


validate_dashboard_config()
if dashboard_enabled():
    initialize_settings_from_env()
    initialize_stats_manager_schema()

app = FastAPI(
    title="BroEdenBot Local Dashboard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("DASHBOARD_SECRET_KEY", "").strip()
    or "dashboard-disabled",
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


async def require_action_csrf(request: Request) -> None:
    form = await request.form()
    if not csrf_is_valid(request, str(form.get("csrf", ""))):
        raise HTTPException(status_code=400, detail="Invalid CSRF token.")


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
    message = request.session.pop("settings_message", None)
    error = request.session.pop("settings_error", None)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=template_context(
            request,
            page_title="Settings",
            sections=settings_for_dashboard(),
            recent_changes=recent_setting_changes(),
            message=message,
            error=error,
        ),
    )


@app.post("/settings/update", name="update_setting")
async def update_setting(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    form = await request.form()
    key = str(form.get("key", "")).strip()
    value = str(form.get("value", ""))
    if is_forbidden_key(key) or key not in EDITABLE_SETTING_KEYS:
        raise HTTPException(status_code=400, detail="This setting is not editable.")
    try:
        normalized = set_setting(
            key,
            value,
            changed_by=str(request.session.get("dashboard_user", "dashboard")),
        )
    except ValueError as exc:
        request.session["settings_error"] = f"{key or 'Setting'}: {exc}"
    else:
        request.session["settings_message"] = f"{key} saved as {normalized or '(blank)'}."
    return RedirectResponse(
        url=request.url_for("settings"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/operations", response_class=HTMLResponse, name="operations_page")
async def operations_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    message = request.session.pop("operations_message", None)
    error = request.session.pop("operations_error", None)
    return templates.TemplateResponse(
        request=request,
        name="operations.html",
        context=template_context(
            request,
            page_title="Bot Operations",
            services=[
                service_status("bot"),
                service_status("dashboard"),
            ],
            logs=[
                service_logs("bot"),
                service_logs("dashboard"),
            ],
            system=system_status(),
            databases=operations_database_status(),
            message=message,
            error=error,
        ),
    )


def operations_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("operations_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def stats_redirect(request: Request, stat_id: str | None = None) -> RedirectResponse:
    url = (
        request.url_for("stats_detail", stat_id=stat_id)
        if stat_id
        else request.url_for("stats_page")
    )
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/stats", response_class=HTMLResponse, name="stats_page")
async def stats_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context=template_context(
            request,
            page_title="Stats Graphics",
            stats=list_stats(),
            message=request.session.pop("stats_message", None),
            error=request.session.pop("stats_error", None),
        ),
    )


@app.get("/stats/{stat_id}", response_class=HTMLResponse, name="stats_detail")
async def stats_detail(request: Request, stat_id: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    try:
        record = get_stat(stat_id)
    except ValueError:
        record = None
    if record is None:
        raise HTTPException(status_code=404, detail="Stat was not found.")
    return templates.TemplateResponse(
        request=request,
        name="stats_detail.html",
        context=template_context(
            request,
            page_title=record["title"],
            stat=record,
            message=request.session.pop("stats_message", None),
            error=request.session.pop("stats_error", None),
        ),
    )


@app.get("/stats/{stat_id}/edit", response_class=HTMLResponse, name="stats_edit")
async def stats_edit(request: Request, stat_id: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    try:
        record = get_stat(stat_id)
    except ValueError:
        record = None
    if record is None:
        raise HTTPException(status_code=404, detail="Stat was not found.")
    if not record["editable"]:
        request.session["stats_error"] = (
            "This activity report does not support dashboard editing."
        )
        return stats_redirect(request, stat_id)
    return templates.TemplateResponse(
        request=request,
        name="stats_edit.html",
        context=template_context(
            request,
            page_title=f"Edit {record['title']}",
            stat=record,
            error=request.session.pop("stats_error", None),
        ),
    )


@app.post("/stats/{stat_id}/edit", name="stats_update")
async def stats_update(request: Request, stat_id: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    form = await request.form()
    try:
        update_stat(
            stat_id,
            title=str(form.get("title", "")),
            body=str(form.get("body", "")),
            image_url=str(form.get("image_url", "")),
        )
    except ValueError as exc:
        request.session["stats_error"] = str(exc)
        return RedirectResponse(
            url=request.url_for("stats_edit", stat_id=stat_id),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["stats_message"] = (
        "Configuration saved. Queue a refresh to update the Discord post."
    )
    return stats_redirect(request, stat_id)


@app.post("/stats/{stat_id}/refresh", name="stats_refresh")
async def stats_refresh(request: Request, stat_id: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    try:
        action_id = queue_stat_refresh(
            stat_id,
            str(request.session.get("dashboard_user", "dashboard")),
        )
    except ValueError as exc:
        request.session["stats_error"] = str(exc)
    else:
        request.session["stats_message"] = (
            f"Refresh queued as dashboard action #{action_id}. "
            "The Discord bot process will handle it."
        )
    return stats_redirect(request, stat_id)


@app.post("/stats/{stat_id}/archive", name="stats_archive")
async def stats_archive(request: Request, stat_id: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    try:
        archive_stat(stat_id)
    except ValueError as exc:
        request.session["stats_error"] = str(exc)
    else:
        request.session["stats_message"] = (
            f"{stat_id} archived. Its Discord message was not deleted."
        )
    return stats_redirect(request, stat_id)


@app.get("/stats/{stat_id}/export.csv", name="stats_export")
async def stats_export(request: Request, stat_id: str):
    if redirect := login_redirect(request):
        return redirect
    try:
        data = export_stat_csv(stat_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Stat was not found.")
    if data is None:
        request.session["stats_error"] = (
            "No stored member snapshot is available yet. "
            "Queue a refresh and try again after the bot processes it."
        )
        return stats_redirect(request, stat_id)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{stat_id}-members.csv"'
        },
    )


@app.post("/operations/restart-bot", name="restart_bot")
async def restart_bot(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    ok, message = restart_service("bot")
    request.session["operations_message" if ok else "operations_error"] = message
    return operations_redirect(request)


@app.post("/operations/restart-dashboard", name="restart_dashboard")
async def restart_dashboard(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    ok, message = restart_service("dashboard")
    request.session["operations_message" if ok else "operations_error"] = message
    return operations_redirect(request)


@app.post("/operations/backup-database", name="backup_active_database")
async def backup_active_database(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    try:
        destination = backup_database()
    except (OSError, sqlite3.Error) as exc:
        request.session["operations_error"] = (
            f"Database backup failed: {type(exc).__name__}: {exc}"
        )
    else:
        request.session["operations_message"] = (
            f"Database backup created: {destination.name}"
        )
    return operations_redirect(request)


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
