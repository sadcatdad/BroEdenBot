"""Scout Garden routes. Each handler revalidates live membership and capability."""
import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from dashboard.auth import csrf_is_valid, current_user
from dashboard.rbac import record_audit
from utils.scouts import Scouts, DEFAULTS, ACTIVITY_TYPES, ACTION_TYPES, CARD_LAYOUT_VERSION, WORKFLOW_STATUSES, STATUS_HELP, status_key, status_label, record_title
from utils.scout_access import dashboard_actor, fetch_member, member_actor
from utils.scout_ai import preview
from utils.scout_forms import FORM_DEFAULTS
from fastapi.responses import JSONResponse
from utils.settings import get_setting
from utils.discord_metadata import initialize_discord_metadata_schema

SECTIONS = {'overview': None, 'activity': 'activity.view', 'escalations':'activity.view', 'system': 'moderation.view',
            'reports': 'activity.view', 'checkins': None, 'settings': 'settings.manage', 'forms':'settings.manage'}


def guild_id():
    return str(get_setting('GUILD_ID', '') or os.getenv('GUILD_ID', '')).strip()


def service():
    s = Scouts()
    s.initialize()
    return s


def install_scout_routes(app, templates, context):
    templates.env.filters['scout_time'] = lambda v: datetime.fromtimestamp(float(v), timezone.utc).strftime('%b %d, %Y %H:%M UTC') if v else '—'
    templates.env.filters.update(scout_status=status_label,scout_status_key=status_key,scout_title=record_title)

    async def setup(request, cap=None):
        s = await asyncio.to_thread(service)
        try:
            actor = await dashboard_actor(request, s, guild_id())
            if cap:
                actor.require(cap)
            elif not actor.eligible or not any(p.startswith('scouts.') for p in actor.permissions):
                raise PermissionError('No Scout Operations access.')
            return s, actor
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    async def form(request):
        f = await request.form()
        if not csrf_is_valid(request, str(f.get('csrf', ''))):
            raise HTTPException(400, 'Invalid CSRF token.')
        return f

    def render(request, actor, **values):
        return templates.TemplateResponse(request=request, name='scouts.html', context=context(
            request, page_title='Scouts', scout_actor=actor,
            scout_can=lambda k: 'scouts.' + k in actor.permissions,
            sections=SECTIONS, activity_types=ACTIVITY_TYPES, action_types=ACTION_TYPES,
            card_layout_version=CARD_LAYOUT_VERSION, form_defaults=FORM_DEFAULTS,
            workflow_statuses=WORKFLOW_STATUSES,status_help=STATUS_HELP,now=time.time(), **values))

    def audit(request, action, rid=''):
        user = current_user(request)
        record_audit(actor_user_id=user['id'], actor_label=user['username'], action='scouts.' + action, target_type='scout_record', target_id=rid)

    def redirect(request, path='/scouts', message='Saved.'):
        request.session['scout_message'] = message
        return RedirectResponse(path, status_code=303)

    @app.get('/scouts', name='scouts_overview')
    async def overview(request: Request):
        return await listing(request, 'overview')

    @app.get('/scouts/section/{section}', name='scouts_section')
    async def listing(request: Request, section: str):
        if section not in SECTIONS:
            raise HTTPException(404)
        s, actor = await setup(request, SECTIONS[section])
        if section == 'checkins' and not any('scouts.checkins.' + k in actor.permissions for k in ('respond','request','view_all')):
            raise HTTPException(403)
        cfg = getattr(request.state, 'scout_config', None) or await asyncio.to_thread(s.settings, actor.guild_id)
        values = dict(section=section, config=cfg, records=[], message=request.session.pop('scout_message', None), error=getattr(request.state,'scout_error',None), detail=None, activity_draft=getattr(request.state,'scout_draft',{}))
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            days = max(1, min(3650, int(request.query_params.get('days', 30))))
        except ValueError:
            raise HTTPException(400, 'Enter valid page and date-range values.') from None
        q = request.query_params.get('q', '')[:100]
        status = request.query_params.get('status', '')
        scout = request.query_params.get('scout', '')
        values.update(page=page, days=days, query=q, status=status, selected_scout=scout)
        if section in ('activity','escalations','reports','system','checkins'):
            kind = dict(activity='manual',escalations='escalation', reports='report', system='system', checkins='checkin')[section]
            values['records'] = await asyncio.to_thread(s.listing, actor, kind, page=page, days=days, query=q, status=status, scout=scout)
        if section == 'overview':
            groups = []
            for label, kind, cap in [('Recent activity','manual','activity.view'), ('New reports','report','activity.view'), ('Help requests','escalation','activity.view'), ('Discord actions','system','moderation.view')]:
                if 'scouts.' + cap in actor.permissions:
                    rows = await asyncio.to_thread(s.listing, actor, kind, status='new' if kind == 'report' else 'unresolved' if kind=='escalation' else '', days=30)
                    groups.append((label, rows[:5]))
            if any('scouts.checkins.' + k in actor.permissions for k in ('respond','request','view_all')):
                groups.append(('Check-ins', (await asyncio.to_thread(s.listing, actor, 'checkin'))[:5]))
            values['groups'] = groups
        if section in ('settings', 'overview'):
            runtime = await asyncio.to_thread(s.rows, 'SELECT * FROM scout_runtime WHERE guild_id=?', (actor.guild_id,))
            values['runtime'] = json.loads(runtime[0]['data']) if runtime and runtime[0]['heartbeat'] > time.time()-90 else None
        if section == 'checkins' and 'scouts.checkins.request' in actor.permissions:
            runtime = await asyncio.to_thread(s.rows, 'SELECT data FROM scout_runtime WHERE guild_id=? AND heartbeat>?', (actor.guild_id,time.time()-90))
            values['roster'] = json.loads(runtime[0]['data']).get('scouts',[]) if runtime else []
        if section == 'settings':
            from utils.ai_config import get_ai_config
            ai_config=await asyncio.to_thread(get_ai_config)
            values.update(context_default_model=ai_config.models.default,
                          context_advanced_model=ai_config.models.advanced,context_advanced_enabled=ai_config.advanced_enabled)
            # A validation error preserves posted strings for the form; statistics
            # still use the last valid retention value, never unvalidated input.
            retention=(await asyncio.to_thread(s.settings,actor.guild_id))['retention_days']
            stats=await asyncio.to_thread(s.rows,'SELECT COUNT(*) count,MAX(observed_at) latest FROM scout_buffer WHERE guild_id=? AND observed_at>?',(actor.guild_id,time.time()-retention*86400))
            values['capture_stats']=stats[0]
            await asyncio.to_thread(initialize_discord_metadata_schema)
            values['roles'] = await asyncio.to_thread(s.rows, 'SELECT id,name FROM dashboard_discord_roles WHERE guild_id=? AND is_bot_role=0 ORDER BY position DESC', (actor.guild_id,))
            values['channels'] = await asyncio.to_thread(s.rows, "SELECT id,name,parent_name FROM dashboard_discord_channels WHERE guild_id=? AND type IN ('text','news') AND is_thread=0 ORDER BY position,name", (actor.guild_id,))
            values['categories'] = await asyncio.to_thread(s.rows, 'SELECT id,name FROM dashboard_discord_categories WHERE guild_id=? ORDER BY position,name', (actor.guild_id,))
            # Preserve missing saved selections so a routine edit never silently drops them.
            for name, keys in [('roles', ('scout_roles','report_roles','escalation_roles')), ('channels', ('monitored_channels','excluded_channels')), ('categories', ('monitored_categories','excluded_categories'))]:
                saved = {v for key in keys for v in cfg[key]}
                if name == 'channels':
                    saved |= {cfg['activity_channel'], cfg['moderation_channel']} - {''}
                known = {str(r['id']) for r in values[name]}
                values[name].extend(dict(id=v, name=f'Unavailable ({v})') for v in saved-known)
        if section in ('overview','settings') and 'scouts.settings.manage' in actor.permissions:
            values['failures'] = await asyncio.to_thread(s.rows, "SELECT j.id,j.record_id,r.short_id record_short_id,j.status,j.error FROM scout_jobs j JOIN scout_records r ON r.id=j.record_id AND r.guild_id=j.guild_id WHERE j.guild_id=? AND j.status IN ('failed','uncertain') ORDER BY j.created_at DESC LIMIT 30", (actor.guild_id,))
        return render(request, actor, **values)

    @app.get('/scouts/api/records', name='scouts_record_choices')
    async def record_choices(request: Request):
        s,actor=await setup(request,'activity.create')
        return JSONResponse(await asyncio.to_thread(s.reference_choices,actor,request.query_params.get('q','')))

    @app.get('/scouts/new/{kind}', name='scouts_compose')
    async def compose(request: Request, kind: str):
        if kind not in ('activity','escalation'):
            raise HTTPException(404)
        s,actor=await setup(request,'activity.create')
        cfg=await asyncio.to_thread(s.settings,actor.guild_id)
        draft=getattr(request.state,'scout_draft',{})
        related=str(draft.get('related') or request.query_params.get('related',''))
        choices=await asyncio.to_thread(s.reference_choices,actor,'')
        if related:
            try:
                ref=await asyncio.to_thread(s.resolve_reference,actor,related)
            except ValueError:
                if not draft:
                    raise HTTPException(400,'Choose a valid entry from the follow-up selector.') from None
                ref=dict(record_id='',short_id=related)
                choices.insert(0,dict(value=related,name='Review this selection: '+related[:50]))
            if ref['record_id'] and ref['short_id'] not in {c['value'] for c in choices}:
                choices.insert(0,dict(value=ref['short_id'],name='Follow up to #'+ref['short_id']))
            related=ref['short_id']
        await asyncio.to_thread(initialize_discord_metadata_schema)
        channels=await asyncio.to_thread(s.rows,"SELECT id,name FROM dashboard_discord_channels WHERE guild_id=? AND type IN ('text','news') ORDER BY position,name",(actor.guild_id,))
        return render(request,actor,section='compose',compose_kind=kind,config=cfg,detail=None,activity_draft=draft,
            related=related,choices=choices,channels=channels,message=None,error=getattr(request.state,'scout_error',None))

    @app.post('/scouts/forms', name='scouts_save_forms')
    async def save_forms(request: Request):
        s,actor=await setup(request,'settings.manage')
        f=await form(request)
        cfg=await asyncio.to_thread(s.settings,actor.guild_id)
        definitions={}
        for profile, fields in FORM_DEFAULTS.items():
            definitions[profile]=[]
            for base in fields:
                prefix=profile+'.'+base['key']+'.'
                definitions[profile].append(dict(base,label=f.get(prefix+'label',''),placeholder=f.get(prefix+'placeholder',''),
                    required=prefix+'required' in f,enabled=prefix+'enabled' in f))
        try:
            await asyncio.to_thread(s.save_settings,actor,dict(cfg,forms=definitions,questions=[q.strip() for q in str(f.get('questions','')).splitlines() if q.strip()]))
        except ValueError as exc:
            request.state.scout_error=str(exc)
            request.state.scout_config=dict(cfg,forms=definitions)
            response=await listing(request,'forms')
            response.status_code=400
            return response
        audit(request,'forms.saved')
        return redirect(request,'/scouts/section/forms')

    @app.post('/scouts/settings', name='scouts_save_settings')
    async def save_settings(request: Request):
        s, actor = await setup(request, 'settings.manage')
        f = await form(request)
        values = dict(f)
        for key, default in DEFAULTS.items():
            if isinstance(default, list):
                values[key] = f.getlist(key) if key != 'questions' else [v.strip() for v in str(f.get('questions','')).splitlines() if v.strip()]
        try:
            await asyncio.to_thread(initialize_discord_metadata_schema)
            old = await asyncio.to_thread(s.settings, actor.guild_id)
            for table, keys in [('roles',('scout_roles','report_roles','escalation_roles')),('channels',('activity_channel','moderation_channel','monitored_channels','excluded_channels')),('categories',('monitored_categories','excluded_categories'))]:
                known = {r['id'] for r in await asyncio.to_thread(s.rows, 'SELECT id FROM dashboard_discord_' + table + ' WHERE guild_id=?', (actor.guild_id,))}
                for key in keys:
                    selected = values.get(key,[]) if isinstance(DEFAULTS[key],list) else [values.get(key,'')]
                    previous = old[key] if isinstance(old[key],list) else [old[key]]
                    if set(selected) - known - set(previous) - {''}:
                        raise ValueError('Select roles and channels from this server’s named pickers.')
            await asyncio.to_thread(s.save_settings, actor, values)
        except ValueError as exc:
            request.state.scout_error = str(exc)
            shown = dict(DEFAULTS, **values)
            for key, default in DEFAULTS.items():
                if isinstance(default,bool):
                    shown[key] = values.get(key) in ('on','true','1',True)
            request.state.scout_config = shown
            response = await listing(request, 'settings')
            response.status_code = 400
            return response
        audit(request, 'settings.saved')
        return redirect(request, '/scouts/section/settings')

    @app.get('/scouts/records/{rid}', name='scouts_detail')
    async def detail(request: Request, rid: str):
        s, actor = await setup(request)
        try:
            r = await asyncio.to_thread(s.record, actor, rid)
            if rid != r['short_id']:
                return RedirectResponse('/scouts/records/' + r['short_id'], status_code=303)
            protected = await asyncio.to_thread(s.reporter, actor, rid) if r['kind'] == 'report' and 'scouts.reports.identity.view' in actor.permissions else None
            answers = None
            if r['kind'] == 'checkin' and ('scouts.checkins.view_all' in actor.permissions or (r['data']['recipient_id'] == actor.user_id and 'scouts.checkins.respond' in actor.permissions)):
                answers = await asyncio.to_thread(s.answers, actor, rid)
            revisions = await asyncio.to_thread(s.revisions, actor, rid) if 'scouts.activity.review' in actor.permissions else []
            for revision in revisions:
                snapshot = json.loads(revision['data'])
                content = dict(snapshot.get('data', {}))
                if content.get('related'):
                    content['related'] = '#' + r.get('related_short_id', '')
                revision['display_data'] = dict(status=status_label(snapshot), content=content)
            candidates = r['data'].get('audit_candidates', [])
            r['audit_candidate_refs'] = []
            for candidate in candidates:
                found = await asyncio.to_thread(s.rows, 'SELECT short_id FROM scout_records WHERE guild_id=? AND id=?', (actor.guild_id, candidate))
                if found:
                    r['audit_candidate_refs'].append(found[0]['short_id'])
            linked = await asyncio.to_thread(s.rows, "SELECT id,short_id,kind,status,created_at FROM scout_records WHERE guild_id=? AND json_extract(data,'$.related')=? ORDER BY created_at", (actor.guild_id, r['id']))
            can_update_status=s.can_change_status(actor,r)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        await asyncio.to_thread(initialize_discord_metadata_schema)
        cid=r['data'].get('channel') or r['data'].get('channel_id') or r['data'].get('message',{}).get('channel_id')
        channels=await asyncio.to_thread(s.rows,'SELECT name FROM dashboard_discord_channels WHERE guild_id=? AND id=?',(actor.guild_id,cid))
        return render(request, actor, section='detail', config=await asyncio.to_thread(s.settings, actor.guild_id), detail=r, protected=protected, answers=answers, revisions=revisions, linked=linked, can_update_status=can_update_status, channel_label='#'+channels[0]['name'] if channels else '', message=request.session.pop('scout_message',None), error=getattr(request.state,'scout_error',None), edit_draft=getattr(request.state,'scout_draft',{}))

    @app.post('/scouts/activity', name='scouts_create_activity')
    async def create_activity(request: Request):
        s, actor = await setup(request, 'activity.create')
        f = await form(request)
        try:
            r = await asyncio.to_thread(s.manual, actor, dict(f), str(f.get('related','')), 'dashboard:' + str(f.get('submission','')))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            request.state.scout_error = str(exc)
            request.state.scout_draft = dict(f)
            response = await compose(request, 'activity')
            response.status_code = 400
            return response
        audit(request, 'activity.created', r['short_id'])
        return redirect(request, '/scouts/records/' + r['short_id'],f"Activity #{r['short_id']} saved. Delivery to the staff activity channel is queued.")

    @app.post('/scouts/records/{rid}/action', name='scouts_record_action')
    async def record_action(request: Request, rid: str):
        s, actor = await setup(request)
        f = await form(request)
        action = str(f.get('action_type',''))
        try:
            if action == 'edit':
                r = await asyncio.to_thread(s.edit, actor, rid, dict(f), int(f.get('version',0)))
            elif action == 'status':
                r = await asyncio.to_thread(s.change_status,actor,rid,str(f.get('status','')),int(f.get('version',0)),f.get('note',''))
            elif action == 'void':
                r = await asyncio.to_thread(s.void, actor, rid, f.get('reason'))
            elif action == 'handle':
                r = await asyncio.to_thread(s.handle, actor, rid, f.get('reason') or f.get('outcome'),dict(f))
            elif action == 'escalate':
                r = await asyncio.to_thread(s.escalate, actor, f.get('reason'), rid, 'dashboard:' + str(f.get('submission','')),dict(f))
            elif action in ('cancel','nudge'):
                r = await asyncio.to_thread(s.checkin_action, actor, rid, action)
            elif action == 'answer':
                r = await asyncio.to_thread(s.answer, actor, rid, f.getlist('answers'))
            else:
                raise ValueError('Unknown action.')
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            request.state.scout_error = str(exc)
            request.state.scout_draft = dict(f)
            response = await detail(request, rid)
            response.status_code = 400
            return response
        audit(request, action, r['short_id'])
        return redirect(request, '/scouts/records/' + r['short_id'],
                        'Status saved: '+status_label(r)+'.' if action=='status' else 'Saved.')

    @app.post('/scouts/escalate', name='scouts_escalate')
    async def escalate(request: Request):
        s, actor = await setup(request, 'activity.create')
        f = await form(request)
        try:
            r = await asyncio.to_thread(s.escalate, actor, f.get('reason'), str(f.get('related','')), 'dashboard:' + str(f.get('submission','')),dict(f))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except ValueError as exc:
            request.state.scout_error = str(exc)
            request.state.scout_draft = dict(f)
            response = await compose(request, 'escalation')
            response.status_code = 400
            return response
        audit(request, 'escalate', r['short_id'])
        return redirect(request, '/scouts/records/' + r['short_id'],f"Help request #{r['short_id']} saved. Notification to higher staff is queued.")

    @app.post('/scouts/checkins/preview', name='scouts_checkin_preview')
    async def checkin_preview(request: Request):
        s, actor = await setup(request, 'checkins.request')
        f = await form(request)
        cfg = await asyncio.to_thread(s.settings, actor.guild_id)
        # Current roster comes from the bot; every selected member is fetched again on send.
        runtime = await asyncio.to_thread(s.rows, 'SELECT * FROM scout_runtime WHERE guild_id=? AND heartbeat>?', (actor.guild_id,time.time()-90))
        if not runtime:
            raise HTTPException(400, 'The bot must be online to preview current Scouts.')
        roster = json.loads(runtime[0]['data']).get('scouts', [])
        requested = f.getlist('recipient_ids') or str(f.get('recipients','')).replace(',', ' ').split()
        selected = roster if requested == ['all'] else [r for r in roster if r['id'] in requested]
        if not selected or (requested != ['all'] and set(requested) != {r['id'] for r in selected}):
            raise HTTPException(400, 'Select current Scout IDs, or enter all.')
        questions = [q.strip() for q in str(f.get('questions','')).splitlines() if q.strip()] or cfg['questions']
        if len(questions)>3 or any(len(q)>200 for q in questions):
            raise HTTPException(400, 'Provide one to three questions, at most 200 characters each.')
        deadline = str(f.get('deadline','')).strip()
        try:
            deadline = datetime.fromisoformat(deadline).replace(tzinfo=timezone.utc).timestamp() if deadline else None
            if deadline and deadline<=time.time():
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Enter a future deadline in UTC.') from None
        token = secrets.token_urlsafe(24)
        data = dict(ids=[r['id'] for r in selected], questions=questions, deadline=deadline)
        def save_preview():
            with s.connect() as c:
                c.execute('DELETE FROM scout_previews WHERE created_at<?', (time.time()-600,))
                c.execute('INSERT INTO scout_previews VALUES(?,?,?,?,?)', (token,actor.guild_id,actor.user_id,json.dumps(data),time.time()))
        await asyncio.to_thread(save_preview)
        return render(request, actor, section='preview', config=cfg, recipients=selected, questions=questions, deadline=deadline, token=token, detail=None, message=None, error=None)

    @app.post('/scouts/checkins/send', name='scouts_checkin_send')
    async def checkin_send(request: Request):
        s, actor = await setup(request, 'checkins.request')
        f = await form(request)
        token = str(f.get('token',''))
        saved = await asyncio.to_thread(s.rows, 'SELECT data FROM scout_previews WHERE token=? AND guild_id=? AND actor_id=? AND created_at>?', (token,actor.guild_id,actor.user_id,time.time()-600))
        if not saved:
            raise HTTPException(400, 'Preview the current recipients again.')
        data = json.loads(saved[0]['data'])
        cfg = await asyncio.to_thread(s.settings, actor.guild_id)
        try:
            for uid in data['ids']:
                member = await fetch_member(actor.guild_id, uid)
                recipient = await asyncio.to_thread(member_actor, s, actor.guild_id, uid, member['roles'], pending=member.get('pending',False), bot=member.get('user',{}).get('bot',False))
                recipient.require('checkins.respond')
                if member.get('pending') or member.get('user',{}).get('bot') or not set(member['roles']) & set(cfg['scout_roles']):
                    raise ValueError('A selected member is no longer an eligible Scout. Preview again.')
            await asyncio.to_thread(s.request_checkins, actor, data['ids'], data['questions'], data['deadline'], token)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from None
        audit(request, 'checkins.requested')
        return redirect(request, '/scouts/section/checkins')

    @app.post('/scouts/delivery/{job_id}/retry', name='scouts_retry')
    async def retry(request: Request, job_id: str):
        s, actor = await setup(request, 'settings.manage')
        await form(request)
        await asyncio.to_thread(s.retry_delivery, actor, job_id)
        audit(request, 'delivery.retry', job_id)
        return redirect(request, '/scouts/section/settings')

    @app.post('/scouts/ai', name='scouts_ai')
    async def ai(request: Request):
        s, actor = await setup(request, 'ai.use')
        f = await form(request)
        draft = str(f.get('draft',''))
        ai_success = False
        try:
            result = await preview(s, actor, str(f.get('mode','draft')), draft=draft, scout=str(f.get('scout','')), days=int(f.get('days',30)))
            _, fresh_actor = await setup(request, 'ai.use')
            content_caps = {'scouts.activity.view','scouts.activity.create','scouts.moderation.view','scouts.checkins.respond','scouts.checkins.view_all'}
            if (actor.permissions & content_caps) - fresh_actor.permissions:
                raise PermissionError('Permissions changed during this preview. Reload and request a fresh preview.')
            actor = fresh_actor
            ai_success = True
        except (ValueError, PermissionError) as exc:
            result = str(exc)
        except Exception:
            # Provider/storage failures must not discard a staff member's draft.
            # Do not log exception payloads, which may contain private input.
            result = 'Gemini could not complete this preview. Your original draft is preserved below.'
        return render(request, actor, section='ai', config=await asyncio.to_thread(s.settings, actor.guild_id), detail=None, ai_result=result, draft=draft, ai_success=ai_success, submission=secrets.token_hex(16), message=None, error=None)

    templates.env.globals['scout_submission'] = lambda: secrets.token_hex(16)
