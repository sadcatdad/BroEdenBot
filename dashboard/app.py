from __future__ import annotations

import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dashboard.auth import (
    OAUTH_STATE_KEY,
    csrf_is_valid,
    csrf_token,
    current_user,
    has_write_access,
    is_admin,
    is_authenticated,
    login_user,
    logout_user,
)
from dashboard.oauth import (
    DiscordOAuthError,
    discord_authorize_url,
    discord_oauth_configured,
    fetch_discord_identity,
)
from dashboard.db import (
    bank_overview,
    database_status,
    find_bank_database_path,
    find_database_path,
    import_history,
    vcxp_overview,
)
from dashboard.discord_metadata import (
    categories_metadata,
    channels_metadata,
    guild_structure,
    picker_metadata,
    queue_metadata_refresh,
    roles_metadata,
)
from dashboard.operations import (
    backup_database,
    operations_database_status,
    restart_service,
    service_logs,
    service_status,
    system_status,
)
from dashboard.users import (
    authenticate_password,
    initialize_dashboard_users,
    list_dashboard_users,
    upsert_discord_user,
)
from utils.knowledge_manager import (
    document_details,
    initialize_knowledge_schema,
    list_documents,
    queue_knowledge_reindex,
    recent_knowledge_audit,
    save_document,
)
from utils.analytics import (
    LIMITS,
    RANGES,
    export_analytics_csv,
    get_activity_series,
    get_analytics_overview,
    get_channel_leaderboard,
    get_heatmap,
    get_member_leaderboard,
    get_voice_overview,
    validate_export_type,
    validate_limit,
    validate_range,
)
from utils.settings import (
    DEFINITIONS_BY_KEY,
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
    initialize_knowledge_schema()
    initialize_dashboard_users()

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
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")


def template_context(request: Request, **values: Any) -> dict[str, Any]:
    return {
        "request": request,
        "current_path": request.url.path,
        "authenticated": is_authenticated(request),
        "current_user": current_user(request) if is_authenticated(request) else None,
        "can_write": has_write_access(request),
        "can_manage_users": is_admin(request),
        "csrf_token": csrf_token(request),
        **values,
    }


def render_settings_page(
    request: Request,
    *,
    page_title: str = "Settings",
    visible_sections: tuple[str, ...] = (
        "ask",
        "permissions",
        "vcxp",
        "models",
        "dashboard_json",
    ),
    **values: Any,
) -> HTMLResponse:
    message = request.session.pop("settings_message", None)
    error = request.session.pop("settings_error", None)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=template_context(
            request,
            page_title=page_title,
            sections=settings_for_dashboard(),
            visible_sections=visible_sections,
            recent_changes=recent_setting_changes(),
            message=message,
            error=error,
            **values,
        ),
    )


def settings_redirect_for_key(request: Request, key: str) -> str:
    definition = DEFINITIONS_BY_KEY.get(key)
    if definition and definition.section == "dashboard_json":
        return str(request.url_for("settings_discord"))
    if definition and definition.section == "permissions":
        return str(request.url_for("settings_permissions"))
    if definition and definition.section == "advanced":
        return str(request.url_for("settings_advanced"))
    return str(request.url_for("settings"))


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
    if not has_write_access(request):
        raise HTTPException(
            status_code=403,
            detail="Viewer accounts cannot perform dashboard actions.",
        )


def login_response(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=template_context(
            request,
            error=error,
            dashboard_enabled=dashboard_enabled(),
            discord_oauth_enabled=discord_oauth_configured(),
        ),
        status_code=status_code,
    )


@app.get("/login", response_class=HTMLResponse, name="login_page")
async def login_page(request: Request) -> HTMLResponse:
    if is_authenticated(request) and dashboard_enabled():
        return RedirectResponse(
            url=request.url_for("home"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return login_response(request)


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
    else:
        user = authenticate_password(username, password)
        if user is not None:
            login_user(request, user, auth_provider="password")
            return RedirectResponse(
                url=request.url_for("home"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        error = "Invalid username or password."
    return login_response(
        request,
        error=error,
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@app.get("/auth/discord/login", name="discord_login")
async def discord_login(request: Request) -> Response:
    if not dashboard_enabled():
        return RedirectResponse(
            url=request.url_for("login_page"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if not discord_oauth_configured():
        return login_response(
            request,
            error="Discord login is not configured.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    state_token = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_KEY] = state_token
    return RedirectResponse(
        url=discord_authorize_url(state_token),
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/auth/discord/callback", name="discord_callback")
async def discord_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    expected_state = str(request.session.pop(OAUTH_STATE_KEY, ""))
    if error:
        return login_response(
            request,
            error="Discord login was canceled.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not expected_state or not state:
        return login_response(
            request,
            error="Discord login session is missing or expired.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not secrets.compare_digest(expected_state, state):
        return login_response(
            request,
            error="Discord login session could not be verified.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not code:
        return login_response(
            request,
            error="Discord did not return an authorization code.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        identity = await fetch_discord_identity(code)
        user = upsert_discord_user(identity)
    except DiscordOAuthError:
        return login_response(
            request,
            error="Discord login could not be completed. Please try again.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except PermissionError as exc:
        return login_response(
            request,
            error=str(exc),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    except ValueError:
        return login_response(
            request,
            error="Discord identity could not be verified.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    login_user(request, user, auth_provider="discord")
    return RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/logout")
async def logout(request: Request, csrf: str = Form(...)) -> RedirectResponse:
    if csrf_is_valid(request, csrf):
        logout_user(request)
    return RedirectResponse(
        url=request.url_for("login_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/users", response_class=HTMLResponse, name="users_legacy")
async def users_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("users_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/settings/users", response_class=HTMLResponse, name="users_page")
async def users_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    if not is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="Admin or owner access is required.",
        )
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context=template_context(
            request,
            page_title="Dashboard Users",
            users=list_dashboard_users(),
        ),
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
            vcxp=vcxp_overview(),
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
    return render_settings_page(
        request,
        page_title="Bot Configuration",
        visible_sections=("ask", "vcxp", "models"),
    )


@app.get("/settings/permissions", response_class=HTMLResponse, name="settings_permissions")
async def settings_permissions(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return render_settings_page(
        request,
        page_title="Permissions & Access",
        visible_sections=("permissions",),
    )


@app.get("/settings/discord", response_class=HTMLResponse, name="settings_discord")
async def settings_discord(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return render_settings_page(
        request,
        page_title="Discord Roles & Channels",
        visible_sections=("dashboard_json",),
        discord_metadata=picker_metadata(),
    )


@app.post("/settings/discord/refresh", name="refresh_discord_metadata")
async def refresh_discord_metadata(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    action_id = queue_metadata_refresh(
        str(request.session.get("dashboard_user", "dashboard"))
    )
    request.session["settings_message"] = (
        f"Discord metadata refresh queued as dashboard action #{action_id}. "
        "The live bot process will update the snapshot."
    )
    return RedirectResponse(
        url=request.url_for("settings_discord"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/settings/advanced", response_class=HTMLResponse, name="settings_advanced")
async def settings_advanced(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return render_settings_page(
        request,
        page_title="Advanced Settings",
        visible_sections=("advanced",),
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
        url=settings_redirect_for_key(request, key),
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


def knowledge_redirect(
    request: Request,
    doc_key: str | None = None,
) -> RedirectResponse:
    url = (
        request.url_for("knowledge_detail", doc_key=doc_key)
        if doc_key
        else request.url_for("knowledge_page")
    )
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def knowledge_document_or_error(doc_key: str) -> dict[str, Any]:
    try:
        return document_details(doc_key)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="Knowledge document was not found.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def analytics_parameters(
    range_key: str,
    limit: int | str = 25,
    *,
    heatmap: bool = False,
) -> tuple[str, int]:
    try:
        return validate_range(range_key, heatmap=heatmap), validate_limit(limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def analytics_context(
    request: Request,
    *,
    range_key: str,
    limit: int = 25,
    **values: Any,
) -> dict[str, Any]:
    return template_context(
        request,
        range_key=range_key,
        range_label=RANGES[range_key][0],
        ranges=RANGES,
        limit=limit,
        limits=sorted(LIMITS),
        **values,
    )


@app.get("/analytics", response_class=HTMLResponse, name="analytics_page")
async def analytics_page(
    request: Request,
    range: str = "30d",
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, limit = analytics_parameters(range, 25)
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context=analytics_context(
            request,
            page_title="Server Analytics",
            range_key=range_key,
            limit=limit,
            analytics=get_analytics_overview(range_key),
        ),
    )


@app.get("/analytics/exports", response_class=HTMLResponse, name="analytics_exports")
async def analytics_exports(
    request: Request,
    range: str = "30d",
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, limit = analytics_parameters(range, 25)
    exports = [
        {
            "type": "overview",
            "range": range_key,
            "label": "Overview",
            "description": "Top-level server activity metrics.",
        },
        {
            "type": "activity",
            "range": range_key,
            "label": "Activity Analytics",
            "description": "Daily, weekly, and monthly message counts.",
        },
        {
            "type": "channels",
            "range": range_key,
            "label": "Channels",
            "description": "Aggregated channel leaderboard.",
        },
        {
            "type": "members",
            "range": range_key,
            "label": "Members",
            "description": "Aggregated member leaderboard.",
        },
        {
            "type": "voice",
            "range": range_key,
            "label": "VC Analytics",
            "description": "Completed voice session summaries.",
        },
        {
            "type": "heatmap",
            "range": "90d" if range_key == "7d" else range_key,
            "label": "Heatmap",
            "description": "Activity heatmap export.",
        },
    ]
    return templates.TemplateResponse(
        request=request,
        name="analytics_exports.html",
        context=analytics_context(
            request,
            page_title="Analytics Exports",
            range_key=range_key,
            limit=limit,
            exports=exports,
        ),
    )


@app.get(
    "/analytics/activity",
    response_class=HTMLResponse,
    name="analytics_activity",
)
async def analytics_activity(
    request: Request,
    range: str = "30d",
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, limit = analytics_parameters(range, 25)
    return templates.TemplateResponse(
        request=request,
        name="analytics_activity.html",
        context=analytics_context(
            request,
            page_title="Message Activity",
            range_key=range_key,
            limit=limit,
            activity=get_activity_series(range_key),
        ),
    )


@app.get(
    "/analytics/channels",
    response_class=HTMLResponse,
    name="analytics_channels",
)
async def analytics_channels(
    request: Request,
    range: str = "30d",
    limit: int = 25,
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, safe_limit = analytics_parameters(range, limit)
    return templates.TemplateResponse(
        request=request,
        name="analytics_channels.html",
        context=analytics_context(
            request,
            page_title="Channel Analytics",
            range_key=range_key,
            limit=safe_limit,
            channels=get_channel_leaderboard(range_key, safe_limit),
        ),
    )


@app.get(
    "/analytics/members",
    response_class=HTMLResponse,
    name="analytics_members",
)
async def analytics_members(
    request: Request,
    range: str = "30d",
    limit: int = 25,
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, safe_limit = analytics_parameters(range, limit)
    return templates.TemplateResponse(
        request=request,
        name="analytics_members.html",
        context=analytics_context(
            request,
            page_title="Member Analytics",
            range_key=range_key,
            limit=safe_limit,
            members=get_member_leaderboard(range_key, safe_limit),
        ),
    )


@app.get(
    "/analytics/voice",
    response_class=HTMLResponse,
    name="analytics_voice",
)
async def analytics_voice(
    request: Request,
    range: str = "30d",
    limit: int = 25,
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, safe_limit = analytics_parameters(range, limit)
    return templates.TemplateResponse(
        request=request,
        name="analytics_voice.html",
        context=analytics_context(
            request,
            page_title="Voice Analytics",
            range_key=range_key,
            limit=safe_limit,
            voice=get_voice_overview(range_key, safe_limit),
        ),
    )


@app.get(
    "/analytics/heatmap",
    response_class=HTMLResponse,
    name="analytics_heatmap",
)
async def analytics_heatmap(
    request: Request,
    range: str = "30d",
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    range_key, limit = analytics_parameters(range, 25, heatmap=True)
    return templates.TemplateResponse(
        request=request,
        name="analytics_heatmap.html",
        context=analytics_context(
            request,
            page_title="Activity Heatmap",
            range_key=range_key,
            limit=limit,
            heatmap=get_heatmap(range_key),
        ),
    )


@app.get("/analytics/export.csv", name="analytics_export")
async def analytics_export(
    request: Request,
    range: str = "30d",
    type: str = "overview",
) -> Response:
    if redirect := login_redirect(request):
        return redirect
    try:
        export_type = validate_export_type(type)
        range_key = validate_range(range, heatmap=export_type == "heatmap")
        filename, data = export_analytics_csv(range_key, export_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/knowledge", response_class=HTMLResponse, name="knowledge_legacy")
async def knowledge_legacy(request: Request) -> RedirectResponse:
    return knowledge_redirect(request)


@app.get("/settings/knowledge", response_class=HTMLResponse, name="knowledge_page")
async def knowledge_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="knowledge.html",
        context=template_context(
            request,
            page_title="Knowledge Base",
            documents=list_documents(),
            recent_audit=recent_knowledge_audit(),
            message=request.session.pop("knowledge_message", None),
            error=request.session.pop("knowledge_error", None),
        ),
    )


@app.get("/knowledge/{doc_key}", response_class=HTMLResponse, name="knowledge_detail_legacy")
async def knowledge_detail_legacy(request: Request, doc_key: str) -> RedirectResponse:
    return knowledge_redirect(request, doc_key)


@app.get(
    "/settings/knowledge/{doc_key}",
    response_class=HTMLResponse,
    name="knowledge_detail",
)
async def knowledge_detail(request: Request, doc_key: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    document = knowledge_document_or_error(doc_key)
    return templates.TemplateResponse(
        request=request,
        name="knowledge_detail.html",
        context=template_context(
            request,
            page_title=document["display_name"],
            document=document,
            message=request.session.pop("knowledge_message", None),
            error=request.session.pop("knowledge_error", None),
        ),
    )


@app.get("/knowledge/{doc_key}/preview", response_class=HTMLResponse, name="knowledge_preview_legacy")
async def knowledge_preview_legacy(request: Request, doc_key: str) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("knowledge_preview", doc_key=doc_key),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/settings/knowledge/{doc_key}/preview",
    response_class=HTMLResponse,
    name="knowledge_preview",
)
async def knowledge_preview(request: Request, doc_key: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    document = knowledge_document_or_error(doc_key)
    return templates.TemplateResponse(
        request=request,
        name="knowledge_preview.html",
        context=template_context(
            request,
            page_title=f"Preview {document['display_name']}",
            document=document,
        ),
    )


@app.get("/knowledge/{doc_key}/edit", response_class=HTMLResponse, name="knowledge_edit_legacy")
async def knowledge_edit_legacy(request: Request, doc_key: str) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("knowledge_edit", doc_key=doc_key),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/settings/knowledge/{doc_key}/edit",
    response_class=HTMLResponse,
    name="knowledge_edit",
)
async def knowledge_edit(request: Request, doc_key: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    document = knowledge_document_or_error(doc_key)
    if not document["editable"]:
        raise HTTPException(status_code=403, detail="This document is read-only.")
    return templates.TemplateResponse(
        request=request,
        name="knowledge_edit.html",
        context=template_context(
            request,
            page_title=f"Edit {document['display_name']}",
            document=document,
            error=request.session.pop("knowledge_error", None),
        ),
    )


@app.post("/knowledge/{doc_key}/edit", name="knowledge_update")
async def knowledge_update(request: Request, doc_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    document = knowledge_document_or_error(doc_key)
    if not document["editable"]:
        raise HTTPException(status_code=403, detail="This document is read-only.")
    form = await request.form()
    try:
        backup_path = save_document(
            doc_key,
            str(form.get("content", "")),
            str(request.session.get("dashboard_user", "dashboard")),
        )
    except ValueError as exc:
        request.session["knowledge_error"] = str(exc)
        return RedirectResponse(
            url=request.url_for("knowledge_edit", doc_key=doc_key),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    backup_message = (
        f" Backup created as {backup_path.name}." if backup_path else ""
    )
    request.session["knowledge_message"] = (
        f"{document['display_name']} saved.{backup_message} "
        "Queue a reindex if this document is used by the bot."
    )
    return knowledge_redirect(request, doc_key)


@app.post("/knowledge/{doc_key}/reindex", name="knowledge_reindex")
async def knowledge_reindex(request: Request, doc_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    document = knowledge_document_or_error(doc_key)
    try:
        action_id = queue_knowledge_reindex(
            doc_key,
            str(request.session.get("dashboard_user", "dashboard")),
        )
    except ValueError as exc:
        request.session["knowledge_error"] = str(exc)
    else:
        request.session["knowledge_message"] = (
            f"Reindex queued as dashboard action #{action_id} for "
            f"{document['display_name']}."
        )
    return knowledge_redirect(request, doc_key)


@app.post("/knowledge/reindex-all", name="knowledge_reindex_all")
async def knowledge_reindex_all(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    action_id = queue_knowledge_reindex(
        None,
        str(request.session.get("dashboard_user", "dashboard")),
    )
    request.session["knowledge_message"] = (
        f"Full knowledge reindex queued as dashboard action #{action_id}."
    )
    return knowledge_redirect(request)


@app.get("/stats", response_class=HTMLResponse, name="stats_legacy")
async def stats_legacy(request: Request) -> RedirectResponse:
    return stats_redirect(request)


@app.get("/analytics/stats", response_class=HTMLResponse, name="stats_page")
async def stats_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context=template_context(
            request,
            page_title="Stats Graphics",
            range_key="30d",
            limit=25,
            stats=list_stats(),
            message=request.session.pop("stats_message", None),
            error=request.session.pop("stats_error", None),
        ),
    )


@app.get("/stats/{stat_id}", response_class=HTMLResponse, name="stats_detail_legacy")
async def stats_detail_legacy(request: Request, stat_id: str) -> RedirectResponse:
    return stats_redirect(request, stat_id)


@app.get("/analytics/stats/{stat_id}", response_class=HTMLResponse, name="stats_detail")
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


@app.get("/stats/{stat_id}/edit", response_class=HTMLResponse, name="stats_edit_legacy")
async def stats_edit_legacy(request: Request, stat_id: str) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("stats_edit", stat_id=stat_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/analytics/stats/{stat_id}/edit", response_class=HTMLResponse, name="stats_edit")
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


@app.get("/imports", response_class=HTMLResponse, name="imports_legacy")
async def imports_legacy(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("imports"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/settings/imports", response_class=HTMLResponse, name="imports")
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


@app.get("/api/discord/roles", response_class=JSONResponse)
async def api_discord_roles(request: Request) -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    return JSONResponse(roles_metadata())


@app.get("/api/discord/channels", response_class=JSONResponse)
async def api_discord_channels(request: Request) -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    return JSONResponse(channels_metadata())


@app.get("/api/discord/categories", response_class=JSONResponse)
async def api_discord_categories(request: Request) -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    return JSONResponse(categories_metadata())


@app.get("/api/discord/guild-structure", response_class=JSONResponse)
async def api_discord_guild_structure(request: Request) -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    return JSONResponse(guild_structure())


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
