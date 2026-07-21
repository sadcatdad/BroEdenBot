from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from dashboard.auth import (
    OAUTH_STATE_KEY,
    csrf_is_valid,
    csrf_token,
    current_user,
    has_write_access,
    has_permission,
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
    ai_dashboard_visible,
    ai_usage_overview,
    bank_overview,
    database_status,
    delete_failed_vcxp_pulses,
    find_bank_database_path,
    find_database_path,
    import_history,
    message_context_overview,
    vcxp_overview,
)
from dashboard.discord_metadata import (
    categories_metadata,
    channels_metadata,
    emojis_metadata,
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
from dashboard.reminders_manager import (
    list_reminders as list_dashboard_reminders,
    queue_reminder_action,
    reminder_detail as dashboard_reminder_detail,
    reminder_overview as dashboard_reminder_overview,
)
from dashboard.streaks_manager import (
    adjust_streak_day,
    initialize_streak_dashboard_schema,
    queue_streak_restore,
    streaks_overview,
)
from dashboard.users import (
    authenticate_password,
    initialize_dashboard_users,
    list_dashboard_users,
    upsert_discord_user,
)
from dashboard.rbac import (
    access_overview,
    assign_direct_role,
    initialize_rbac_schema,
    list_audit_events,
    list_roles,
    permission_catalog,
    record_audit,
    remove_direct_role,
    remove_discord_role_mapping,
    replace_discord_role_mappings,
    save_custom_role,
    set_user_permission_override,
    set_user_status,
)
from dashboard.features import (
    FEATURES_BY_KEY,
    feature_inventory,
    feature_key_for_setting,
    feature_snapshot,
)
from utils.knowledge_manager import (
    document_details,
    initialize_knowledge_schema,
    list_documents,
    queue_knowledge_reindex,
    recent_knowledge_audit,
    save_document,
)
from utils.ai_kb import (
    MAX_SOURCE_CHARS,
    SOURCE_TYPES,
    VISIBILITIES,
    delete_kb_source,
    get_kb_source,
    get_kb_status,
    initialize_ai_kb_schema,
    list_kb_sources,
    search_kb,
    set_kb_source_ai_enabled,
    upsert_kb_source,
)
from utils.live_knowledge import (
    KNOWLEDGE_SOURCE_TYPES,
    KNOWLEDGE_SYNC_MODES,
    KNOWLEDGE_VISIBILITIES,
    delete_live_knowledge_source_sync,
    initialize_live_knowledge_schema_sync,
    list_live_knowledge_sources_sync,
    queue_live_knowledge_sync,
    upsert_live_knowledge_source_sync,
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
    set_settings,
    settings_database_path,
    settings_for_dashboard,
    settings_for_feature,
)
from utils.display_names import normalize_display_name
from utils.embed_templates import (
    default_embed_payload,
    default_message_payload,
    delete_embed_template,
    get_embed_template,
    initialize_embed_templates_schema,
    list_embed_templates,
    save_embed_template,
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
from utils.visual_studio import initialize_visual_studio_schema
from utils.visual_studio.preview import render_preview, validate_preview
from utils.visual_studio.registry import REGISTRY, asset_type_guidance
from utils.visual_studio.repository import (
    archive_theme,
    delete_schedule,
    delete_theme,
    discard_template_draft,
    duplicate_theme,
    export_configuration,
    get_global_settings,
    get_theme,
    get_visual_template,
    import_configuration_as_drafts,
    list_global_schedules,
    list_recent_audit,
    list_themes,
    list_visual_templates,
    publish_template,
    publish_global_settings,
    reset_template_to_defaults,
    restore_template_version,
    restore_theme,
    save_global_settings_draft,
    save_schedule,
    save_template_draft,
    save_theme,
    save_variant,
    set_schedule_enabled,
    set_default_theme,
)
from utils.visual_studio.storage import (
    ASSET_TYPES,
    archive_asset,
    asset_bytes,
    asset_path,
    delete_asset,
    get_asset,
    inspect_upload,
    list_assets,
    rename_asset,
    save_asset,
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
    initialize_ai_kb_schema()
    initialize_live_knowledge_schema_sync()
    initialize_dashboard_users()
    initialize_rbac_schema()
    initialize_streak_dashboard_schema()
    initialize_embed_templates_schema()
    initialize_visual_studio_schema()

app = FastAPI(
    title="BroEdenBot Local Dashboard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def required_permission(path: str, method: str) -> str | None:
    """Resolve the stable server-side capability for a dashboard request."""
    normalized_method = method.upper()
    if path in {"/health", "/login", "/logout"} or path.startswith("/auth/discord/") or path.startswith("/static/"):
        return None
    if path in {"/users", "/settings/users"} or path.startswith("/settings/access"):
        return "access.manage"
    if path.startswith("/settings/audit"):
        return "audit_log.view"
    if path.startswith("/settings/discord") or path.startswith("/api/discord/"):
        return "discord_metadata.refresh" if normalized_method != "GET" else "discord_metadata.view"
    if path in {"/imports", "/settings/imports"}:
        return "imports.manage" if normalized_method != "GET" else "imports.view"
    if path.startswith("/settings"):
        return "settings.manage" if normalized_method != "GET" else "settings.view"
    if path.startswith("/features"):
        if normalized_method != "GET":
            return "features.manage"
        parts = [part for part in path.split("/") if part]
        feature = FEATURES_BY_KEY.get(parts[1]) if len(parts) > 1 else None
        return feature.permission if feature else "features.view"
    if path.startswith("/analytics") or path.startswith("/stats"):
        return "analytics.manage" if normalized_method != "GET" else "analytics.view"
    if path.startswith("/operations/reminders"):
        return "reminders.manage" if normalized_method != "GET" else "reminders.view"
    if path.startswith("/operations/restart"):
        return "bot.restart"
    if path.startswith("/operations"):
        return "operations.manage" if normalized_method != "GET" else "operations.view"
    if path.startswith("/streaks"):
        return "streaks.manage" if normalized_method != "GET" else "streaks.view"
    if path.startswith("/vcxp/"):
        return "operations.manage"
    if path.startswith("/bank"):
        return "bank.manage" if normalized_method != "GET" else "bank.view"
    if path.startswith("/embeds"):
        return "message_studio.manage" if normalized_method != "GET" else "message_studio.view"
    if path.startswith("/visual") or path.startswith("/api/visual"):
        return "visual.manage" if normalized_method != "GET" else "visual.view"
    if path.startswith("/knowledge"):
        return "knowledge.manage" if normalized_method != "GET" or path.endswith("/edit") else "knowledge.view"
    if path.startswith("/ai/kb/new") or (path.startswith("/ai/kb/") and path.endswith("/edit")):
        return "ai.manage"
    if path.startswith("/ai"):
        return "ai.manage" if normalized_method != "GET" else "ai.view"
    return "dashboard.view" if normalized_method == "GET" else "dashboard.unmapped"


class DashboardPermissionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        permission = required_permission(request.url.path, request.method)
        if permission:
            if not dashboard_enabled() or not is_authenticated(request):
                return RedirectResponse(
                    url=request.url_for("login_page"),
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            if not has_permission(request, permission):
                user = current_user(request)
                record_audit(
                    actor_user_id=user.get("id"),
                    actor_label=user.get("username") or "anonymous",
                    action="access.denied",
                    target_type="route",
                    target_id=request.url.path,
                    success=False,
                    error=f"Missing permission: {permission}",
                )
                if request.url.path.startswith("/api/"):
                    return JSONResponse(
                        {"detail": "You do not have permission to access this resource."},
                        status_code=status.HTTP_403_FORBIDDEN,
                    )
                return templates.TemplateResponse(
                    request=request,
                    name="403.html",
                    context=template_context(
                        request,
                        page_title="Access denied",
                        required_permission=permission,
                    ),
                    status_code=status.HTTP_403_FORBIDDEN,
                )
        response = await call_next(request)
        if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path not in {"/login", "/logout"}:
            user = current_user(request) if is_authenticated(request) else {}
            record_audit(
                actor_user_id=user.get("id"),
                actor_label=user.get("username") or "anonymous",
                action="request." + request.method.lower(),
                target_type="route",
                target_id=request.url.path,
                success=response.status_code < 400,
                error=None if response.status_code < 400 else f"HTTP {response.status_code}",
            )
        return response


app.add_middleware(DashboardPermissionMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("DASHBOARD_SECRET_KEY", "").strip()
    or "dashboard-disabled",
    session_cookie="broeden_dashboard_session",
    max_age=60 * 60 * 12,
    same_site="lax",
    # Set DASHBOARD_COOKIE_SECURE=true when the dashboard is served over HTTPS
    # (e.g. behind a TLS reverse proxy) so the session cookie is only sent over
    # secure connections. Defaults to False for plain-HTTP LAN access on the Pi.
    https_only=env_flag("DASHBOARD_COOKIE_SECURE", default=False),
)
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR / "static"), name="static")
templates = Jinja2Templates(directory=DASHBOARD_DIR / "templates")
templates.env.filters["display_name"] = normalize_display_name


def friendly_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Never"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone()
    return "{} {}, {} at {}".format(
        local.strftime("%B"), local.day, local.year,
        local.strftime("%I:%M %p %Z").lstrip("0"),
    )


templates.env.filters["friendly_datetime"] = friendly_datetime


def template_context(request: Request, **values: Any) -> dict[str, Any]:
    permissions = sorted(
        permission for permission in (
            str(item["key"]) for item in permission_catalog()
        ) if has_permission(request, permission)
    ) if is_authenticated(request) else []
    return {
        "request": request,
        "current_path": request.url.path,
        "authenticated": is_authenticated(request),
        "current_user": current_user(request) if is_authenticated(request) else None,
        "can_write": has_write_access(request),
        "can_manage_users": is_admin(request),
        "permissions": permissions,
        "can": lambda permission: permission in permissions,
        "ai_dashboard_visible": ai_dashboard_visible(),
        "csrf_token": csrf_token(request),
        **values,
    }


def settings_redirect_for_key(request: Request, key: str) -> str:
    feature_key = feature_key_for_setting(key)
    if feature_key in FEATURES_BY_KEY:
        return str(request.url_for("feature_detail", feature_key=feature_key))
    definition = DEFINITIONS_BY_KEY.get(key)
    if definition and definition.section in {
        "bumps",
        "reminders",
        "streaks",
        "stats_features",
    }:
        return str(request.url_for("settings_features"))
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
            record_audit(
                actor_user_id=int(user["id"]),
                actor_label=str(user.get("username") or "owner"),
                action="auth.login.succeeded",
                target_type="auth_provider",
                target_id="password",
            )
            return RedirectResponse(
                url=request.url_for("home"),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        error = "Invalid username or password."
        record_audit(
            actor_label=str(username or "anonymous")[:120],
            action="auth.login.failed",
            target_type="auth_provider",
            target_id="password",
            success=False,
            error="Invalid credentials",
        )
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
        record_audit(
            actor_label="anonymous", action="auth.login.failed",
            target_type="auth_provider", target_id="discord",
            success=False, error="OAuth canceled",
        )
        return login_response(
            request,
            error="Discord login was canceled.",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    if not expected_state or not state:
        record_audit(
            actor_label="anonymous", action="auth.login.failed",
            target_type="auth_provider", target_id="discord",
            success=False, error="OAuth state missing or expired",
        )
        return login_response(
            request,
            error="Discord login session is missing or expired.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not secrets.compare_digest(expected_state, state):
        record_audit(
            actor_label="anonymous", action="auth.login.failed",
            target_type="auth_provider", target_id="discord",
            success=False, error="OAuth state mismatch",
        )
        return login_response(
            request,
            error="Discord login session could not be verified.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not code:
        record_audit(
            actor_label="anonymous", action="auth.login.failed",
            target_type="auth_provider", target_id="discord",
            success=False, error="Authorization code missing",
        )
        return login_response(
            request,
            error="Discord did not return an authorization code.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        identity = await fetch_discord_identity(code)
        user = upsert_discord_user(identity)
    except DiscordOAuthError:
        record_audit(
            actor_label="anonymous", action="auth.login.failed",
            target_type="auth_provider", target_id="discord",
            success=False, error="Discord exchange or membership lookup failed",
        )
        return login_response(
            request,
            error="Discord login could not be completed. Please try again.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    except PermissionError as exc:
        record_audit(
            actor_label=str(identity.get("username") or "discord")[:120],
            action="auth.login.failed", target_type="auth_provider",
            target_id="discord", success=False, error=str(exc)[:300],
        )
        return login_response(
            request,
            error=str(exc),
            status_code=status.HTTP_403_FORBIDDEN,
        )
    except ValueError:
        record_audit(
            actor_label="anonymous", action="auth.login.failed",
            target_type="auth_provider", target_id="discord",
            success=False, error="Discord identity validation failed",
        )
        return login_response(
            request,
            error="Discord identity could not be verified.",
            status_code=status.HTTP_502_BAD_GATEWAY,
        )
    login_user(request, user, auth_provider="discord")
    record_audit(
        actor_user_id=int(user["id"]),
        actor_label=str(
            user.get("discord_global_name")
            or user.get("discord_username")
            or user.get("username")
            or "discord"
        ),
        action="auth.login.succeeded",
        target_type="auth_provider",
        target_id="discord",
        after={"access_source": user.get("access_source")},
    )
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
    return RedirectResponse(
        url=request.url_for("settings_access"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/", response_class=HTMLResponse, name="home")
async def home(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    database = database_status(find_database_path())
    bank_database = database_status(find_bank_database_path())
    ai_usage = ai_usage_overview(limit=5) if ai_dashboard_visible() else None
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
            vcxp_message=request.session.pop("vcxp_message", None),
            vcxp_error=request.session.pop("vcxp_error", None),
            ai_usage=ai_usage,
            discord_token_status=safe_setting("DISCORD_TOKEN"),
            gemini_key_status=safe_setting("GEMINI_API_KEY"),
            gemini_models_configured=all(os.getenv(name, "").strip() for name in model_names),
            current_time=datetime.now().astimezone(),
        ),
    )


@app.post("/vcxp/failed/clear", name="clear_failed_vcxp_pulses")
async def clear_failed_vcxp_pulses(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "operations.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    try:
        deleted = delete_failed_vcxp_pulses()
    except (OSError, sqlite3.Error) as exc:
        request.session["vcxp_error"] = (
            f"Failed VC XP pulses could not be cleared: {type(exc).__name__}."
        )
    else:
        request.session["vcxp_message"] = (
            f"Cleared {deleted:,} failed VC XP pulse "
            f"record{'s' if deleted != 1 else ''}. Reward accounting was unchanged."
        )
    return RedirectResponse(
        url=request.url_for("home"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def streaks_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("streaks_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/streaks", response_class=HTMLResponse, name="streaks_page")
async def streaks_page(
    request: Request,
    q: str = "",
    guild_id: str = "",
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    try:
        overview = streaks_overview(query=q, guild_id=guild_id)
    except (OSError, sqlite3.Error) as exc:
        overview = {
            "guild_id": guild_id or safe_setting("GUILD_ID"),
            "today": datetime.now().date().isoformat(),
            "members": [],
            "summary": {
                "members": 0,
                "active": 0,
                "tracked_days": 0,
                "best_longest": 0,
            },
            "runtime": None,
            "restores": [],
            "adjustments": [],
            "query": q,
        }
        request.session["streaks_error"] = (
            f"Streak data could not be loaded: {type(exc).__name__}."
        )
    return templates.TemplateResponse(
        request=request,
        name="streaks.html",
        context=template_context(
            request,
            page_title="Streaks",
            streaks=overview,
            message=request.session.pop("streaks_message", None),
            error=request.session.pop("streaks_error", None),
        ),
    )


@app.post("/streaks/restore", name="streaks_restore")
async def streaks_restore(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    form = await request.form()
    try:
        request_id, created = queue_streak_restore(
            guild_id=form.get("guild_id", ""),
            start_date=form.get("start_date", ""),
            end_date=form.get("end_date", ""),
            requested_by=dashboard_user_label(request),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        request.session["streaks_error"] = str(exc)
    else:
        if created:
            request.session["streaks_message"] = (
                f"Restore request #{request_id} queued. The bot will scan "
                "Discord history when it is online."
            )
        else:
            request.session["streaks_message"] = (
                f"Restore request #{request_id} already covers that range."
            )
    return streaks_redirect(request)


@app.post("/streaks/adjust", name="streaks_adjust")
async def streaks_adjust(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    form = await request.form()
    try:
        result = adjust_streak_day(
            guild_id=form.get("guild_id", ""),
            user_id=form.get("user_id", ""),
            activity_date=form.get("activity_date", ""),
            action=str(form.get("action", "")),
            reason=str(form.get("reason", "")),
            changed_by=dashboard_user_label(request),
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        request.session["streaks_error"] = str(exc)
    else:
        request.session["streaks_message"] = (
            f"Streak adjustment #{result['adjustment_id']} saved. "
            f"Current: {result['current']} days; longest: {result['longest']} days."
        )
    return streaks_redirect(request)


@app.get("/ai", response_class=HTMLResponse, name="ai_page")
async def ai_page(
    request: Request,
    command: Optional[str] = None,
    model: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    if not ai_dashboard_visible():
        raise HTTPException(status_code=404, detail="AI dashboard is hidden.")
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context=template_context(
            request,
            page_title="AI Framework",
            ai=ai_usage_overview(
                command=(command or "").strip(),
                model=(model or "").strip(),
                status_filter=(status_filter or "").strip().casefold(),
            ),
        ),
    )


@app.get("/ai/kb", response_class=HTMLResponse, name="ai_kb_page")
async def ai_kb_page(
    request: Request,
    query: Optional[str] = None,
    visibility: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 5,
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    if not has_permission(request, "ai.view"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    source_types = [source_type] if source_type else None
    search_results = (
        search_kb(
            query=query,
            visibility=visibility or "all",
            source_types=source_types,
            limit=limit,
        )
        if query
        else []
    )
    return templates.TemplateResponse(
        request=request,
        name="ai_kb_info.html",
        context=template_context(
            request,
            page_title="AI Knowledge Sources",
            kb_status=get_kb_status(),
            knowledge=knowledge_sources_summary(),
            sources=list_kb_sources(),
            source_types=sorted(SOURCE_TYPES),
            visibilities=sorted(VISIBILITIES),
            search_results=search_results,
            filters={
                "query": query or "",
                "visibility": visibility or "all",
                "source_type": source_type or "",
                "limit": max(1, min(limit, 25)),
            },
            message=request.session.pop("ai_kb_message", None),
            error=request.session.pop("ai_kb_error", None),
        ),
    )


@app.get("/ai/kb/new", response_class=HTMLResponse, name="ai_kb_new")
async def ai_kb_new(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    if not has_permission(request, "ai.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    return templates.TemplateResponse(
        request=request,
        name="ai_kb_edit.html",
        context=template_context(
            request,
            page_title="New AI KB Source",
            source=None,
            source_types=sorted(SOURCE_TYPES),
            visibilities=sorted(VISIBILITIES),
            max_source_chars=MAX_SOURCE_CHARS,
            error=request.session.pop("ai_kb_error", None),
        ),
    )


@app.get("/ai/kb/{source_name}/edit", response_class=HTMLResponse, name="ai_kb_edit")
async def ai_kb_edit(request: Request, source_name: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    if not has_permission(request, "ai.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    source = get_kb_source(source_name)
    if source is None:
        raise HTTPException(status_code=404, detail="KB source was not found.")
    return templates.TemplateResponse(
        request=request,
        name="ai_kb_edit.html",
        context=template_context(
            request,
            page_title=f"Edit {source_name}",
            source=source,
            source_types=sorted(SOURCE_TYPES),
            visibilities=sorted(VISIBILITIES),
            max_source_chars=MAX_SOURCE_CHARS,
            error=request.session.pop("ai_kb_error", None),
        ),
    )


@app.post("/ai/kb/save", name="ai_kb_save")
async def ai_kb_save(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "ai.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    form = await request.form()
    source_name = str(form.get("source_name", "")).strip()
    try:
        result = upsert_kb_source(
            source_name=source_name,
            source_type=str(form.get("source_type", "")),
            visibility=str(form.get("source_visibility", "")),
            raw_text=str(form.get("raw_content", "")),
            ai_enabled=str(form.get("ai_enabled", "")).strip() == "1",
            metadata={
                "source": "dashboard",
                "changed_by": dashboard_user_label(request),
            },
        )
    except ValueError as exc:
        request.session["ai_kb_error"] = str(exc)
        target = (
            request.url_for("ai_kb_edit", source_name=source_name)
            if source_name and get_kb_source(source_name)
            else request.url_for("ai_kb_new")
        )
        return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
    request.session["ai_kb_message"] = (
        f"{result['source_name']} saved with {result['chunk_count']} chunk(s)."
    )
    return RedirectResponse(
        url=request.url_for("knowledge_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/ai/kb/{source_name}/delete", name="ai_kb_delete")
async def ai_kb_delete(request: Request, source_name: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "ai.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    form = await request.form()
    if str(form.get("confirm", "")).strip() != source_name:
        request.session["ai_kb_error"] = "Type the source name to confirm deletion."
        return RedirectResponse(
            url=request.url_for("ai_kb_page"),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        deleted = delete_kb_source(source_name)
    except ValueError as exc:
        request.session["ai_kb_error"] = str(exc)
    else:
        request.session["ai_kb_message"] = (
            f"{source_name} deleted with {deleted} chunk(s)."
        )
    return RedirectResponse(
        url=request.url_for("knowledge_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/knowledge/ai/{source_name}/toggle", name="knowledge_ai_toggle")
async def knowledge_ai_toggle(request: Request, source_name: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "knowledge.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    form = await request.form()
    enabled = str(form.get("ai_enabled", "")).strip() == "1"
    try:
        set_kb_source_ai_enabled(source_name, enabled)
    except ValueError as exc:
        request.session["knowledge_error"] = str(exc)
    else:
        request.session["knowledge_message"] = (
            f"{source_name} is {'connected to' if enabled else 'excluded from'} AI retrieval."
        )
    return knowledge_redirect(request)


@app.post("/knowledge/live/save", name="knowledge_live_save")
async def knowledge_live_save(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "knowledge.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    form = await request.form()
    channel_id = str(form.get("channel_id", "")).strip()
    if not channel_id or channel_id == "[]":
        channel_id = str(form.get("manual_channel_id", "")).strip()
    channel_id = channel_id.strip("[]\" ")
    guild_id = str(form.get("guild_id", "")).strip() or os.getenv("GUILD_ID", "").strip()
    channel_name = str(form.get("channel_name", "")).strip() or f"Discord source {channel_id}"
    try:
        if not channel_id.isdigit() or not guild_id.isdigit():
            raise ValueError("Guild ID and source channel/thread ID are required.")
        upsert_live_knowledge_source_sync(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            channel_name=channel_name,
            source_type=str(form.get("source_type", "")),
            visibility=str(form.get("visibility", "")),
            sync_mode=str(form.get("sync_mode", "")),
            enabled=str(form.get("enabled", "")).strip() == "1",
            ai_enabled=str(form.get("ai_enabled", "")).strip() == "1",
        )
    except ValueError as exc:
        request.session["knowledge_error"] = str(exc)
    else:
        request.session["knowledge_message"] = f"{channel_name} saved as a live knowledge source."
    return knowledge_redirect(request)


@app.post("/knowledge/live/{guild_id}/{channel_id}/remove", name="knowledge_live_remove")
async def knowledge_live_remove(
    request: Request,
    guild_id: int,
    channel_id: int,
) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "knowledge.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    deleted = delete_live_knowledge_source_sync(
        guild_id=guild_id,
        channel_id=channel_id,
    )
    request.session["knowledge_message"] = (
        f"Live source removed with {deleted} indexed entr{'y' if deleted == 1 else 'ies'}."
    )
    return knowledge_redirect(request)


@app.post("/knowledge/live/{guild_id}/{channel_id}/sync", name="knowledge_live_sync")
async def knowledge_live_sync(
    request: Request,
    guild_id: int,
    channel_id: int,
) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "knowledge.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    form = await request.form()
    limit = int(str(form.get("limit", "200")).strip() or "200")
    action_id = queue_live_knowledge_sync(
        guild_id=guild_id,
        channel_id=channel_id,
        limit=limit,
        requested_by=dashboard_user_label(request),
    )
    request.session["knowledge_message"] = (
        f"Live knowledge sync queued as dashboard action #{action_id}."
    )
    return knowledge_redirect(request)


def render_embed_editor(
    request: Request,
    *,
    template: Optional[dict[str, Any]],
    asset_type: str = "embed",
    error: Optional[str] = None,
) -> HTMLResponse:
    clean_type = str((template or {}).get("asset_type") or asset_type).casefold()
    if clean_type not in {"embed", "message"}:
        clean_type = "embed"
    default_payload = (
        default_message_payload()
        if clean_type == "message"
        else default_embed_payload()
    )
    return templates.TemplateResponse(
        request=request,
        name="embed_edit.html",
        context=template_context(
            request,
            page_title=(
                f"Edit {template['name']}"
                if template and template.get("id")
                else f"Create {clean_type.title()}"
            ),
            embed_template=template,
            embed_payload=(template or {}).get("payload", default_payload),
            asset_type=clean_type,
            discord_metadata=picker_metadata(),
            error=error or request.session.pop("embed_error", None),
            message=request.session.pop("embed_message", None),
        ),
    )


@app.get("/embeds", response_class=HTMLResponse, name="embed_templates")
async def embed_templates_page(
    request: Request,
    q: str = "",
    sort: str = "updated",
    order: str = "desc",
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    clean_sort = sort if sort in {"name", "type", "updated", "features"} else "updated"
    clean_order = "asc" if order.casefold() == "asc" else "desc"
    return templates.TemplateResponse(
        request=request,
        name="embeds.html",
        context=template_context(
            request,
            page_title="Embed/Message Editor",
            embeds=list_embed_templates(q, clean_sort, clean_order),
            query=q,
            sort=clean_sort,
            order=clean_order,
            message=request.session.pop("embed_message", None),
            error=request.session.pop("embed_error", None),
        ),
    )


@app.get("/embeds/new", response_class=HTMLResponse, name="embed_new")
async def embed_new(request: Request, asset_type: str = "embed") -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    clean_type = asset_type.casefold()
    if clean_type not in {"embed", "message"}:
        raise HTTPException(status_code=400, detail="Choose Embed or Message.")
    return render_embed_editor(request, template=None, asset_type=clean_type)


@app.get("/embeds/{template_id}/edit", response_class=HTMLResponse, name="embed_edit")
async def embed_edit(request: Request, template_id: int) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    template = get_embed_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Asset was not found.")
    return render_embed_editor(request, template=template)


@app.post("/embeds/save", name="embed_save")
async def embed_save(request: Request) -> Response:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    form = await request.form()
    raw_id = str(form.get("template_id", "")).strip()
    template_id = int(raw_id) if raw_id.isdigit() else None
    name = str(form.get("name", ""))
    asset_type = str(form.get("asset_type", "embed")).strip().casefold()
    payload_json = str(form.get("payload_json", ""))
    try:
        saved_id = save_embed_template(
            name=name,
            payload_json=payload_json,
            updated_by=dashboard_user_label(request),
            template_id=template_id,
            asset_type=asset_type,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        try:
            draft_payload = json.loads(payload_json)
        except json.JSONDecodeError:
            draft_payload = (
                default_message_payload()
                if asset_type == "message"
                else default_embed_payload()
            )
        draft = {
            "id": template_id,
            "name": name,
            "asset_type": asset_type,
            "payload": draft_payload,
            "features": [],
        }
        return render_embed_editor(
            request,
            template=draft,
            asset_type=asset_type,
            error=str(exc),
        )
    request.session["embed_message"] = f"{asset_type.title()} saved."
    return RedirectResponse(
        url=request.url_for("embed_edit", template_id=saved_id),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/embeds/{template_id}/delete", name="embed_delete")
async def embed_delete(request: Request, template_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    try:
        name = delete_embed_template(template_id)
    except (OSError, sqlite3.Error, ValueError) as exc:
        request.session["embed_error"] = str(exc)
    else:
        request.session["embed_message"] = f"{name} deleted."
    return RedirectResponse(
        url=request.url_for("embed_templates"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def visual_redirect(request: Request, route: str, **parameters: Any) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for(route, **parameters),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def visual_actor(request: Request) -> str:
    user = current_user(request)
    return str((user or {}).get("username") or "dashboard")


async def require_visual_admin(request: Request) -> Any:
    await require_action_csrf(request)
    if not has_permission(request, "visual.manage"):
        raise HTTPException(
            status_code=403,
            detail="Visual Content Studio changes require dashboard admin access.",
        )
    return await request.form()


def visual_settings_from_form(form: Any, template_key: str) -> dict[str, Any]:
    definition = REGISTRY.get(template_key)
    raw_json = str(form.get("settings_json", "")).strip()
    if raw_json:
        try:
            settings = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Advanced settings must be valid JSON.") from exc
        if not isinstance(settings, dict):
            raise ValueError("Advanced settings must be a JSON object.")
    else:
        settings = {}
    text_fields = {
        "title",
        "subtitle",
        "footer_text",
        "accent_color",
        "panel_color",
        "text_color",
        "muted_text_color",
        "density",
        "avatar_shape",
        "background_fit",
    }
    numeric_fields = {
        "panel_opacity",
        "focal_x",
        "focal_y",
        "background_brightness",
        "background_blur",
        "background_saturation",
        "background_contrast",
        "background_overlay_opacity",
        "maximum_rows",
        "title_size",
        "subtitle_size",
        "body_size",
        "footer_size",
    }
    for key in definition.supported_settings:
        if key in text_fields and key in form:
            value = str(form.get(key, "")).strip()
            if value:
                settings[key] = value
            else:
                settings.pop(key, None)
        elif key in numeric_fields and str(form.get(key, "")).strip():
            settings[key] = str(form.get(key)).strip()
    for key in ("show_avatars", "show_ranks", "show_timestamp", "high_contrast", "reduced_decoration"):
        if key in definition.supported_settings:
            settings[key] = str(form.get(key, "")).casefold() in {"1", "true", "on", "yes"}
    assets = dict(settings.get("assets") or {})
    for slot in definition.asset_slots:
        field = "asset_{}".format(slot.key)
        if field in form:
            value = str(form.get(field, "")).strip()
            if value.isdigit() and int(value) > 0:
                assets[slot.key] = int(value)
            else:
                assets.pop(slot.key, None)
    settings["assets"] = assets
    return settings


def visual_template_response(
    request: Request,
    template_key: str,
    *,
    error: Optional[str] = None,
) -> HTMLResponse:
    visual_template = get_visual_template(template_key)
    return templates.TemplateResponse(
        request=request,
        name="visual_template_edit.html",
        context=template_context(
            request,
            page_title="Customize {}".format(visual_template["display_name"]),
            visual_template=visual_template,
            themes=list_themes(),
            assets=list_assets(limit=200),
            message=request.session.pop("visual_message", None),
            error=error or request.session.pop("visual_error", None),
        ),
    )


@app.get("/visual", response_class=HTMLResponse, name="visual_templates")
@app.get("/visual/templates", response_class=HTMLResponse)
async def visual_templates_page(request: Request, category: str = "") -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    registered = list_visual_templates()
    categories = sorted({item["category"] for item in registered})
    if category:
        registered = [item for item in registered if item["category"] == category]
    return templates.TemplateResponse(
        request=request,
        name="visual_templates.html",
        context=template_context(
            request,
            page_title="Visual Content Studio",
            visual_templates=registered,
            categories=categories,
            selected_category=category,
            message=request.session.pop("visual_message", None),
            error=request.session.pop("visual_error", None),
        ),
    )


@app.get("/visual/templates/{template_key}", response_class=HTMLResponse, name="visual_template_edit")
async def visual_template_edit(request: Request, template_key: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    try:
        return visual_template_response(request, template_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="Visual template was not found.")


@app.get("/visual/templates/{template_key}/preview", name="visual_template_preview")
async def visual_template_preview(
    request: Request,
    template_key: str,
    draft: bool = False,
    edge_case: str = "maximum",
    safe_area: bool = False,
) -> Response:
    if redirect := login_redirect(request):
        return redirect
    if edge_case not in {"maximum", "minimum", "empty"}:
        raise HTTPException(status_code=400, detail="Unknown preview data set.")
    try:
        png = await __import__("asyncio").to_thread(
            render_preview,
            template_key,
            draft=draft,
            edge_case=edge_case,
            safe_area=safe_area,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return Response(
        png,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'inline; filename="{}-preview.png"'.format(template_key),
        },
    )


@app.post("/visual/templates/{template_key}/draft", name="visual_template_save_draft")
async def visual_template_save_draft(request: Request, template_key: str) -> Response:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        settings = visual_settings_from_form(form, template_key)
        raw_theme = str(form.get("theme_id", "")).strip()
        theme_id = int(raw_theme) if raw_theme.isdigit() and int(raw_theme) > 0 else None
        save_template_draft(
            template_key,
            settings,
            theme_id=theme_id,
            actor=visual_actor(request),
        )
    except (KeyError, sqlite3.Error, ValueError) as exc:
        return visual_template_response(request, template_key, error=str(exc))
    request.session["visual_message"] = "Draft saved. Live bot output is unchanged."
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/templates/{template_key}/publish", name="visual_template_publish")
async def visual_template_publish(request: Request, template_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        await __import__("asyncio").to_thread(
            validate_preview,
            template_key,
            draft=True,
        )
        version = publish_template(
            template_key,
            actor=visual_actor(request),
            change_summary=str(form.get("change_summary", "Published from dashboard")),
        )
    except (KeyError, sqlite3.Error, ValueError, OSError) as exc:
        request.session["visual_error"] = "Published configuration failed validation: {}".format(exc)
        audit_user = current_user(request)
        record_audit(
            actor_user_id=audit_user.get("id"), actor_label=audit_user.get("username") or "dashboard",
            action="content.publish", target_type="visual_template", target_id=template_key,
            success=False, error=type(exc).__name__,
        )
    else:
        request.session["visual_message"] = "Published version {}. New bot renders now use it.".format(version)
        audit_user = current_user(request)
        record_audit(
            actor_user_id=audit_user.get("id"), actor_label=audit_user.get("username") or "dashboard",
            action="content.publish", target_type="visual_template", target_id=template_key,
            after={"version": version},
        )
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/templates/{template_key}/discard", name="visual_template_discard")
async def visual_template_discard(request: Request, template_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    discard_template_draft(template_key, visual_actor(request))
    request.session["visual_message"] = "Draft discarded."
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/templates/{template_key}/reset", name="visual_template_reset")
async def visual_template_reset(request: Request, template_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    reset_template_to_defaults(template_key, visual_actor(request))
    request.session["visual_message"] = "Built-in defaults were loaded into a draft. Publish to make them live."
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/templates/{template_key}/restore/{version}", name="visual_template_restore")
async def visual_template_restore(request: Request, template_key: str, version: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        restore_template_version(template_key, version, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Version {} restored as a draft. Review and publish it when ready.".format(version)
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/templates/{template_key}/variants", name="visual_variant_save")
async def visual_variant_save(request: Request, template_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        raw_settings = json.loads(str(form.get("variant_settings_json", "{}")) or "{}")
        raw_theme = str(form.get("variant_theme_id", "")).strip()
        raw_width = str(form.get("variant_width", "")).strip()
        raw_height = str(form.get("variant_height", "")).strip()
        variant_id = save_variant(
            template_key,
            name=str(form.get("variant_name", "")),
            description=str(form.get("variant_description", "")),
            settings=raw_settings,
            width=int(raw_width) if raw_width.isdigit() else None,
            height=int(raw_height) if raw_height.isdigit() else None,
            theme_id=int(raw_theme) if raw_theme.isdigit() else None,
            actor=visual_actor(request),
        )
    except (json.JSONDecodeError, ValueError, sqlite3.Error) as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Variant #{} saved. Commands continue using their default variant.".format(variant_id)
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/templates/{template_key}/schedules", name="visual_schedule_save")
async def visual_schedule_save(request: Request, template_key: str) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        schedule_id = save_schedule(
            template_key=template_key,
            theme_id=int(str(form.get("schedule_theme_id", "0"))),
            variant_id=(int(str(form.get("schedule_variant_id"))) if str(form.get("schedule_variant_id", "")).isdigit() else None),
            starts_at=str(form.get("starts_at", "")),
            ends_at=str(form.get("ends_at", "")),
            timezone_name=str(form.get("timezone", "America/Chicago")),
            priority=int(str(form.get("priority", "0")) or "0"),
            actor=visual_actor(request),
        )
    except (ValueError, sqlite3.Error) as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Seasonal schedule #{} created.".format(schedule_id)
    return visual_redirect(request, "visual_template_edit", template_key=template_key)


@app.post("/visual/schedules", name="visual_global_schedule_save")
async def visual_global_schedule_save(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        schedule_id = save_schedule(
            template_key=None,
            theme_id=int(str(form.get("schedule_theme_id", "0"))),
            variant_id=None,
            starts_at=str(form.get("starts_at", "")),
            ends_at=str(form.get("ends_at", "")),
            timezone_name=str(form.get("timezone", "America/Chicago")),
            priority=int(str(form.get("priority", "0")) or "0"),
            actor=visual_actor(request),
        )
    except (ValueError, sqlite3.Error) as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Global seasonal schedule #{} created.".format(schedule_id)
    return visual_redirect(request, "visual_globals")


@app.post("/visual/schedules/{schedule_id}/toggle", name="visual_schedule_toggle")
async def visual_schedule_toggle(request: Request, schedule_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    enabled = str(form.get("enabled", "")).casefold() in {"1", "true", "on", "yes"}
    try:
        template_key = set_schedule_enabled(
            schedule_id,
            enabled,
            visual_actor(request),
        )
    except (sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
        template_key = str(form.get("template_key", "")).strip() or None
    else:
        request.session["visual_message"] = "Schedule {}.".format(
            "enabled" if enabled else "disabled"
        )
    return (
        visual_redirect(request, "visual_template_edit", template_key=template_key)
        if template_key
        else visual_redirect(request, "visual_globals")
    )


@app.post("/visual/schedules/{schedule_id}/delete", name="visual_schedule_delete")
async def visual_schedule_delete(request: Request, schedule_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        template_key = delete_schedule(schedule_id, visual_actor(request))
    except (sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
        template_key = str(form.get("template_key", "")).strip() or None
    else:
        request.session["visual_message"] = "Schedule deleted."
    return (
        visual_redirect(request, "visual_template_edit", template_key=template_key)
        if template_key
        else visual_redirect(request, "visual_globals")
    )


@app.get("/visual/assets", response_class=HTMLResponse, name="visual_assets")
async def visual_assets_page(request: Request, asset_type: str = "", archived: bool = False) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="visual_assets.html",
        context=template_context(
            request,
            page_title="Visual Asset Library",
            assets=list_assets(asset_type=asset_type or None, include_archived=archived),
            asset_types=ASSET_TYPES,
            selected_asset_type=asset_type,
            include_archived=archived,
            message=request.session.pop("visual_message", None),
            error=request.session.pop("visual_error", None),
        ),
    )


@app.get("/visual/assets/upload", response_class=HTMLResponse, name="visual_asset_upload")
async def visual_asset_upload_page(
    request: Request,
    template_key: str = "",
    slot_key: str = "",
    replace_asset_id: int = 0,
) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    guidance = None
    replace_asset = get_asset(replace_asset_id) if replace_asset_id else None
    if replace_asset is not None:
        metadata = replace_asset.get("metadata") or {}
        template_key = template_key or str(metadata.get("template_key") or "")
        slot_key = slot_key or str(metadata.get("slot_key") or "")
    if template_key and slot_key:
        try:
            guidance = REGISTRY.get(template_key).slot(slot_key).as_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail="Asset slot was not found.")
    return templates.TemplateResponse(
        request=request,
        name="visual_asset_upload.html",
        context=template_context(
            request,
            page_title="Upload Visual Asset",
            registry=REGISTRY.as_dicts(),
            generic_guidance={
                kind: asset_type_guidance(kind) for kind in ASSET_TYPES
            },
            asset_types=ASSET_TYPES,
            selected_template_key=template_key,
            selected_slot_key=slot_key,
            guidance=guidance or asset_type_guidance("other"),
            replace_asset=replace_asset,
            error=request.session.pop("visual_error", None),
        ),
    )


@app.post("/visual/assets/upload", name="visual_asset_upload_save")
async def visual_asset_upload_save(request: Request) -> Response:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        request.session["visual_error"] = "Choose a PNG, JPG, or WEBP file."
        return visual_redirect(request, "visual_asset_upload")
    data = await upload.read()
    template_key = str(form.get("template_key", "")).strip() or None
    slot_key = str(form.get("slot_key", "")).strip() or None
    raw_replace_id = str(form.get("replace_asset_id", "")).strip()
    replace_asset_id = int(raw_replace_id) if raw_replace_id.isdigit() else None
    try:
        asset_id, inspection = await __import__("asyncio").to_thread(
            save_asset,
            data,
            filename=str(getattr(upload, "filename", "upload")),
            name=str(form.get("name", "")),
            asset_type=str(form.get("asset_type", "other")),
            actor=visual_actor(request),
            template_key=template_key,
            slot_key=slot_key,
            focal_x=float(str(form.get("focal_x", "0.5")) or "0.5"),
            focal_y=float(str(form.get("focal_y", "0.5")) or "0.5"),
            acknowledge_quality=str(form.get("acknowledge_quality", "")).casefold() in {"1", "on", "true"},
            allow_crop=str(form.get("allow_crop", "")).casefold() in {"1", "on", "true"},
            replace_asset_id=replace_asset_id,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
        query = "?template_key={}&slot_key={}".format(
            template_key or "",
            slot_key or "",
        )
        if replace_asset_id is not None:
            query += "&replace_asset_id={}".format(replace_asset_id)
        return RedirectResponse(
            url="{}{}".format(request.url_for("visual_asset_upload"), query),
            status_code=status.HTTP_303_SEE_OTHER,
        )
    request.session["visual_message"] = "Asset #{} uploaded and normalized to {} x {} px.".format(
        asset_id,
        inspection["normalized_width"],
        inspection["normalized_height"],
    )
    return visual_redirect(request, "visual_assets")


@app.get("/visual/assets/{asset_id}", response_class=HTMLResponse, name="visual_asset_detail")
async def visual_asset_detail(request: Request, asset_id: int) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    asset = get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset was not found.")
    return templates.TemplateResponse(
        request=request,
        name="visual_asset_detail.html",
        context=template_context(
            request,
            page_title=asset["name"],
            asset=asset,
            message=request.session.pop("visual_message", None),
            error=request.session.pop("visual_error", None),
        ),
    )


@app.get("/visual/assets/{asset_id}/image", name="visual_asset_image")
async def visual_asset_image(request: Request, asset_id: int, thumbnail: bool = False, download: bool = False) -> Response:
    if redirect := login_redirect(request):
        return redirect
    asset = get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset was not found.")
    storage_key = asset["metadata"].get("thumbnail_key") if thumbnail else asset["storage_key"]
    if not storage_key:
        raise HTTPException(status_code=404, detail="Asset preview is unavailable.")
    try:
        data = asset_bytes(storage_key)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Asset file is unavailable.")
    disposition = "attachment" if download else "inline"
    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": '{}; filename="asset-{}.png"'.format(disposition, asset_id),
        },
    )


@app.post("/visual/assets/{asset_id}/rename", name="visual_asset_rename")
async def visual_asset_rename(request: Request, asset_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        rename_asset(asset_id, str(form.get("name", "")), visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    return visual_redirect(request, "visual_assets")


@app.post("/visual/assets/{asset_id}/archive", name="visual_asset_archive")
async def visual_asset_archive(request: Request, asset_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        archive_asset(asset_id, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Asset archived."
    return visual_redirect(request, "visual_assets")


@app.post("/visual/assets/{asset_id}/restore", name="visual_asset_restore")
async def visual_asset_restore(request: Request, asset_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    archive_asset(asset_id, visual_actor(request), restore=True)
    request.session["visual_message"] = "Asset restored."
    return visual_redirect(request, "visual_assets")


@app.post("/visual/assets/{asset_id}/delete", name="visual_asset_delete")
async def visual_asset_delete(request: Request, asset_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        delete_asset(asset_id, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Archived asset permanently deleted."
    return visual_redirect(request, "visual_assets")


@app.get("/visual/themes", response_class=HTMLResponse, name="visual_themes")
async def visual_themes_page(request: Request, archived: bool = False) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="visual_themes.html",
        context=template_context(
            request,
            page_title="Visual Themes",
            themes=list_themes(include_archived=archived),
            assets=list_assets(limit=200),
            message=request.session.pop("visual_message", None),
            error=request.session.pop("visual_error", None),
        ),
    )


@app.get("/visual/themes/new", response_class=HTMLResponse, name="visual_theme_new")
async def visual_theme_new(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="visual_theme_edit.html",
        context=template_context(request, page_title="Create Visual Theme", theme=None, assets=list_assets(limit=200)),
    )


@app.get("/visual/themes/{theme_id}", response_class=HTMLResponse, name="visual_theme_edit")
async def visual_theme_edit(request: Request, theme_id: int) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    theme = get_theme(theme_id)
    if theme is None:
        raise HTTPException(status_code=404, detail="Theme was not found.")
    return templates.TemplateResponse(
        request=request,
        name="visual_theme_edit.html",
        context=template_context(request, page_title="Edit {}".format(theme["name"]), theme=theme, assets=list_assets(limit=200), error=request.session.pop("visual_error", None)),
    )


@app.post("/visual/themes/save", name="visual_theme_save")
async def visual_theme_save(request: Request) -> Response:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    raw_id = str(form.get("theme_id", "")).strip()
    try:
        settings = json.loads(str(form.get("settings_json", "{}")) or "{}")
        if not isinstance(settings, dict):
            raise ValueError("Theme settings must be a JSON object.")
        assets = dict(settings.get("assets") or {})
        for slot in ("background", "header_graphic", "logo", "watermark"):
            raw_asset = str(form.get("theme_asset_{}".format(slot), "")).strip()
            if raw_asset.isdigit() and int(raw_asset) > 0:
                assets[slot] = int(raw_asset)
            elif "theme_asset_{}".format(slot) in form:
                assets.pop(slot, None)
        settings["assets"] = assets
        theme_id = save_theme(
            name=str(form.get("name", "")),
            description=str(form.get("description", "")),
            settings=settings,
            actor=visual_actor(request),
            theme_id=int(raw_id) if raw_id.isdigit() else None,
        )
    except (json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
        return visual_redirect(request, "visual_theme_edit", theme_id=int(raw_id)) if raw_id.isdigit() else visual_redirect(request, "visual_theme_new")
    request.session["visual_message"] = "Theme saved."
    return visual_redirect(request, "visual_theme_edit", theme_id=theme_id)


@app.post("/visual/themes/{theme_id}/default", name="visual_theme_default")
async def visual_theme_default(request: Request, theme_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        set_default_theme(theme_id, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Default theme updated."
    return visual_redirect(request, "visual_themes")


@app.post("/visual/themes/{theme_id}/duplicate", name="visual_theme_duplicate")
async def visual_theme_duplicate(request: Request, theme_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        duplicate_id = duplicate_theme(theme_id, visual_actor(request))
    except (sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
        return visual_redirect(request, "visual_themes")
    request.session["visual_message"] = "Theme duplicated. Customize the copy before assigning it."
    return visual_redirect(request, "visual_theme_edit", theme_id=duplicate_id)


@app.post("/visual/themes/{theme_id}/archive", name="visual_theme_archive")
async def visual_theme_archive(request: Request, theme_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        archive_theme(theme_id, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Theme archived."
    return visual_redirect(request, "visual_themes")


@app.post("/visual/themes/{theme_id}/restore", name="visual_theme_restore")
async def visual_theme_restore(request: Request, theme_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        restore_theme(theme_id, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Theme restored."
    return visual_redirect(request, "visual_themes")


@app.post("/visual/themes/{theme_id}/delete", name="visual_theme_delete")
async def visual_theme_delete(request: Request, theme_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        delete_theme(theme_id, visual_actor(request))
    except ValueError as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Archived theme permanently deleted."
    return visual_redirect(request, "visual_themes")


@app.get("/visual/global", response_class=HTMLResponse, name="visual_globals")
async def visual_globals_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="visual_globals.html",
        context=template_context(
            request,
            page_title="Global Visual Settings",
            global_settings=get_global_settings(),
            schedules=list_global_schedules(),
            themes=list_themes(),
            assets=list_assets(limit=200),
            audits=list_recent_audit(40),
            message=request.session.pop("visual_message", None),
            error=request.session.pop("visual_error", None),
        ),
    )


@app.post("/visual/global", name="visual_globals_save")
async def visual_globals_save(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        settings = json.loads(str(form.get("settings_json", "{}")) or "{}")
        if not isinstance(settings, dict):
            raise ValueError("Global settings must be a JSON object.")
        assets = dict(settings.get("assets") or {})
        for slot in ("background", "header_graphic", "logo", "watermark"):
            field = "global_asset_{}".format(slot)
            raw_asset = str(form.get(field, "")).strip()
            if raw_asset.isdigit() and int(raw_asset) > 0:
                assets[slot] = int(raw_asset)
            elif field in form:
                assets.pop(slot, None)
        settings["assets"] = assets
        save_global_settings_draft(settings, visual_actor(request))
    except (json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Global visual defaults saved as a draft. Live renderers are unchanged."
    return visual_redirect(request, "visual_globals")


@app.post("/visual/global/publish", name="visual_globals_publish")
async def visual_globals_publish(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_visual_admin(request)
    try:
        publish_global_settings(visual_actor(request))
    except (sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
        audit_user = current_user(request)
        record_audit(
            actor_user_id=audit_user.get("id"), actor_label=audit_user.get("username") or "dashboard",
            action="content.publish", target_type="visual_global_settings",
            success=False, error=type(exc).__name__,
        )
    else:
        request.session["visual_message"] = "Global visual defaults published."
        audit_user = current_user(request)
        record_audit(
            actor_user_id=audit_user.get("id"), actor_label=audit_user.get("username") or "dashboard",
            action="content.publish", target_type="visual_global_settings",
        )
    return visual_redirect(request, "visual_globals")


@app.get("/visual/reference", response_class=HTMLResponse, name="visual_size_reference")
async def visual_size_reference(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="visual_reference.html",
        context=template_context(request, page_title="Visual Upload Size Reference", registry=REGISTRY.as_dicts()),
    )


@app.get("/visual/export", name="visual_export")
async def visual_export(request: Request, template_key: str = "") -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    document = export_configuration(template_key or None)
    return JSONResponse(
        document,
        headers={"Content-Disposition": 'attachment; filename="broeden-visual-content.json"'},
    )


@app.post("/visual/import", name="visual_import")
async def visual_import(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    form = await require_visual_admin(request)
    try:
        document = json.loads(str(form.get("configuration_json", "")))
        imported = import_configuration_as_drafts(document, visual_actor(request))
    except (json.JSONDecodeError, sqlite3.Error, ValueError) as exc:
        request.session["visual_error"] = str(exc)
    else:
        request.session["visual_message"] = "Imported {} template configuration(s) as drafts. Nothing was published.".format(len(imported))
    return visual_redirect(request, "visual_globals")


@app.get("/api/visual/templates", response_class=JSONResponse, name="api_visual_templates")
async def api_visual_templates(request: Request) -> JSONResponse:
    if not dashboard_enabled() or not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required.")
    return JSONResponse({"templates": list(REGISTRY.as_dicts())})


@app.get("/settings", response_class=HTMLResponse, name="settings")
async def settings(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="settings_overview.html",
        context=template_context(
            request,
            page_title="Settings",
            discord_metadata=picker_metadata(),
            roles=list_roles(),
            feature_count=len(feature_inventory()),
            oauth_ready=discord_oauth_configured(),
            database_path=str(settings_database_path()),
            recent_changes=recent_setting_changes(limit=5),
        ),
    )


@app.get("/settings/permissions", response_class=HTMLResponse, name="settings_permissions")
async def settings_permissions(request: Request) -> HTMLResponse:
    return RedirectResponse(
        url=request.url_for("settings_access"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/settings/discord", response_class=HTMLResponse, name="settings_discord")
async def settings_discord(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="discord_connection.html",
        context=template_context(
            request,
            page_title="Discord Connection",
            discord_metadata=picker_metadata(),
            message=request.session.pop("settings_message", None),
            error=request.session.pop("settings_error", None),
        ),
    )


@app.get("/settings/access", response_class=HTMLResponse, name="settings_access")
async def settings_access(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    return templates.TemplateResponse(
        request=request,
        name="access.html",
        context=template_context(
            request,
            page_title="Dashboard Access",
            access=access_overview(list_dashboard_users()),
            discord_metadata=picker_metadata(),
            message=request.session.pop("access_message", None),
            error=request.session.pop("access_error", None),
        ),
    )


def access_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("settings_access"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/settings/access/roles/save", name="access_role_save")
async def access_role_save(request: Request) -> RedirectResponse:
    await require_action_csrf(request)
    form = await request.form()
    raw_role_id = str(form.get("role_id", "")).strip()
    try:
        role_id = int(raw_role_id) if raw_role_id else None
        save_custom_role(
            role_id=role_id,
            name=str(form.get("name", "")),
            description=str(form.get("description", "")),
            permissions=form.getlist("permissions"),
            changed_by=dashboard_user_label(request),
        )
    except (TypeError, ValueError) as exc:
        request.session["access_error"] = str(exc)
    else:
        request.session["access_message"] = "Dashboard role saved."
    return access_redirect(request)


@app.post("/settings/access/mappings/save", name="access_mapping_save")
async def access_mapping_save(request: Request) -> RedirectResponse:
    await require_action_csrf(request)
    form = await request.form()
    role_ids = re.findall(r"\d{17,20}", str(form.get("discord_role_ids", "")))
    try:
        replace_discord_role_mappings(
            role_ids,
            int(str(form.get("dashboard_role_id", "")).strip()),
            changed_by=dashboard_user_label(request),
        )
    except (TypeError, ValueError) as exc:
        request.session["access_error"] = str(exc)
    else:
        request.session["access_message"] = (
            f"Saved {len(role_ids):,} Discord role mapping{'s' if len(role_ids) != 1 else ''}."
        )
    return access_redirect(request)


@app.post(
    "/settings/access/mappings/{discord_role_id}/{dashboard_role_id}/remove",
    name="access_mapping_remove",
)
async def access_mapping_remove(
    request: Request, discord_role_id: str, dashboard_role_id: int,
) -> RedirectResponse:
    await require_action_csrf(request)
    remove_discord_role_mapping(
        discord_role_id,
        dashboard_role_id,
        changed_by=dashboard_user_label(request),
    )
    request.session["access_message"] = "Discord role mapping removed."
    return access_redirect(request)


@app.post("/settings/access/users/{user_id}/role", name="access_user_role")
async def access_user_role(request: Request, user_id: int) -> RedirectResponse:
    await require_action_csrf(request)
    form = await request.form()
    try:
        role_id = int(str(form.get("role_id", "")).strip())
        if str(form.get("action", "assign")).strip() == "remove":
            remove_direct_role(user_id, role_id, changed_by=dashboard_user_label(request))
            message = "Direct dashboard role removed."
        else:
            assign_direct_role(user_id, role_id, changed_by=dashboard_user_label(request))
            message = "Direct dashboard role assigned."
    except (TypeError, ValueError) as exc:
        request.session["access_error"] = str(exc)
    else:
        request.session["access_message"] = message
    return access_redirect(request)


@app.post("/settings/access/users/{user_id}/status", name="access_user_status")
async def access_user_status(request: Request, user_id: int) -> RedirectResponse:
    await require_action_csrf(request)
    form = await request.form()
    try:
        set_user_status(
            user_id,
            str(form.get("status", "")),
            changed_by=dashboard_user_label(request),
        )
    except ValueError as exc:
        request.session["access_error"] = str(exc)
    else:
        request.session["access_message"] = "Dashboard user status updated."
    return access_redirect(request)


@app.post("/settings/access/users/{user_id}/permission", name="access_user_permission")
async def access_user_permission(request: Request, user_id: int) -> RedirectResponse:
    await require_action_csrf(request)
    form = await request.form()
    try:
        set_user_permission_override(
            user_id,
            str(form.get("permission_key", "")),
            str(form.get("mode", "inherit")),
            changed_by=dashboard_user_label(request),
        )
    except ValueError as exc:
        request.session["access_error"] = str(exc)
    else:
        request.session["access_message"] = "User permission override updated."
    return access_redirect(request)


@app.get("/settings/audit", response_class=HTMLResponse, name="settings_audit")
async def settings_audit(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    action = request.query_params.get("action", "").strip()
    actor = request.query_params.get("actor", "").strip()
    return templates.TemplateResponse(
        request=request,
        name="audit_log.html",
        context=template_context(
            request,
            page_title="Audit Log",
            events=list_audit_events(limit=200, action=action, actor=actor),
            filters={"action": action, "actor": actor},
        ),
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
    user = current_user(request)
    record_audit(
        actor_user_id=user.get("id"), actor_label=user.get("username") or "dashboard",
        action="discord_metadata.refresh.queued", target_type="dashboard_action",
        target_id=str(action_id), after={"status": "pending"},
    )
    return RedirectResponse(
        url=request.url_for("settings_discord"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/settings/advanced", response_class=HTMLResponse, name="settings_advanced")
async def settings_advanced(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    advanced_settings = [
        setting
        for section in settings_for_dashboard().values()
        for setting in section
        if setting.get("feature_key") in {"system", "imports"}
    ]
    return templates.TemplateResponse(
        request=request,
        name="feature_detail.html",
        context=template_context(
            request,
            page_title="Advanced Settings",
            feature={
                "key": "advanced",
                "name": "Advanced Settings",
                "description": "Technical operator defaults and compatibility controls.",
                "category": "System",
                "enabled": True,
                "health": "healthy",
                "support_status": "internal",
                "missing_settings": [],
            },
            feature_settings=advanced_settings,
            save_url=request.url_for("advanced_settings_save"),
            settings_editable=has_permission(request, "settings.manage"),
            message=request.session.pop("settings_message", None),
            error=request.session.pop("settings_error", None),
            asset_options=[],
        ),
    )


@app.get("/settings/features", response_class=HTMLResponse, name="settings_features")
async def settings_features(request: Request) -> HTMLResponse:
    return RedirectResponse(
        url=request.url_for("features_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/features", response_class=HTMLResponse, name="features_page")
async def features_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    features = [
        feature for feature in feature_inventory()
        if has_permission(request, str(feature["permission"]))
    ]
    categories: dict[str, list[dict[str, Any]]] = {}
    for feature in features:
        categories.setdefault(str(feature["category"]), []).append(feature)
    return templates.TemplateResponse(
        request=request,
        name="features.html",
        context=template_context(
            request,
            page_title="Features",
            feature_categories=categories,
        ),
    )


@app.get("/features/{feature_key}", response_class=HTMLResponse, name="feature_detail")
async def feature_detail(request: Request, feature_key: str) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    definition = FEATURES_BY_KEY.get(feature_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Feature was not found.")
    if not has_permission(request, definition.permission):
        raise HTTPException(status_code=403, detail="This feature is not available to your dashboard role.")
    return templates.TemplateResponse(
        request=request,
        name="feature_detail.html",
        context=template_context(
            request,
            page_title=definition.name,
            feature=feature_snapshot(definition),
            feature_settings=settings_for_feature(feature_key),
            save_url=request.url_for("feature_settings_save", feature_key=feature_key),
            settings_editable=has_permission(request, "features.manage"),
            message=request.session.pop("settings_message", None),
            error=request.session.pop("settings_error", None),
            asset_options=list_embed_templates(sort="name", order="asc"),
        ),
    )


async def _save_settings_form(request: Request, allowed_keys: set[str], redirect_url: str) -> RedirectResponse:
    await require_action_csrf(request)
    form = await request.form()
    values = {
        str(key)[len("setting__"):]: str(value)
        for key, value in form.multi_items()
        if str(key).startswith("setting__")
    }
    if not values or set(values) - allowed_keys:
        raise HTTPException(status_code=400, detail="The settings request was invalid.")
    actor = dashboard_user_label(request)
    try:
        changed = set_settings(values, changed_by=actor)
    except ValueError as exc:
        request.session["settings_error"] = str(exc)
    else:
        request.session["settings_message"] = (
            f"Saved {len(changed):,} changed setting{'s' if len(changed) != 1 else ''}."
            if changed else "No settings changed."
        )
        if changed:
            record_audit(
                actor_user_id=current_user(request).get("id"),
                actor_label=actor,
                action="configuration.changed",
                target_type="settings_group",
                target_id=redirect_url,
                after={"keys": sorted(changed)},
            )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/features/{feature_key}/save", name="feature_settings_save")
async def feature_settings_save(request: Request, feature_key: str) -> RedirectResponse:
    definition = FEATURES_BY_KEY.get(feature_key)
    if definition is None:
        raise HTTPException(status_code=404, detail="Feature was not found.")
    if not has_permission(request, "features.manage"):
        raise HTTPException(status_code=403, detail="Feature management permission is required.")
    allowed_keys = {str(item["key"]) for item in settings_for_feature(feature_key) if item["editable"]}
    return await _save_settings_form(
        request,
        allowed_keys,
        str(request.url_for("feature_detail", feature_key=feature_key)),
    )


@app.post("/settings/advanced/save", name="advanced_settings_save")
async def advanced_settings_save(request: Request) -> RedirectResponse:
    allowed_keys = {
        definition.key for definition in DEFINITIONS_BY_KEY.values()
        if definition.visible and feature_key_for_setting(definition.key) in {"system", "imports"}
    }
    return await _save_settings_form(
        request, allowed_keys, str(request.url_for("settings_advanced"))
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
            message_context=message_context_overview(),
            message=message,
            error=error,
        ),
    )


def operations_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("operations_page"),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def reminders_redirect(request: Request, reminder_id: int | None = None) -> RedirectResponse:
    url = (
        request.url_for("reminders_detail", reminder_id=reminder_id)
        if reminder_id is not None
        else request.url_for("reminders_page")
    )
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/operations/reminders", response_class=HTMLResponse, name="reminders_page")
async def reminders_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    filters = {
        "guild_id": request.query_params.get("guild_id", "").strip(),
        "reminder_type": request.query_params.get("reminder_type", "").strip(),
        "status": request.query_params.get("status", "").strip(),
        "creator": request.query_params.get("creator", "").strip(),
        "channel": request.query_params.get("channel", "").strip(),
        "recurrence": request.query_params.get("recurrence", "").strip(),
        "date_from": request.query_params.get("date_from", "").strip(),
        "date_to": request.query_params.get("date_to", "").strip(),
    }
    try:
        reminders = list_dashboard_reminders(**filters)
        overview = dashboard_reminder_overview(**filters)
    except (OSError, sqlite3.Error, ValueError) as exc:
        reminders = []
        overview = {
            "upcoming": 0, "completed": 0, "cancelled": 0,
            "failed": 0, "active_subscriptions": 0, "failed_deliveries": 0,
        }
        error = str(exc)
    else:
        error = request.session.pop("reminders_error", None)
    return templates.TemplateResponse(
        request=request,
        name="reminders.html",
        context=template_context(
            request,
            page_title="Reminder Operations",
            reminders=reminders,
            overview=overview,
            filters=filters,
            message=request.session.pop("reminders_message", None),
            error=error,
        ),
    )


@app.get(
    "/operations/reminders/{reminder_id}",
    response_class=HTMLResponse,
    name="reminders_detail",
)
async def reminders_detail(request: Request, reminder_id: int) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    detail = dashboard_reminder_detail(
        reminder_id,
        guild_id=request.query_params.get("guild_id", "").strip(),
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Reminder was not found.")
    if not has_permission(request, "reminders.manage"):
        detail["subscriptions"] = []
        for delivery in detail["deliveries"]:
            delivery["recipient_user_id"] = "Private"
            delivery["error_detail"] = None
    return templates.TemplateResponse(
        request=request,
        name="reminder_detail.html",
        context=template_context(
            request,
            page_title=detail["reminder"]["title"],
            detail=detail,
            message=request.session.pop("reminders_message", None),
            error=request.session.pop("reminders_error", None),
        ),
    )


@app.post("/operations/reminders/{reminder_id}/action", name="reminders_action")
async def reminders_action(request: Request, reminder_id: int) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    if not has_permission(request, "reminders.manage"):
        raise HTTPException(status_code=403, detail="Admin access is required.")
    form = await request.form()
    action = str(form.get("action", "")).strip()
    guild_id = str(form.get("guild_id", "")).strip()
    payload: dict[str, Any] = {}
    if action == "cancel":
        payload["reason"] = str(form.get("reason", "")).strip()
    elif action == "retry":
        payload["delivery_id"] = str(form.get("delivery_id", "")).strip()
    elif action == "edit":
        payload = {
            key: str(form.get(key, "")).strip()
            for key in (
                "title",
                "description",
                "scheduled_at_utc",
                "destination_channel_id",
                "destination_channel_name",
                "timings",
            )
        }
    try:
        action_id = queue_reminder_action(
            reminder_id,
            action=action,
            requested_by=dashboard_user_label(request),
            guild_id=guild_id,
            payload=payload,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        request.session["reminders_error"] = str(exc)
    else:
        request.session["reminders_message"] = (
            f"Reminder action #{action_id} queued. The bot will process it within about 30 seconds."
        )
    return reminders_redirect(request, reminder_id)


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


def dashboard_user_label(request: Request) -> str:
    return str(request.session.get("dashboard_user", "dashboard"))


def knowledge_sources_summary() -> dict[str, Any]:
    documents = list_documents()
    ai_sources = [
        source
        for source in list_kb_sources()
        if not str(source.get("source_name", "")).startswith("live-discord:")
    ]
    live_sources = list_live_knowledge_sources_sync()
    ai_connected = sum(1 for source in ai_sources if source.get("ai_enabled"))
    return {
        "documents": documents,
        "ai_sources": ai_sources,
        "live_sources": live_sources,
        "has_reindexable_documents": any(
            document.get("reindex_supported") for document in documents
        ),
        "counts": {
            "documents": len(documents),
            "ai_sources": len(ai_sources),
            "ai_connected": ai_connected,
            "live_sources": len(live_sources),
            "live_ai_connected": sum(
                1 for source in live_sources if source.get("ai_enabled")
            ),
            "public_items": (
                sum(1 for item in documents if item.get("visibility") == "public")
                + sum(1 for item in ai_sources if item.get("source_visibility") == "public")
                + sum(1 for item in live_sources if item.get("visibility") == "public")
            ),
            "staff_items": (
                sum(1 for item in documents if item.get("visibility") == "staff")
                + sum(
                    1
                    for item in ai_sources
                    if item.get("source_visibility") in {"staff", "staff_only"}
                )
                + sum(1 for item in live_sources if item.get("visibility") == "staff_only")
            ),
        },
    }


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


@app.get("/knowledge", response_class=HTMLResponse, name="knowledge_page")
async def knowledge_page(request: Request) -> HTMLResponse:
    if redirect := login_redirect(request):
        return redirect
    summary = knowledge_sources_summary()
    return templates.TemplateResponse(
        request=request,
        name="knowledge.html",
        context=template_context(
            request,
            page_title="Knowledge",
            documents=summary["documents"],
            ai_sources=summary["ai_sources"],
            live_sources=summary["live_sources"],
            knowledge_counts=summary["counts"],
            has_reindexable_documents=summary["has_reindexable_documents"],
            kb_status=get_kb_status(),
            source_types=sorted(SOURCE_TYPES),
            live_source_types=sorted(KNOWLEDGE_SOURCE_TYPES),
            visibilities=sorted(VISIBILITIES),
            live_visibilities=sorted(KNOWLEDGE_VISIBILITIES),
            live_sync_modes=sorted(KNOWLEDGE_SYNC_MODES),
            default_guild_id=os.getenv("GUILD_ID", "").strip(),
            recent_audit=recent_knowledge_audit(),
            message=request.session.pop("knowledge_message", None),
            error=request.session.pop("knowledge_error", None),
        ),
    )


@app.get("/settings/knowledge", response_class=HTMLResponse, name="knowledge_settings_legacy")
async def knowledge_settings_legacy(request: Request) -> RedirectResponse:
    return knowledge_redirect(request)


@app.get("/settings/knowledge/{doc_key}", response_class=HTMLResponse, name="knowledge_detail_settings_legacy")
async def knowledge_detail_settings_legacy(request: Request, doc_key: str) -> RedirectResponse:
    return knowledge_redirect(request, doc_key)


@app.get("/settings/knowledge/{doc_key}/preview", response_class=HTMLResponse, name="knowledge_preview_settings_legacy")
async def knowledge_preview_settings_legacy(request: Request, doc_key: str) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("knowledge_preview", doc_key=doc_key),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/settings/knowledge/{doc_key}/edit", response_class=HTMLResponse, name="knowledge_edit_settings_legacy")
async def knowledge_edit_settings_legacy(request: Request, doc_key: str) -> RedirectResponse:
    return RedirectResponse(
        url=request.url_for("knowledge_edit", doc_key=doc_key),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    "/knowledge/{doc_key}",
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


@app.get(
    "/knowledge/{doc_key}/preview",
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


@app.get(
    "/knowledge/{doc_key}/edit",
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
    requested_by = str(request.session.get("dashboard_user", "dashboard"))
    try:
        action_id = queue_knowledge_reindex(
            doc_key,
            requested_by,
        )
        metadata_action_id = queue_metadata_refresh(requested_by)
    except ValueError as exc:
        request.session["knowledge_error"] = str(exc)
    else:
        request.session["knowledge_message"] = (
            f"Reindex queued as dashboard action #{action_id} for "
            f"{document['display_name']}. Discord emoji metadata refresh "
            f"queued as action #{metadata_action_id}."
        )
    return knowledge_redirect(request, doc_key)


@app.post("/knowledge/reindex-all", name="knowledge_reindex_all")
async def knowledge_reindex_all(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    requested_by = str(request.session.get("dashboard_user", "dashboard"))
    action_id = queue_knowledge_reindex(
        None,
        requested_by,
    )
    metadata_action_id = queue_metadata_refresh(requested_by)
    request.session["knowledge_message"] = (
        f"Full knowledge reindex queued as dashboard action #{action_id}. "
        f"Discord emoji metadata refresh queued as action #{metadata_action_id}."
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
    user = current_user(request)
    record_audit(
        actor_user_id=user.get("id"), actor_label=user.get("username") or "dashboard",
        action="service.restart", target_type="service", target_id="bot",
        success=ok, error=None if ok else message,
    )
    return operations_redirect(request)


@app.post("/operations/restart-dashboard", name="restart_dashboard")
async def restart_dashboard(request: Request) -> RedirectResponse:
    if redirect := login_redirect(request):
        return redirect
    await require_action_csrf(request)
    ok, message = restart_service("dashboard")
    request.session["operations_message" if ok else "operations_error"] = message
    user = current_user(request)
    record_audit(
        actor_user_id=user.get("id"), actor_label=user.get("username") or "dashboard",
        action="service.restart", target_type="service", target_id="dashboard",
        success=ok, error=None if ok else message,
    )
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
    user = current_user(request)
    succeeded = "destination" in locals()
    record_audit(
        actor_user_id=user.get("id"), actor_label=user.get("username") or "dashboard",
        action="database.backup", target_type="database",
        target_id=destination.name if succeeded else "shared",
        success=succeeded,
        error=None if succeeded else request.session.get("operations_error"),
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


@app.get("/api/discord/emojis", response_class=JSONResponse)
async def api_discord_emojis(request: Request) -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    return JSONResponse(emojis_metadata())


@app.get("/api/discord/guild-structure", response_class=JSONResponse)
async def api_discord_guild_structure(request: Request) -> JSONResponse:
    if redirect := login_redirect(request):
        return redirect
    return JSONResponse(guild_structure())


@app.get("/health", response_class=JSONResponse)
async def health() -> JSONResponse:
    database = database_status(find_database_path())
    ai_usage = ai_usage_overview(limit=1)
    return JSONResponse(
        {
            "status": "ok" if dashboard_enabled() else "disabled",
            "dashboard_enabled": dashboard_enabled(),
            "ai": {
                "enabled": ai_usage["config"]["enabled"],
                "api_key_present": ai_usage["config"]["api_key_present"],
                "default_model": ai_usage["config"]["default_model"],
                "daily_spend_usd": ai_usage["daily_spend_usd"],
                "monthly_spend_usd": ai_usage["monthly_spend_usd"],
                "last_success_at": ai_usage["last_success_at"],
                "last_error": ai_usage["last_error"],
            },
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
    duplicate_theme,
    list_global_schedules,
