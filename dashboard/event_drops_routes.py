"""Garden Event Drops routes; Discord operations are queued for the bot."""

from __future__ import annotations

import asyncio
import csv
import io
import os
import secrets
import time
from datetime import datetime

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from dashboard.auth import csrf_is_valid, current_user
from dashboard.events_manager import (
    MAX_EVENT_IMAGE_BYTES,
    event_timezone,
    normalize_event_image,
)
from dashboard.rbac import record_audit
from utils.discord_metadata import initialize_discord_metadata_schema
from utils.event_drops import DEFAULTS, EventDrops
from utils.settings import get_setting


def guild_id():
    return str(get_setting("GUILD_ID", "") or os.getenv("GUILD_ID", "")).strip()


def service():
    result = EventDrops()
    result.initialize()
    return result


def picker(svc):
    initialize_discord_metadata_schema()
    channels = svc.rows(
        "SELECT id,name,parent_name FROM dashboard_discord_channels WHERE guild_id=? AND type IN ('text','news') AND is_thread=0 ORDER BY parent_name,position,name",
        (guild_id(),),
    )
    roles = svc.rows(
        "SELECT id,name FROM dashboard_discord_roles WHERE guild_id=? AND is_bot_role=0 ORDER BY position DESC",
        (guild_id(),),
    )
    return channels, roles


def form_values(form):
    values = {key: form.get(key, default) for key, default in DEFAULTS.items()}
    for field in ("start_at", "end_at"):
        raw = str(form.get(field) or "").strip()
        try:
            values[field] = (
                datetime.fromisoformat(raw).replace(tzinfo=event_timezone()).timestamp()
                if raw
                else None
            )
        except ValueError:
            raise ValueError("Enter a valid campaign date and time.") from None
    return values


def display_time(value):
    return (
        datetime.fromtimestamp(float(value), event_timezone()).strftime(
            "%b %d, %Y · %I:%M %p %Z"
        )
        if value
        else "—"
    )


def csv_cell(value):
    text = str(value or "")
    return (
        "'" + text
        if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
        else text
    )


def install_event_drop_routes(app, templates, context):
    templates.env.filters["drop_time"] = display_time

    def scoped_campaign(svc, campaign_id):
        try:
            return svc.campaign(campaign_id, guild_id())
        except ValueError as exc:
            raise HTTPException(404, "Campaign not found.") from exc

    def render(request, name, **values):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context=context(request, page_title="Event Drops", **values),
        )

    def audit(request, action, target):
        user = current_user(request)
        record_audit(
            action="event_drops." + action,
            actor_user_id=user.get("id"),
            actor_label=user.get("username", "dashboard"),
            target_type="event_drop_campaign",
            target_id=str(target),
        )

    async def checked_form(request):
        form = await request.form()
        if not csrf_is_valid(request, str(form.get("csrf", ""))):
            raise HTTPException(400, "Invalid CSRF token.")
        return form

    def redirect(campaign_id=None):
        return RedirectResponse(
            "/events/drops" + (f"/{campaign_id}" if campaign_id else ""),
            status_code=303,
        )

    @app.get("/events/drops", response_class=HTMLResponse, name="event_drops_list")
    async def listing(request: Request):
        svc = service()
        heartbeat = svc.rows("SELECT heartbeat FROM event_drop_worker WHERE id=1")
        return render(
            request,
            "event_drops.html",
            campaigns=svc.campaigns(guild_id()),
            worker_ready=bool(
                heartbeat and heartbeat[0]["heartbeat"] > time.time() - 90
            ),
            message=request.session.pop("drops_message", None),
        )

    def editor(request, svc, campaign, error=None):
        channels, roles = picker(svc)
        known = {c["id"] for c in channels}
        for cid in campaign.get("channels", []):
            if cid not in known:
                channels.append(
                    dict(
                        id=cid,
                        name=f"Unavailable channel ({cid})",
                        parent_name="Saved selections · check permissions",
                    )
                )
        known_roles = {r["id"] for r in roles}
        for rid in (
            set(campaign.get("eligible_roles", []) + campaign.get("excluded_roles", []))
            - known_roles
        ):
            roles.append(dict(id=rid, name=f"Unavailable role ({rid})"))
        values = dict(campaign)
        for key in ("start_at", "end_at"):
            if isinstance(values.get(key), (int, float)):
                values[key] = datetime.fromtimestamp(
                    values[key], event_timezone()
                ).strftime("%Y-%m-%dT%H:%M")
        assets = svc.rows(
            "SELECT id FROM event_drop_assets WHERE campaign_id=? AND created_at>0",
            (campaign.get("id", 0),),
        )
        return render(
            request,
            "event_drop_form.html",
            campaign=values,
            channels=channels,
            roles=roles,
            assets=assets,
            error=error,
            timezone_label=str(event_timezone()),
        )

    @app.get("/events/drops/new", response_class=HTMLResponse, name="event_drops_new")
    async def new(request: Request):
        return editor(
            request,
            service(),
            dict(DEFAULTS, channels=[], eligible_roles=[], excluded_roles=[]),
        )

    async def save_form(request, campaign_id=None):
        form = await checked_form(request)
        svc = service()
        existing = scoped_campaign(svc, campaign_id) if campaign_id else {}
        values = dict(form)
        try:
            values = form_values(form)
            channels, roles = picker(svc)
            known = {c["id"] for c in channels} | set(existing.get("channels", []))
            selected = form.getlist("channels")
            if set(selected) - known:
                raise ValueError(
                    "Select individual text channels from the server channel picker."
                )
            role_ids = (
                {r["id"] for r in roles}
                | set(existing.get("eligible_roles", []))
                | set(existing.get("excluded_roles", []))
            )
            eligible = form.getlist("eligible_roles")
            excluded = form.getlist("excluded_roles")
            if (set(eligible) | set(excluded)) - role_ids:
                raise ValueError("Select roles from this server.")
            uploads = [u for u in form.getlist("images") if getattr(u, "filename", "")]
            if len(uploads) > 10:
                raise ValueError("Upload at most 10 campaign images.")
            assets = []
            for upload in uploads:
                data = await upload.read(MAX_EVENT_IMAGE_BYTES + 1)
                assets.append(
                    await asyncio.to_thread(
                        normalize_event_image, data, upload.content_type
                    )
                )
            cid = svc.save(
                guild_id(),
                current_user(request).get("id", "dashboard"),
                values,
                selected,
                campaign_id,
                assets,
                form.getlist("remove_assets"),
                eligible,
                excluded,
            )
        except (ValueError, TypeError) as exc:
            draft = dict(DEFAULTS, **{k: v for k, v in values.items() if k in DEFAULTS})
            draft.update(
                id=campaign_id,
                channels=form.getlist("channels"),
                eligible_roles=form.getlist("eligible_roles"),
                excluded_roles=form.getlist("excluded_roles"),
            )
            response = editor(request, svc, draft, str(exc))
            response.status_code = 400
            return response
        audit(request, "updated" if campaign_id else "created", cid)
        return redirect(cid)

    @app.post("/events/drops/new", name="event_drops_create")
    async def create(request: Request):
        return await save_form(request)

    @app.get(
        "/events/drops/{campaign_id:int}/edit",
        response_class=HTMLResponse,
        name="event_drops_edit",
    )
    async def edit(request: Request, campaign_id: int):
        svc = service()
        c = scoped_campaign(svc, campaign_id)
        if c["status"] not in ("draft", "paused"):
            raise HTTPException(400, "Pause the campaign to edit it.")
        return editor(request, svc, c)

    @app.post("/events/drops/{campaign_id:int}/edit", name="event_drops_save")
    async def update(request: Request, campaign_id: int):
        return await save_form(request, campaign_id)

    @app.get(
        "/events/drops/{campaign_id:int}",
        response_class=HTMLResponse,
        name="event_drops_detail",
    )
    async def detail(request: Request, campaign_id: int):
        svc = service()
        c = scoped_campaign(svc, campaign_id)
        channels, _ = picker(svc)
        names = {r["id"]: r["name"] for r in channels}
        scores = svc.leaderboard(campaign_id)
        page = max(
            1,
            (
                int(request.query_params.get("page", "1"))
                if request.query_params.get("page", "1").isdigit()
                else 1
            ),
        )
        history = svc.rows(
            "SELECT * FROM event_drops WHERE campaign_id=? ORDER BY id DESC LIMIT 51 OFFSET ?",
            (campaign_id, (page - 1) * 50),
        )
        active = svc.rows(
            "SELECT * FROM event_drops WHERE campaign_id=? AND status='active' ORDER BY id DESC",
            (campaign_id,),
        )
        pending = svc.rows(
            "SELECT * FROM event_drops WHERE campaign_id=? AND status IN ('pending','sending') ORDER BY id DESC",
            (campaign_id,),
        )
        heartbeat = svc.rows("SELECT heartbeat FROM event_drop_worker WHERE id=1")
        return render(
            request,
            "event_drop_detail.html",
            campaign=c,
            scores=scores,
            history=history[:50],
            more=len(history) > 50,
            page=page,
            active_drops=active,
            pending_drops=pending,
            channel_names=names,
            submission_id=secrets.token_hex(16),
            worker_ready=bool(
                heartbeat and heartbeat[0]["heartbeat"] > time.time() - 90
            ),
            message=request.session.pop("drops_message", None),
            error=request.session.pop("drops_error", None),
        )

    @app.post("/events/drops/{campaign_id:int}/action", name="event_drops_action")
    async def action(request: Request, campaign_id: int):
        form = await checked_form(request)
        svc = service()
        scoped_campaign(svc, campaign_id)
        kind = str(form.get("action", ""))
        destination = campaign_id
        try:
            if kind == "end" and form.get("confirmation") != "END":
                raise ValueError(
                    "Type END to confirm ending the campaign and closing its drops."
                )
            if kind == "delete":
                svc.delete(campaign_id, guild_id())
                destination = None
            elif kind == "duplicate":
                destination = svc.duplicate(
                    campaign_id,
                    guild_id(),
                    current_user(request).get("id", "dashboard"),
                )
            elif kind == "drop":
                token = str(form.get("submission_id", ""))
                if not token or len(token) > 100:
                    raise ValueError("Reload the page before sending a manual drop.")
                did = svc.queue_manual(
                    campaign_id,
                    guild_id(),
                    f"garden:{campaign_id}:{token}",
                    form.get("channel_id") or None,
                )
                request.session["drops_message"] = (
                    f"Manual drop #{did} queued. Delivery status appears below."
                )
            else:
                svc.transition(campaign_id, guild_id(), kind)
        except ValueError as exc:
            request.session["drops_error"] = str(exc)
            return redirect(campaign_id)
        audit(request, kind, campaign_id)
        return redirect(destination)

    @app.get("/events/drops/{campaign_id:int}/export.csv", name="event_drops_export")
    async def export(request: Request, campaign_id: int):
        svc = service()
        scoped_campaign(svc, campaign_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ["user_id", "display_name", "total_points", "drops_claimed", "rank"]
        )
        for row in svc.leaderboard(campaign_id):
            writer.writerow(
                [
                    row["user_id"],
                    csv_cell(
                        row["display_name"]
                        or f'Unknown/Former Member ({row["user_id"]})'
                    ),
                    row["total_points"],
                    row["drops_claimed"],
                    row["rank"],
                ]
            )
        audit(request, "export", campaign_id)
        return Response(
            output.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="event-drops-{campaign_id}.csv"'
            },
        )

    @app.get(
        "/events/drops/{campaign_id:int}/claims/{drop_id:int}",
        response_class=HTMLResponse,
        name="event_drops_claimers",
    )
    async def claimers(request: Request, campaign_id: int, drop_id: int):
        svc = service()
        c = scoped_campaign(svc, campaign_id)
        rows = svc.rows(
            "SELECT q.*,m.display_name FROM event_drop_claims q LEFT JOIN event_drop_members m ON m.guild_id=? AND m.user_id=q.user_id WHERE q.campaign_id=? AND q.drop_id=? ORDER BY q.claimed_at",
            (guild_id(), campaign_id, drop_id),
        )
        return render(
            request,
            "event_drop_claimers.html",
            campaign=c,
            drop_id=drop_id,
            claims=rows,
        )

    @app.get(
        "/events/drops/{campaign_id:int}/assets/{asset_id:int}",
        name="event_drops_asset",
    )
    async def image(request: Request, campaign_id: int, asset_id: int):
        svc = service()
        scoped_campaign(svc, campaign_id)
        rows = svc.rows(
            "SELECT image_bytes,content_type FROM event_drop_assets WHERE campaign_id=? AND id=?",
            (campaign_id, asset_id),
        )
        if not rows:
            raise HTTPException(404, "Image not found.")
        return Response(
            rows[0]["image_bytes"],
            media_type=rows[0]["content_type"],
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )
