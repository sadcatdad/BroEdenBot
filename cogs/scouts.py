"""Discord Scout Operations: evidence listeners, private forms and durable outbox.

No moderation is executed here. Audit records name only the actor Discord supplies;
gateway deletion evidence remains separate when exact attribution is impossible.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import time
from typing import Optional
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.scouts import Scouts, ACTIVITY_TYPES, REPORT_CATEGORIES, encode
from utils.scout_access import discord_member_actor
from utils.settings import get_setting

log = logging.getLogger(__name__)


def dashboard_url(rid=''):
    base = str(get_setting('DASHBOARD_PUBLIC_URL', '') or os.getenv('DASHBOARD_PUBLIC_URL', '')).rstrip('/')
    return base + '/scouts' + ('/records/' + rid if rid else '') if base.startswith('https://') else ''


def snapshot(message):
    return dict(message_id=str(message.id), channel_id=str(message.channel.id), author_id=str(message.author.id),
                content=message.content, created_at=message.created_at.timestamp(),
                edited_at=message.edited_at.timestamp() if message.edited_at else None,
                jump_url=message.jump_url,
                attachments=[dict(id=str(a.id), filename=a.filename, url=a.url, proxy_url=a.proxy_url) for a in message.attachments])


def safe(text, limit=3900):
    # All content is untrusted: suppress pings and escape display formatting.
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))[:limit]


def card_embed(record, marker):
    d = record['data']
    title = {'manual':'Scout activity','report':'Member report','system':'Automatic moderation evidence', 'escalation':'Escalated', 'checkin':'Requested Scout check-in'}[record['kind']]
    lines = [f"**Status:** {record['status'].title()}", f"**Record:** {record['id']}"]
    if record['kind'] == 'report':
        m = d['message']
        lines += ['**Category:** ' + safe(d['category']), '**Reported member:** ' + m.get('author_id','Unknown'),
                  '**Channel:** ' + m['channel_id'], safe(m.get('content') or 'Content unavailable', 2200),
                  m.get('jump_url','')]
    elif record['kind'] == 'checkin':
        lines += ['You have been asked for a check-in. Respond using the button below. Answers are available only in The Garden.',
                  '**Recipient:** ' + d['recipient_id']]
        lines.append('**Questions:**\n' + '\n'.join(f'{i+1}. {safe(q,200)}' for i,q in enumerate(d['questions'])))
        if d.get('deadline'):
            lines.append(f"**Deadline:** <t:{int(d['deadline'])}:F>")
        if record['status'] != 'pending':
            lines.append('This request is closed.')
    elif record['kind'] == 'system':
        lines += ['**Action:** ' + d['action_type'], '**Attribution:** ' + safe(d.get('attribution','unavailable'),300)]
        if d.get('moderator_id'):
            lines.append('**Audit actor:** ' + d['moderator_id'])
        if d.get('target_id'):
            lines.append('**Target:** ' + d['target_id'])
        if d.get('channel_id'):
            lines.append('**Channel:** ' + d['channel_id'])
        lines.append('**Reason:** ' + safe(d.get('reason') or 'None supplied',400))
        for m in d.get('messages', [])[:5]:
            lines.append('**Deleted message ' + m['message_id'] + ':** ' + safe(m.get('content') if m.get('content') is not None else 'Content unavailable',450))
        if len(d.get('messages',[]))>5:
            lines.append('Additional messages and full evidence are in The Garden.')
        if d.get('count'):
            lines.append('**Grouped audit count:** ' + str(d['count']))
    else:
        lines += ['**Scout:** ' + record['author_id'], safe(d.get('summary',''),2000)]
        if d.get('action'):
            lines.append('**Action taken:** ' + safe(d['action'],800))
        if d.get('related'):
            lines.append('**Related record:** ' + d['related'])
        if record['status']=='void':
            lines.append('**Void reason:** ' + safe(d.get('void_reason',''),400))
    if d.get('outcome'):
        lines.append('**Outcome:** ' + safe(d['outcome'],450))
    embed = discord.Embed(title=title, description='\n\n'.join(lines)[:4096], color=0xf0319b,
                          timestamp=datetime.fromtimestamp(record['created_at'], timezone.utc))
    embed.set_footer(text=marker)
    return embed


class OpenForm(discord.ui.View):
    def __init__(self, uid, factory):
        super().__init__(timeout=300)
        self.uid, self.factory = uid, factory

    @discord.ui.button(label='Open form', style=discord.ButtonStyle.primary)
    async def open(self, interaction, button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This form belongs to another member.', ephemeral=True)
            return
        await interaction.response.send_modal(self.factory())


class ScoutModal(discord.ui.Modal):
    def __init__(self, cog, guild, mode, *, rid='', values=None, related='', source='', questions=None):
        super().__init__(title={'manual':'Log Scout Activity','edit':'Edit Scout Activity','escalate':'Escalate to higher staff','handle':'Mark report handled','answer':'Respond to Check-in'}[mode], timeout=600)
        self.cog, self.guild, self.mode, self.rid = cog, str(guild), mode, rid
        self.values, self.related, self.source = values or {}, related, source
        fields = [('summary','What happened',4000),('action','Action taken',4000),('outcome','Outcome (optional)',1000),('evidence','Evidence links (optional)',1000)] if mode in ('manual','edit') else [('reason','Help needed' if mode=='escalate' else 'Outcome',4000)]
        if mode=='answer':
            fields = [('answer'+str(i), q[:45], 4000) for i,q in enumerate(questions or [])]
        self.inputs = {}
        for key, label, limit in fields:
            field = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, required=key not in ('outcome','evidence'), max_length=limit,
                                         default=self.values.get(key) or None, placeholder=(questions[int(key[6:])][:100] if key.startswith('answer') else None))
            self.inputs[key] = field
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            actor = await self.cog.actor(interaction.user.id, self.guild)
            values = dict(self.values, **{key: str(field.value) for key,field in self.inputs.items()})
            if self.mode=='manual':
                r = await self.cog.call(self.cog.service.manual, actor, values, self.related, self.source or str(interaction.id))
            elif self.mode=='edit':
                r = await self.cog.call(self.cog.service.edit, actor, self.rid, values, self.values['version'])
            elif self.mode=='escalate':
                r = await self.cog.call(self.cog.service.escalate, actor, values['reason'], self.related or self.rid, self.source or str(interaction.id))
            elif self.mode=='handle':
                r = await self.cog.call(self.cog.service.handle, actor, self.rid, values['reason'])
            else:
                r = await self.cog.call(self.cog.service.answer, actor, self.rid, [f.value for f in self.inputs.values()])
            message = ('Response saved. Your answers are available only in The Garden.' if self.mode=='answer' else f"Saved #{r['id']}. Delivery is queued; no need to resubmit.")
            await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            log.exception('Scout form failed mode=%s record=%s', self.mode, self.rid)
            await interaction.followup.send('Could not confirm this save. Check The Garden before resubmitting.', ephemeral=True)


class ReportModal(discord.ui.Modal, title='Report to Staff'):
    explanation = discord.ui.TextInput(label='Why are you reporting this message?', style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, cog, message, category, reporter, source):
        super().__init__(timeout=600)
        self.cog, self.message, self.category, self.reporter, self.source = cog, message, category, reporter, source
        self.target_snapshot = snapshot(message)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id != self.reporter:
                raise PermissionError('This report form belongs to another member.')
            await self.cog.report_member(interaction.user.id, self.message.channel)
            r = await self.cog.call(self.cog.service.report, self.message.guild.id, interaction.user.id,
                                    self.target_snapshot, self.category, self.explanation.value, self.source)
            await interaction.followup.send(f"Report #{r['id']} received. Staff authorized to view protected submissions can read your identity and explanation in The Garden.", ephemeral=True)
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            # Never include the form, reporter or explanation in diagnostic logs.
            log.error('Member-report storage or delivery preparation failed')
            await interaction.followup.send('Could not confirm receipt. Please try again; duplicates are prevented.', ephemeral=True)


class ReportCategory(discord.ui.Select):
    def __init__(self, cog, message, uid, source):
        super().__init__(placeholder='Choose a report category', options=[discord.SelectOption(label=c) for c in REPORT_CATEGORIES])
        self.cog, self.message, self.uid, self.source = cog, message, uid, source

    async def callback(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This report belongs to another member.', ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self.cog, self.message, self.values[0], self.uid, self.source))


class ScoutControl(discord.ui.DynamicItem[discord.ui.Button], template=r'scouts:(?P<action>add|escalate|handle|answer):(?P<rid>[a-f0-9]{16}):(?P<guild>[0-9]+)'):
    def __init__(self, action, rid, guild):
        self.action, self.rid, self.guild = action, rid, str(guild)
        super().__init__(discord.ui.Button(label={'add':'Add Activity','escalate':'Escalate','handle':'Mark Handled','answer':'Respond to Check-in'}[action],
                                          custom_id=f'scouts:{action}:{rid}:{guild}', style=discord.ButtonStyle.primary if action=='answer' else discord.ButtonStyle.secondary))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['action'], match['rid'], match['guild'])

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        cog = interaction.client.get_cog('ScoutsCog')
        if not cog:
            await interaction.followup.send('Scout Operations is unavailable.', ephemeral=True)
            return
        try:
            if interaction.guild_id and str(interaction.guild_id) != self.guild:
                raise PermissionError('This control belongs to another server.')
            actor = await cog.actor(interaction.user.id, self.guild)
            r = await cog.call(cog.service.record, actor, self.rid)
            mode = {'add':'manual','escalate':'escalate','handle':'handle','answer':'answer'}[self.action]
            actor.require('checkins.respond' if mode=='answer' else 'reports.handle' if mode=='handle' else 'activity.create')
            if mode=='answer' and (r['kind']!='checkin' or r['data']['recipient_id']!=actor.user_id or r['status']!='pending'):
                raise PermissionError('This check-in is not open for you.')
            if mode=='handle' and (r['kind']!='report' or r['status']!='new'):
                raise ValueError('This report is already handled or unavailable.')
            factory = lambda: ScoutModal(cog, self.guild, mode, rid=self.rid, related=self.rid if mode in ('manual','escalate') else '', questions=r['data'].get('questions'))
            await interaction.followup.send('Open the form to continue.', ephemeral=True, view=OpenForm(interaction.user.id, factory))
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)


def record_view(record):
    view = discord.ui.View(timeout=None)
    if record['kind']=='checkin':
        if record['status']=='pending':
            view.add_item(ScoutControl('answer',record['id'],record['guild_id']))
    else:
        for action in ('add','escalate'):
            view.add_item(ScoutControl(action,record['id'],record['guild_id']))
        if record['kind']=='report' and record['status']=='new':
            view.add_item(ScoutControl('handle',record['id'],record['guild_id']))
    if url := dashboard_url(record['id']):
        view.add_item(discord.ui.Button(label='View Dashboard', url=url))
    return view


class ActivityPages(discord.ui.View):
    def __init__(self, cog, guild, uid, scout, days):
        super().__init__(timeout=300)
        self.cog, self.guild, self.uid, self.scout, self.days = cog, guild, uid, scout, days
        self.page = 1

    async def content(self):
        actor = await self.cog.actor(self.uid, self.guild)
        rows = await self.cog.call(self.cog.service.listing, actor, 'manual', page=self.page, days=self.days, scout=self.scout)
        self.next_page.disabled = len(rows)<=25
        self.previous.disabled = self.page<=1
        # Count manual originals only; linked follow-ups do not double count system actions.
        totals = await self.cog.call(self.cog.service.rows,
            "SELECT count(*) total FROM scout_records WHERE guild_id=? AND kind='manual' AND status!='void' AND author_id=? AND created_at>=? AND coalesce(json_extract(data,'$.related'),'')=''",
            (str(self.guild),str(self.scout), time.time()-self.days*86400))
        system_total = ''
        if 'scouts.moderation.view' in actor.permissions:
            confirmed = await self.cog.call(self.cog.service.rows,
                "SELECT coalesce(sum(coalesce(json_extract(data,'$.count'),1)),0) total FROM scout_records WHERE guild_id=? AND kind='system' AND source_key LIKE 'audit:%' AND author_id=? AND json_extract(data,'$.confirmed_scout')=1 AND created_at>=?",
                (str(self.guild),str(self.scout),time.time()-self.days*86400))
            system_total = f" {confirmed[0]['total']} confirmed audit actions (grouped counts; exact affected members may be unknown)."
        return f"Scout {self.scout} · {self.days} days · {totals[0]['total']} standalone manual records.{system_total} Linked context is listed separately; member reports are not Scout activity.\nPage {self.page}\n" + '\n'.join(f"#{r['id']} · {r['status']} · {safe(r['data']['summary'],20)}" for r in rows[:25])

    async def turn(self, interaction, amount):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This history belongs to another member.', ephemeral=True)
            return
        await interaction.response.defer()
        self.page = max(1,self.page+amount)
        try:
            await interaction.edit_original_response(content=(await self.content())[:1990], view=self)
        except (ValueError,PermissionError) as exc:
            await interaction.edit_original_response(content=str(exc), view=None)

    @discord.ui.button(label='Previous')
    async def previous(self, interaction, button):
        await self.turn(interaction,-1)

    @discord.ui.button(label='Next')
    async def next_page(self, interaction, button):
        await self.turn(interaction,1)


class BulkConfirm(discord.ui.View):
    def __init__(self, cog, actor, recipients, questions, deadline, source):
        super().__init__(timeout=300)
        self.cog, self.actor, self.recipients, self.questions, self.deadline, self.source = cog, actor, recipients, questions, deadline, source

    @discord.ui.button(label='Send these check-ins', style=discord.ButtonStyle.primary)
    async def confirm(self, interaction, button):
        if str(interaction.user.id)!=self.actor.user_id:
            await interaction.response.send_message('This preview belongs to another member.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            actor = await self.cog.actor(interaction.user.id, self.actor.guild_id)
            actor.require('checkins.request')
            for uid in self.recipients:
                recipient = await self.cog.actor(uid,self.actor.guild_id)
                recipient.require('checkins.respond')
            rows = await self.cog.call(self.cog.service.request_checkins, actor, self.recipients, self.questions, self.deadline, self.source)
            await interaction.edit_original_response(content=f'Queued {len(rows)} check-ins. Individual delivery status is in The Garden.',view=None)
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)


class ScoutsCog(commands.Cog):
    scout = app_commands.Group(name='scout',description='Scout activity, escalation and check-ins',guild_only=True)
    checkin = app_commands.Group(name='checkin',description='Requested private check-ins',parent=scout)

    def __init__(self, bot):
        self.bot, self.service = bot, Scouts()
        self.report_menu = app_commands.ContextMenu(name='Report to Staff', callback=self.report_message, allowed_contexts=app_commands.AppCommandContext(guild=True,dm_channel=False,private_channel=False), allowed_installs=app_commands.AppInstallationType(guild=True,user=False))
        self.log_menu = app_commands.ContextMenu(name='Log Scout Activity', callback=self.log_message, allowed_contexts=app_commands.AppCommandContext(guild=True,dm_channel=False,private_channel=False), allowed_installs=app_commands.AppInstallationType(guild=True,user=False))
        self.audit_ticks = {}
        self._recovered = False

    @staticmethod
    async def call(fn,*args,**kwargs):
        return await asyncio.to_thread(fn,*args,**kwargs)

    async def cog_load(self):
        await self.call(self.service.initialize)
        self.bot.tree.add_command(self.report_menu)
        self.bot.tree.add_command(self.log_menu)
        self.bot.add_dynamic_items(ScoutControl)
        self.worker.start()

    def cog_unload(self):
        self.worker.cancel()
        self.bot.tree.remove_command(self.report_menu.name,type=discord.AppCommandType.message)
        self.bot.tree.remove_command(self.log_menu.name,type=discord.AppCommandType.message)
        self.bot.remove_dynamic_items(ScoutControl)

    async def actor(self, uid, guild_id):
        configured = str(get_setting('GUILD_ID','') or os.getenv('GUILD_ID','')).strip()
        if configured and str(guild_id)!=configured:
            raise PermissionError('Scout Operations is not configured for this server.')
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            raise PermissionError('Server membership is unavailable.')
        try:
            member = await guild.fetch_member(int(uid))
        except discord.HTTPException:
            raise PermissionError('Current server membership could not be verified.') from None
        return await discord_member_actor(self.service, guild_id, member)

    async def report_member(self, uid, channel):
        # Reporting requires membership and message visibility, never staff/login access.
        if not getattr(channel,'guild',None):
            raise PermissionError('Only server messages can be reported.')
        actor = await self.actor(uid,channel.guild.id)
        if not actor.eligible:
            raise PermissionError('This account cannot submit reports.')
        member = await channel.guild.fetch_member(int(uid))
        perms = channel.permissions_for(member)
        if not perms.view_channel or not perms.read_message_history:
            raise PermissionError('You no longer have access to this message channel.')
        if isinstance(channel,discord.Thread) and channel.is_private() and not perms.manage_threads:
            try:
                await channel.fetch_member(int(uid))
            except discord.HTTPException:
                raise PermissionError('You no longer have access to this private thread.') from None
        await self.call(self.service.enabled,channel.guild.id)

    async def report_message(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer(ephemeral=True)
        try:
            if not message.guild or not interaction.guild_id or interaction.guild_id != message.guild.id:
                raise PermissionError('Choose a message in this server.')
            await self.report_member(interaction.user.id,message.channel)
            view = discord.ui.View(timeout=600)
            view.add_item(ReportCategory(self,message,interaction.user.id,str(interaction.id)))
            await interaction.followup.send('Choose the category for this specific message. Only that message is captured. Your identity and explanation are restricted to authorized dashboard readers.',ephemeral=True,view=view)
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    async def log_message(self, interaction: discord.Interaction, message: discord.Message):
        await interaction.response.defer(ephemeral=True)
        try:
            actor = await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            await self.call(self.service.enabled,actor.guild_id)
            await self.report_member(interaction.user.id,message.channel)
            values=dict(type='other', summary=message.content[:4000] or 'Message context', evidence=message.jump_url,members=str(message.author.id),channel=str(message.channel.id))
            await interaction.followup.send('Log activity with the selected message as context.',ephemeral=True,view=OpenForm(interaction.user.id,lambda: ScoutModal(self,interaction.guild_id,'manual',values=values,source=str(interaction.id))))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @scout.command(name='log',description='Write a structured Scout activity record')
    @app_commands.choices(activity_type=[app_commands.Choice(name=t,value=t) for t in ACTIVITY_TYPES])
    async def log_activity(self,interaction:discord.Interaction,activity_type:str='other',members:str='',channel:Optional[discord.TextChannel]=None,incident_time:str='',related_record:str=''):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            await self.call(self.service.enabled,actor.guild_id)
            values=dict(type=activity_type,members=members,channel=str(channel.id) if channel else '',incident_at=incident_time)
            await interaction.followup.send('Open the activity form. Automatically captured actions only need a follow-up when you have context to add.',ephemeral=True,view=OpenForm(interaction.user.id,lambda:ScoutModal(self,interaction.guild_id,'manual',values=values,related=related_record,source=str(interaction.id))))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @scout.command(name='edit',description='Revise an owned manual activity record')
    async def edit_activity(self,interaction:discord.Interaction,record:str):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            r=await self.call(self.service.record,actor,record)
            if r['kind']!='manual' or r['author_id']!=actor.user_id or r['status']=='void':
                raise PermissionError('Only the author can edit an active manual record.')
            values=dict(r['data'],version=r['version'])
            await interaction.followup.send('Open your activity revision.',ephemeral=True,view=OpenForm(interaction.user.id,lambda:ScoutModal(self,interaction.guild_id,'edit',rid=record,values=values)))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @scout.command(name='activity',description='Privately review Scout activity and totals')
    async def activity(self,interaction:discord.Interaction,scout:Optional[discord.Member]=None,days:app_commands.Range[int,1,3650]=30):
        await interaction.response.defer(ephemeral=True)
        try:
            view=ActivityPages(self,interaction.guild_id,interaction.user.id,(scout or interaction.user).id,days)
            await interaction.followup.send((await view.content())[:1990],ephemeral=True,view=view,allowed_mentions=discord.AllowedMentions.none())
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @scout.command(name='escalate',description='Ask higher staff for help on a concern or record')
    async def escalate(self,interaction:discord.Interaction,record:str=''):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            await self.call(self.service.enabled,actor.guild_id)
            await interaction.followup.send('Explain the help needed.',ephemeral=True,view=OpenForm(interaction.user.id,lambda:ScoutModal(self,interaction.guild_id,'escalate',related=record,source=str(interaction.id))))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @checkin.command(name='request',description='Preview and request check-ins for one, selected, or all Scouts')
    async def request_checkin(self,interaction:discord.Interaction,recipients:str,deadline:str='',questions:str=''):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('checkins.request')
            await self.call(self.service.enabled,actor.guild_id)
            cfg=await self.call(self.service.settings,actor.guild_id)
            guild=interaction.guild
            roster=[m async for m in guild.fetch_members(limit=None) if not m.bot and not m.pending and {str(r.id) for r in m.roles}&set(cfg['scout_roles'])]
            ids=recipients.replace(',',' ').split()
            selected=roster if ids==['all'] else [m for m in roster if str(m.id) in ids]
            if not selected or (ids!=['all'] and {str(m.id) for m in selected}!=set(ids)) or len(selected)>500:
                raise ValueError('Choose 1–500 current Scout IDs separated by spaces, or all.')
            deadline_at=datetime.fromisoformat(deadline.replace('Z','+00:00')).timestamp() if deadline else None
            if deadline and datetime.fromisoformat(deadline.replace('Z','+00:00')).tzinfo is None:
                raise ValueError('Include a timezone in the ISO deadline, for example 2026-11-15T18:00:00-06:00.')
            qs=[q.strip() for q in questions.split('|') if q.strip()] or cfg['questions']
            if not 1<=len(qs)<=3 or any(len(q)>200 for q in qs):
                raise ValueError('Provide up to three questions separated by |, at most 200 characters each.')
            view=BulkConfirm(self,actor,[str(m.id) for m in selected],qs,deadline_at,str(interaction.id))
            names=', '.join(safe(m.display_name,50) for m in selected)
            recipient_file = discord.File(io.BytesIO('\n'.join(f'{m.display_name} ({m.id})' for m in selected).encode()),filename='check-in-recipients.txt')
            question_text = '\n'.join(qs)
            await interaction.followup.send(f'Preview: {len(selected)} Scouts: {names[:700]}. Complete recipient list attached.\n\nQuestions:\n{question_text}\n\nDeadline: {deadline or "None"}. Confirm to queue their DMs.',ephemeral=True,view=view,file=recipient_file)
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self,message):
        if message.guild:
            await self.call(self.service.buffer,message.guild.id,snapshot(message),str(message.channel.category_id or ''),str(getattr(message.channel,'parent_id','') or ''))

    @commands.Cog.listener()
    async def on_raw_message_edit(self,payload):
        if not payload.guild_id:
            return
        guild=self.bot.get_guild(payload.guild_id)
        channel=guild.get_channel_or_thread(payload.channel_id) if guild else None
        if not channel or not await self.call(self.service.capture_allowed,payload.guild_id,payload.channel_id,str(channel.category_id or ''),str(getattr(channel,'parent_id','') or '')):
            return
        # Partial raw updates may omit content. Fetch the current message instead of erasing evidence.
        try:
            message=await channel.fetch_message(payload.message_id)
            await self.call(self.service.buffer,guild.id,snapshot(message),str(channel.category_id or ''),str(getattr(channel,'parent_id','') or ''))
        except discord.HTTPException:
            pass

    async def deleted(self,guild_id,channel_id,message_ids,bulk=False):
        guild=self.bot.get_guild(guild_id) if guild_id else None
        channel=guild.get_channel_or_thread(channel_id) if guild else None
        if not channel or not await self.call(self.service.capture_allowed,guild_id,channel_id,str(channel.category_id or ''),str(getattr(channel,'parent_id','') or '')):
            return
        ids=sorted(str(m) for m in message_ids)
        key='gateway:'+('bulk:' if bulk else 'delete:')+hashlib.sha256(','.join(ids).encode()).hexdigest()
        await self.call(self.service.system,guild_id,key,dict(action_type='message_bulk_delete' if bulk else 'message_delete',event_at=time.time(),channel_id=str(channel_id),message_ids=ids,attribution='unattributed: deletion events do not identify an actor'))

    @commands.Cog.listener()
    async def on_raw_message_delete(self,payload):
        await self.deleted(payload.guild_id,payload.channel_id,[payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self,payload):
        await self.deleted(payload.guild_id,payload.channel_id,payload.message_ids,True)

    @commands.Cog.listener()
    async def on_member_update(self,before,after):
        old,new=before.timed_out_until,after.timed_out_until
        if old==new:
            return
        cfg=await self.call(self.service.settings,after.guild.id)
        if not cfg['enabled'] or not cfg['automatic_enabled'] or 'timeout' not in cfg['action_types']:
            return
        old_at=old.timestamp() if old else None
        new_at=new.timestamp() if new else None
        expired=bool(old_at and old_at<=time.time() and not new_at)
        key=f'gateway:timeout:{after.id}:{old_at}:{new_at}'
        await self.call(self.service.system,after.guild.id,key,dict(action_type='timeout',event_at=time.time(),target_id=str(after.id),before=old_at,after=new_at,attribution='automatic timeout expiry' if expired else 'unattributed: see audit evidence',timeout_change='expiry' if expired else 'removed' if not new else 'changed' if old else 'applied'))

    async def ingest_audit(self,guild,entry):
        mapping={discord.AuditLogAction.message_delete:'message_delete',discord.AuditLogAction.message_bulk_delete:'message_bulk_delete',discord.AuditLogAction.member_move:'member_move',discord.AuditLogAction.member_disconnect:'member_disconnect',discord.AuditLogAction.member_update:'timeout'}
        action=mapping.get(entry.action)
        if not action:
            return
        before=after=None
        if action=='timeout':
            if not hasattr(entry.after,'timed_out_until'):
                return
            old=getattr(entry.before,'timed_out_until',None)
            new=getattr(entry.after,'timed_out_until',None)
            before=old.timestamp() if old else None
            after=new.timestamp() if new else None
        cfg=await self.call(self.service.settings,guild.id)
        if entry.created_at.timestamp()<cfg.get('capture_since',time.time()):
            return
        moderator=entry.user
        roles=[]
        human=False
        if moderator:
            try:
                moderator=await guild.fetch_member(moderator.id)
                roles=sorted(str(r.id) for r in moderator.roles if str(r.id) in cfg['scout_roles'])
                human=not moderator.bot
            except discord.HTTPException:
                pass
        extra=entry.extra
        channel=getattr(extra,'channel',None)
        target=getattr(entry.target,'id',None)
        if action=='message_bulk_delete' and not channel and target:
            channel=guild.get_channel_or_thread(target)
        if action in ('message_delete','message_bulk_delete'):
            resolved=guild.get_channel_or_thread(channel.id) if channel else None
            if not resolved or not await self.call(self.service.capture_allowed,guild.id,resolved.id,str(resolved.category_id or ''),str(getattr(resolved,'parent_id','') or '')):
                return
        data=dict(action_type=action,event_at=entry.created_at.timestamp(),audit_id=str(entry.id),
                  moderator_id=str(entry.user.id) if entry.user else '',scout_roles=roles,role_snapshot_at=time.time(),
                  confirmed_scout=bool(human and roles),reason=entry.reason or '',
                  target_id=str(target) if target else '',channel_id=str(channel.id) if channel else '',
                  count=getattr(extra,'count',1),before=before,after=after,
                  attribution='audit actor confirmed; grouped entry may omit exact affected members/messages' if action!='timeout' else 'audit actor and timeout target confirmed')
        await self.call(self.service.system,guild.id,'audit:'+str(entry.id),data)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self,entry):
        await self.ingest_audit(entry.guild,entry)

    async def audit_poll(self,guild,cfg):
        if not cfg['automatic_enabled'] or not guild.me.guild_permissions.view_audit_log:
            return
        # Poll merged/incrementing entries too: Discord does not emit another gateway event.
        # A persisted checkpoint plus overlap covers delays and restarts, including old merged entries.
        runtime=await self.call(self.service.rows,'SELECT data FROM scout_runtime WHERE guild_id=?',(str(guild.id),))
        state=json.loads(runtime[0]['data']) if runtime else {}
        since=max(cfg.get('capture_since',time.time()),state.get('audit_checkpoint',cfg.get('capture_since',time.time()))-180)
        cutoff=datetime.fromtimestamp(max(since,time.time()-45*86400),timezone.utc)
        try:
            async for entry in guild.audit_logs(limit=None,after=cutoff,oldest_first=True):
                await self.ingest_audit(guild,entry)
            # Refresh old grouped entries seen during this activation; their IDs/timestamps don't change on increment.
            old=await self.call(self.service.rows,"SELECT json_extract(data,'$.audit_id') aid FROM scout_records WHERE guild_id=? AND source_key LIKE 'audit:%' AND kind='system' AND json_extract(data,'$.action_type') IN ('message_delete','message_bulk_delete','member_move','member_disconnect') ORDER BY created_at DESC LIMIT 100",(str(guild.id),))
            if old:
                oldest=min(int(r['aid']) for r in old)
                async for entry in guild.audit_logs(limit=300,after=discord.Object(id=oldest-1)):
                    if any(str(entry.id)==r['aid'] for r in old):
                        await self.ingest_audit(guild,entry)
            await self.call(self.service.reconcile_links,guild.id)
            return time.time()
        except discord.HTTPException:
            return state.get('audit_checkpoint',since)

    async def heartbeat(self,guild,cfg,checkpoint=None):
        me=guild.me
        readiness={}
        for label,cid in [('Shared activity destination',cfg['activity_channel']),('Moderation destination',cfg['moderation_channel'] or cfg['activity_channel'])]:
            channel=guild.get_channel(int(cid)) if cid else None
            perms=channel.permissions_for(me) if channel and me else None
            readiness[label]=bool(perms and perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history)
        readiness.update({'Audit-log access':bool(me and me.guild_permissions.view_audit_log),
                          'Guild moderation subscription':self.bot.intents.moderation,
                          'Message events':self.bot.intents.guild_messages,
                          'Message content capture':self.bot.intents.message_content,
                          'Member events':self.bot.intents.members,'Voice-state events':self.bot.intents.voice_states,
                          'Capture scope selected':bool(cfg['monitored_channels'] or cfg['monitored_categories'])})
        roster=[dict(id=str(m.id),name=m.display_name) for m in guild.members if not m.bot and not m.pending and set(str(r.id) for r in m.roles)&set(cfg['scout_roles'])]
        old=await self.call(self.service.rows,'SELECT data FROM scout_runtime WHERE guild_id=?',(str(guild.id),))
        state=json.loads(old[0]['data']) if old else {}
        state.update(readiness=readiness,scouts=roster)
        if checkpoint:
            state['audit_checkpoint']=checkpoint
        await self.sql('INSERT OR REPLACE INTO scout_runtime VALUES(?,?,?)',(str(guild.id),time.time(),encode(state)))

    async def sql(self,query,args=()):
        def run():
            with self.service.connect() as c:
                return c.execute(query,args).rowcount
        return await self.call(run)

    async def deliver(self,job):
        cfg=await self.call(self.service.settings,job['guild_id'])
        if not cfg['enabled']:
            return
        guild=self.bot.get_guild(int(job['guild_id']))
        if not guild:
            return
        def load():
            with self.service.connect() as c:
                return self.service._record(c,job['guild_id'],job['record_id'])
        r=await self.call(load)
        marker='Scout delivery '+job['id']
        if r['kind']=='checkin':
            sent=await self.call(self.service.rows,"SELECT 1 FROM scout_jobs WHERE record_id=? AND status='sent'",(r['id'],))
            if r['status']!='pending' and not sent and job['status']=='pending':
                in_flight=await self.call(self.service.rows,"SELECT 1 FROM scout_jobs WHERE record_id=? AND status IN ('sending','uncertain')",(r['id'],))
                if in_flight:
                    return
                await self.sql("UPDATE scout_jobs SET status='cancelled',error='Request closed before initial delivery.' WHERE id=?",(job['id'],))
                return
            try:
                recipient=await self.actor(r['data']['recipient_id'],job['guild_id'])
                recipient.require('checkins.respond')
                member=await guild.fetch_member(int(r['data']['recipient_id']))
                channel=await member.create_dm()
            except (PermissionError,discord.Forbidden,discord.NotFound):
                await self.sql("UPDATE scout_jobs SET status='failed',error=? WHERE id=?",('Recipient unavailable, ineligible, or DMs closed.',job['id']))
                return
        else:
            channel=guild.get_channel(int(job['destination'])) if job['destination'] else None
            if not channel:
                await self.sql("UPDATE scout_jobs SET status='failed',error='Destination unavailable.' WHERE id=?",(job['id'],))
                return
        if job['status']=='uncertain':
            await self.sql('UPDATE scout_jobs SET checked_at=? WHERE id=?',(time.time(),job['id']))
            # Search until the attempt boundary. Missing history/permissions stays uncertain.
            try:
                after=datetime.fromtimestamp(job['attempted_at']-5,timezone.utc)
                async for message in channel.history(limit=2000,after=after,oldest_first=True):
                    if message.author.id==self.bot.user.id and any(e.footer.text==marker for e in message.embeds):
                        await self.sql("UPDATE scout_jobs SET status='sent',message_id=?,error='' WHERE id=?",(str(message.id),job['id']))
                        return
            except discord.HTTPException:
                pass
            return
        blocked=await self.call(self.service.rows,"SELECT 1 FROM scout_jobs WHERE record_id=? AND id!=? AND status IN ('sending','uncertain') LIMIT 1",(r['id'],job['id']))
        if blocked:
            return
        claimed=await self.sql("UPDATE scout_jobs SET status='sending',attempted_at=? WHERE id=? AND status='pending'",(time.time(),job['id']))
        if not claimed:
            return
        try:
            # Reuse the original channel card. Each committed revision gets a receipt marker.
            receipts=await self.call(self.service.rows,"SELECT destination,message_id FROM scout_jobs WHERE record_id=? AND status='sent' AND message_id!='' ORDER BY created_at DESC LIMIT 1",(r['id'],))
            old=receipts[0] if receipts else None
            kwargs=dict(embed=card_embed(r,marker),view=record_view(r),allowed_mentions=discord.AllowedMentions.none())
            role_ids=[]
            if not old and r['kind']=='report' and r['data'].get('notify_roles'):
                role_ids=cfg['report_roles']
            if not old and r['kind']=='escalation':
                role_ids=cfg['escalation_roles']
            content=' '.join('<@&'+rid+'>' for rid in role_ids) or None
            if old and old['destination']==job['destination'] and r['kind']!='checkin':
                try:
                    message=await channel.fetch_message(int(old['message_id']))
                except discord.NotFound:
                    message=await channel.send(**kwargs)
                else:
                    await message.edit(**kwargs)
            elif old and r['kind']=='checkin' and r['status']!='pending':
                try:
                    message=await channel.fetch_message(int(old['message_id']))
                except discord.NotFound:
                    message=await channel.send(**kwargs)
                else:
                    await message.edit(**kwargs)
            else:
                kwargs['allowed_mentions']=discord.AllowedMentions(roles=[discord.Object(id=int(rid)) for rid in role_ids],users=False,everyone=False)
                message=await channel.send(content,**kwargs)
            await self.sql("UPDATE scout_jobs SET status='sent',message_id=?,error='' WHERE id=?",(str(message.id),job['id']))
        except (discord.Forbidden,discord.NotFound):
            await self.sql("UPDATE scout_jobs SET status='failed',error='Discord denied delivery or the original card is missing.' WHERE id=?",(job['id'],))
        except Exception:
            # HTTP 5xx/network timeouts can mean the message was sent. Do not blindly resend.
            await self.sql("UPDATE scout_jobs SET status='uncertain',error='Delivery acknowledgement lost; reconciling receipt marker.' WHERE id=?",(job['id'],))

    @tasks.loop(seconds=15)
    async def worker(self):
        try:
            if not self._recovered:
                await self.sql("UPDATE scout_jobs SET status='uncertain',error='Interrupted delivery; reconciling receipt marker.' WHERE status='sending'")
                self._recovered=True
            await self.sql("UPDATE scout_jobs SET status='uncertain',error='Interrupted delivery; reconciling receipt marker.' WHERE status='sending' AND attempted_at<?",(time.time()-180,))
            settings=await self.call(self.service.rows,'SELECT guild_id FROM scout_settings')
            for row in settings:
                guild=self.bot.get_guild(int(row['guild_id']))
                if not guild:
                    continue
                cfg=await self.call(self.service.settings,guild.id)
                checkpoint=await self.audit_poll(guild,cfg) if cfg['enabled'] else None
                await self.heartbeat(guild,cfg,checkpoint)
                await self.call(self.service.prune,guild.id)
            jobs=await self.call(self.service.rows,"SELECT j.* FROM scout_jobs j JOIN scout_settings s ON s.guild_id=j.guild_id WHERE json_extract(s.data,'$.enabled')=1 AND (j.status='pending' OR (j.status='uncertain' AND j.checked_at<?)) ORDER BY CASE j.status WHEN 'pending' THEN 0 ELSE 1 END,j.checked_at,j.created_at LIMIT 50", (time.time()-60,))
            for job in jobs:
                try:
                    await self.deliver(job)
                except Exception:
                    log.exception('Scout delivery worker error job=%s',job['id'])
        except Exception:
            log.exception('Scout worker iteration failed')

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ScoutsCog(bot))
