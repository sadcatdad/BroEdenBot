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
from urllib.parse import quote, urlsplit

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.scouts import Scouts, ACTIVITY_TYPES, REPORT_CATEGORIES, CARD_LAYOUT_VERSION, encode, status_label, status_key, record_title
from utils.scout_access import discord_member_actor
from utils.scout_forms import FORM_DEFAULTS
from utils.settings import get_setting

log = logging.getLogger(__name__)
SCOUT_EMBED_COLOR = 0x9B59B6


def dashboard_url(rid=''):
    base = str(get_setting('DASHBOARD_PUBLIC_URL', '') or os.getenv('DASHBOARD_PUBLIC_URL', '')).rstrip('/')
    return base + '/scouts' + ('/records/' + rid if rid else '') if base.startswith('https://') else ''


def snapshot(message):
    return dict(message_id=str(message.id), channel_id=str(message.channel.id), author_id=str(message.author.id), author_name=str(getattr(message.author,'display_name',message.author)),
                content=message.content, created_at=message.created_at.timestamp(),
                edited_at=message.edited_at.timestamp() if message.edited_at else None,
                jump_url=message.jump_url,
                attachments=[dict(id=str(a.id), filename=a.filename, url=a.url, proxy_url=a.proxy_url) for a in message.attachments])


def safe(text, limit=3900):
    # All content is untrusted: suppress pings and escape display formatting.
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))[:limit]


def member_reference(value):
    value = str(value or '')
    return f'<@{value}>' if value.isdigit() else safe(value or 'Unknown', 80)


def channel_reference(value):
    value = str(value or '')
    return f'<#{value}>' if value.isdigit() else safe(value or 'Unknown', 80)


def delivery_receipt_matches(embed, marker):
    # New cards keep the durable receipt in the title link's fragment. Retain
    # footer matching so sends interrupted before this layout update still recover.
    return (getattr(getattr(embed, 'footer', None), 'text', None) == marker
            or urlsplit(getattr(embed, 'url', None) or '').fragment
            == 'scout-delivery=' + quote(marker, safe=''))


def record_label(record):
    # Internal UUIDs remain storage keys; never display a shortened UUID as an ID.
    return str(record.get('short_id') or '')


def card_embed(record, marker):
    d, kind = record['data'], record['kind']
    title = {'manual': 'Scout activity', 'report': 'Member report', 'system': 'Scout action',
             'escalation': 'Help requested', 'checkin': 'Scout check-in'}[kind]
    lines = []

    def bullet(label, value, limit=650):
        if value:
            text = '\n'.join(line.strip() for line in str(value).splitlines() if line.strip())
            lines.append('• **' + label + ':** ' + safe(text, limit))

    if kind == 'system':
        action = d.get('action_type', '')
        title = record_title(record)
        actor = member_reference(d.get('moderator_id')) if d.get('moderator_id') else 'Not confirmed'
        lines.append('• **' + ('Scout' if d.get('confirmed_scout') else 'Actor') + ':** ' + actor)
        if d.get('target_id') and str(d['target_id']) != str(d.get('channel_id', '')):
            lines.append('• **Member:** ' + member_reference(d['target_id']))
        if d.get('channel_id'):
            lines.append('• **Channel:** ' + channel_reference(d['channel_id']))
        bullet('Reason', d.get('reason'), 350)
        if action == 'timeout' and d.get('after'):
            lines.append(f"• **Until:** <t:{int(d['after'])}:f>")
        messages = d.get('messages', [])
        for index, message in enumerate(messages[:3], 1):
            text = message.get('content')
            bullet('Deleted text' if len(messages) == 1 else f'Message {index}',
                   text if text else 'Not captured.', 650)
        if action in ('message_delete', 'message_bulk_delete') and not messages:
            bullet('Deleted text', 'Not captured.')
        if messages and d.get('message_context_attribution'):
            lines.append('• Captured text matched to this action; verify the context in The Garden.')
        if len(messages) > 3:
            lines.append('• More captured messages are in The Garden.')
    elif kind == 'report':
        message = d['message']
        bullet('Category', d['category'], 120)
        lines.append('• **Member:** ' + member_reference(message.get('author_id'))
                     + ' · **Channel:** ' + channel_reference(message['channel_id']))
        bullet('Message', message.get('content') or 'Not captured.', 1800)
        if message.get('jump_url'):
            lines.append('[Open reported message](' + message['jump_url'] + ')')
    elif kind == 'checkin':
        lines.append('• **For:** ' + member_reference(d['recipient_id']))
        if d.get('deadline'):
            lines.append(f"• **Due:** <t:{int(d['deadline'])}:f>")
        for index, question in enumerate(d['questions'], 1):
            bullet(str(index), question, 200)
        lines.append('Answers stay in The Garden.' if record['status'] == 'pending' else 'This request is closed.')
    else:
        lines.append('• **Scout:** ' + member_reference(record['author_id']))
        if kind == 'manual':
            bullet('Type', (d.get('type') or 'other').capitalize(), 80)
        if d.get('channel'):
            lines.append('• **Channel:** ' + channel_reference(d['channel']))
        bullet('Involved', d.get('members'), 180)
        bullet('When', d.get('incident_at'), 80)
        bullet('Help needed' if kind == 'escalation' else 'What happened', d.get('summary'), 1700)
        bullet('Action taken', d.get('action'), 650)
        bullet('Evidence', d.get('evidence'), 350)
        if record.get('related_short_id'):
            lines.append('• **Related:** #' + record['related_short_id'])
        if record['status'] == 'void':
            bullet('Voided', d.get('void_reason'), 250)
    bullet(d.get('form_labels',{}).get('outcome','Outcome'), d.get('outcome'), 400)
    for answer in d.get('form_answers',[]) + d.get('outcome_answers',[]):
        bullet(answer['label'],answer['answer'],300)
    label = record_label(record)
    target = dashboard_url(label) or f"https://discord.com/channels/{record['guild_id']}"
    embed = discord.Embed(title=title, description='\n'.join(lines)[:4096], color=SCOUT_EMBED_COLOR,
                          url=target + '#scout-delivery=' + quote(marker, safe=''),
                          timestamp=datetime.fromtimestamp(record['created_at'], timezone.utc))
    footer = [f'#{label}' if label else 'Scout Operations']
    if kind in ('manual','escalation') or record['status'] not in ('active',):
        footer.append(status_label(record))
    elif label:
        footer.append('Historical evidence' if kind == 'system' and not d.get('confirmed_scout') else 'Scout Operations')
    embed.set_footer(text=' · '.join(footer))
    return embed


class OpenForm(discord.ui.View):
    def __init__(self, uid, factory, *, label='Open form', new_entry_factory=None):
        super().__init__(timeout=300)
        self.uid, self.factory = uid, factory
        self.open.label = label
        self.new_entry_factory=new_entry_factory
        if new_entry_factory:
            button=discord.ui.Button(label='Save as a new entry',style=discord.ButtonStyle.secondary)
            async def start_new(interaction):
                if interaction.user.id!=self.uid:
                    await interaction.response.send_message('This draft belongs to another member.',ephemeral=True)
                    return
                await interaction.response.send_modal(new_entry_factory())
            button.callback=start_new
            self.add_item(button)

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
        self.questions=questions
        self.values, self.related, self.source = values or {}, related, source
        profile = 'activity' if mode in ('manual','edit') else 'outcome' if mode=='handle' else 'escalation'
        schema=self.values.get('_form_schema') or FORM_DEFAULTS[profile]
        self.inputs = {}
        if mode=='answer':
            schema=[dict(key='answer'+str(i),label=q[:45],placeholder=q[:100],required=True,enabled=True) for i,q in enumerate(questions or [])]
        for f in schema:
            if not f['enabled']:
                continue
            key=f['key']
            field = discord.ui.TextInput(label=f['label'],style=discord.TextStyle.paragraph,
                required=f['required'],max_length=2000 if key=='evidence' else 4000,
                default=self.values.get(key) or None,placeholder=f.get('placeholder') or None)
            self.inputs[key]=field
            self.add_item(field)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        values = dict(self.values, **{key: str(field.value) for key,field in self.inputs.items()})
        if self.mode in ('manual', 'escalate'):
            self.source = self.source or str(interaction.id)
        try:
            actor = await self.cog.actor(interaction.user.id, self.guild)
            if self.mode=='manual':
                r = await self.cog.call(self.cog.service.manual, actor, values, self.related, self.source)
            elif self.mode=='edit':
                r = await self.cog.call(self.cog.service.edit, actor, self.rid, values, self.values['version'])
            elif self.mode=='escalate':
                r = await self.cog.call(self.cog.service.escalate, actor, values['reason'], self.related or self.rid, self.source, values)
            elif self.mode=='handle':
                r = await self.cog.call(self.cog.service.handle, actor, self.rid, values['reason'], values)
            else:
                r = await self.cog.call(self.cog.service.answer, actor, self.rid, [f.value for f in self.inputs.values()])
            if self.mode=='answer':
                await interaction.followup.send('Response saved. Your answers are available only in The Garden.', ephemeral=True)
            else:
                await self.cog.send_saved(interaction, r)
        except (ValueError, PermissionError) as exc:
            await self.save_error(interaction, str(exc), values)
        except Exception:
            log.exception('Scout form failed mode=%s record=%s', self.mode, self.rid)
            await self.save_error(interaction, 'Could not confirm this save. Check The Garden before resubmitting.', values)

    async def save_error(self, interaction, message, values):
        kwargs = {}
        if self.mode in ('manual','edit','escalate','handle','answer'):
            try:
                cfg=await self.cog.call(self.cog.service.settings,self.guild)
                profile='activity' if self.mode in ('manual','edit') else 'outcome' if self.mode=='handle' else 'escalation'
                values['_form_schema']=cfg['forms'][profile]
            except Exception:
                pass
            kwargs['view'] = OpenForm(interaction.user.id, lambda: ScoutModal(
                self.cog, self.guild, self.mode, rid=self.rid, values=values,
                related=self.related, source=self.source,questions=self.questions), label='Review draft',new_entry_factory=(lambda: ScoutModal(
                    self.cog,self.guild,self.mode,values=values,related='',source=self.source)) if self.related and self.mode!='edit' else None)
            message += '\nYour draft is preserved. Choose Review draft to correct it and try again.'
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), **kwargs)


class ReportModal(discord.ui.Modal, title='Report to Staff'):

    def __init__(self, cog, message, category, reporter, source, fields=None, values=None, target_snapshot=None):
        super().__init__(timeout=600)
        self.cog, self.message, self.category, self.reporter, self.source = cog, message, category, reporter, source
        self.target_snapshot = target_snapshot or snapshot(message)
        self.fields=fields or FORM_DEFAULTS['report']
        self.inputs={}
        for f in self.fields:
            if f['enabled']:
                item=discord.ui.TextInput(label=f['label'],placeholder=f.get('placeholder') or None,required=f['required'],style=discord.TextStyle.paragraph,max_length=4000,default=(values or {}).get(f['key']) or None)
                self.inputs[f['key']]=item
                self.add_item(item)
        self.explanation=self.inputs['explanation']

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            if interaction.user.id != self.reporter:
                raise PermissionError('This report form belongs to another member.')
            await self.cog.report_member(interaction.user.id, self.message.channel)
            r = await self.cog.call(self.cog.service.report, self.message.guild.id, interaction.user.id,
                                    self.target_snapshot, self.category, self.explanation.value, self.source,{k:f.value for k,f in self.inputs.items()})
            await interaction.followup.send(f"Report #{r['short_id']} received. Staff authorized to view protected submissions can read your identity and explanation in The Garden.", ephemeral=True)
        except (ValueError, PermissionError) as exc:
            await self.retry_draft(interaction,str(exc))
        except Exception:
            # Never include the form, reporter or explanation in diagnostic logs.
            log.error('Member-report storage or delivery preparation failed')
            await self.retry_draft(interaction,'Could not confirm receipt. Check for your receipt before retrying; duplicates are prevented.')

    async def retry_draft(self,interaction,message):
        try:
            cfg=await self.cog.call(self.cog.service.settings,self.message.guild.id)
            fields=cfg['forms']['report']
        except Exception:
            fields=self.fields
        values={k:f.value for k,f in self.inputs.items()}
        view=OpenForm(self.reporter,lambda:ReportModal(self.cog,self.message,self.category,self.reporter,self.source,
            fields,values,self.target_snapshot),label='Review report draft')
        await interaction.followup.send(message+' Your private draft is preserved.',ephemeral=True,view=view,allowed_mentions=discord.AllowedMentions.none())


class ReportCategory(discord.ui.Select):
    def __init__(self, cog, message, uid, source, fields=None):
        super().__init__(placeholder='Choose a report category', options=[discord.SelectOption(label=c) for c in REPORT_CATEGORIES])
        self.cog, self.message, self.uid, self.source = cog, message, uid, source
        self.fields=fields

    async def callback(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This report belongs to another member.', ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self.cog, self.message, self.values[0], self.uid, self.source,self.fields))


class DeliveryStatus(discord.ui.View):
    def __init__(self, cog, uid, guild, rid):
        super().__init__(timeout=600)
        self.cog, self.uid, self.guild, self.rid = cog, uid, str(guild), rid
        if url := dashboard_url(rid):
            self.add_item(discord.ui.Button(label='View record', url=url))

    @discord.ui.button(label='Check delivery', style=discord.ButtonStyle.secondary)
    async def check(self, interaction, button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message('This receipt belongs to another member.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            actor = await self.cog.actor(interaction.user.id, self.guild)
            record = await self.cog.call(self.cog.service.record, actor, self.rid)
            await interaction.edit_original_response(content=await self.cog.delivery_summary(record), view=self,
                                                     allowed_mentions=discord.AllowedMentions.none())
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception:
            await interaction.followup.send('Could not check delivery right now. The saved record is retained; try Check delivery again shortly.', ephemeral=True)


class ScoutControl(discord.ui.DynamicItem[discord.ui.Button], template=r'scouts:(?P<action>add|escalate|handle|answer):(?P<rid>(?:[a-f0-9]{16}|[0-9A-Z]{1,4})):(?P<guild>[0-9]+)'):
    def __init__(self, action, rid, guild):
        self.action, self.rid, self.guild = action, rid, str(guild)
        super().__init__(discord.ui.Button(label={'add':'Add follow-up','escalate':'Ask for help','handle':'Mark Handled','answer':'Respond to Check-in'}[action],
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
            cfg=await cog.call(cog.service.settings,self.guild)
            profile='activity' if mode=='manual' else 'outcome' if mode=='handle' else 'escalation'
            factory = lambda: ScoutModal(cog, self.guild, mode, rid=self.rid, values={'_form_schema':cfg['forms'][profile]}, related=self.rid if mode in ('manual','escalate') else '', questions=r['data'].get('questions'))
            await interaction.followup.send('Open the form to continue.', ephemeral=True, view=OpenForm(interaction.user.id, factory))
        except (ValueError, PermissionError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)


def record_view(record):
    view = discord.ui.View(timeout=None)
    if record['kind']=='checkin':
        if record['status']=='pending':
            view.add_item(ScoutControl('answer',record['short_id'],record['guild_id']))
    else:
        for action in ('add','escalate'):
            view.add_item(ScoutControl(action,record['short_id'],record['guild_id']))
        if record['kind']=='report' and record['status']=='new':
            view.add_item(ScoutControl('handle',record['short_id'],record['guild_id']))
    if url := dashboard_url(record['short_id']):
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
        return f"Scout {self.scout} · {self.days} days · {totals[0]['total']} standalone manual records.{system_total} Linked context is listed separately; member reports are not Scout activity.\nPage {self.page}\n" + '\n'.join(f"#{r['short_id']} · {status_label(r)} · {safe(r['data']['summary'],20)}" for r in rows[:25])

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
        await self.call(self.service.queue_card_refresh)
        self.bot.tree.add_command(self.report_menu)
        self.bot.tree.add_command(self.log_menu)
        self.bot.add_dynamic_items(ScoutControl)
        self.worker.start()
        self.audit_worker.start()

    def cog_unload(self):
        self.worker.cancel()
        self.audit_worker.cancel()
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

    async def delivery_summary(self, record):
        jobs = await self.call(self.service.rows,
            'SELECT status,destination,message_id,error FROM scout_jobs WHERE guild_id=? AND record_id=? AND version=? ORDER BY created_at DESC LIMIT 1',
            (record['guild_id'], record['id'], record['version']))
        message = f"Saved **#{record['short_id']}**. "
        if not jobs:
            return message + 'No channel delivery was queued. Check this record in The Garden.'
        job = jobs[0]
        destination = channel_reference(job['destination'])
        if job['status'] == 'sent':
            return message + f"Posted to {destination}. [View message](https://discord.com/channels/{record['guild_id']}/{job['destination']}/{job['message_id']})."
        if job['status'] in ('pending', 'sending'):
            return message + f"{'Queued for' if job['status']=='pending' else 'Posting to'} {destination}. Use Check delivery to confirm it arrives."
        if job['status'] == 'uncertain':
            return message + f'Delivery to {destination} is being checked after a connection error. Do not resubmit.'
        return message + f"Delivery to {destination} {job['status']}: {safe(job['error'] or 'Check The Garden for details.', 250)} The saved record is retained."

    async def send_saved(self, interaction, record):
        try:
            message = await self.delivery_summary(record)
        except Exception:
            message = f"Saved **#{record['short_id']}**. Check delivery below or view this record in The Garden; no need to resubmit."
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            view=DeliveryStatus(self, interaction.user.id, record['guild_id'], record['short_id']))

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
            cfg=await self.call(self.service.settings,interaction.guild_id)
            view.add_item(ReportCategory(self,message,interaction.user.id,str(interaction.id),cfg['forms']['report']))
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
            cfg=await self.call(self.service.settings,actor.guild_id)
            values=dict(_form_schema=cfg['forms']['activity'],type='other', summary=message.content[:4000] or 'Message context', evidence=message.jump_url,members=str(message.author.id),channel=str(message.channel.id))
            await interaction.followup.send('Log activity with the selected message as context.',ephemeral=True,view=OpenForm(interaction.user.id,lambda: ScoutModal(self,interaction.guild_id,'manual',values=values,source=str(interaction.id))))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @scout.command(name='log',description='Write a structured Scout activity record')
    @app_commands.choices(activity_type=[app_commands.Choice(name=t,value=t) for t in ACTIVITY_TYPES])
    @app_commands.rename(activity_type='type',members='member',incident_time='when',related_record='follow_up_to',evidence_link='message_link')
    @app_commands.describe(activity_type='What kind of activity was this?', members='Involved members (names or mentions)',
        channel='Channel where the activity occurred', incident_time='When it happened, including timezone when known',
        related_record='Choose a recent activity/report to follow up. Leave empty for a new log.',
        evidence_link='Paste a Discord message link, or use the message’s Log Scout Activity action.')
    async def log_activity(self,interaction:discord.Interaction,activity_type:str='other',members:Optional[discord.Member]=None,channel:Optional[discord.TextChannel]=None,incident_time:str='',related_record:str='',evidence_link:str=''):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            await self.call(self.service.enabled,actor.guild_id)
            reference = await self.call(self.service.resolve_reference, actor, related_record)
            values=dict(type=activity_type,members=member_reference(members.id) if hasattr(members,'id') else str(members or ''),channel=str(channel.id) if channel else '',incident_at=incident_time,evidence=evidence_link)
            self.service._add_evidence(values, reference['evidence'])
            cfg = await self.call(self.service.settings, actor.guild_id)
            values['_form_schema']=cfg['forms']['activity']
            message = 'Open the form to finish your activity log. It will post to ' + channel_reference(cfg['activity_channel']) + '.'
            if reference['record_id']:
                message += f" Linked Scout record: #{reference['short_id']}."
            elif reference['evidence']:
                message += ' Your message link is prefilled as evidence.'
            await interaction.followup.send(message,ephemeral=True,allowed_mentions=discord.AllowedMentions.none(),view=OpenForm(interaction.user.id,lambda:ScoutModal(self,interaction.guild_id,'manual',values=values,related=related_record,source=str(interaction.id))))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @scout.command(name='edit',description='Revise an owned manual activity record')
    async def edit_activity(self,interaction:discord.Interaction,record:str):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            reference = await self.call(self.service.resolve_reference, actor, record)
            if not reference['record_id']:
                raise ValueError('Choose an existing Scout record to edit. An ordinary message link can be used as evidence in /scout log.')
            record = reference['short_id']
            r=await self.call(self.service.record,actor,record)
            if r['kind']!='manual' or r['author_id']!=actor.user_id or r['status']=='void':
                raise PermissionError('Only the author can edit a manual entry that has not been voided.')
            cfg=await self.call(self.service.settings,actor.guild_id)
            values=dict(r['data'],version=r['version'],_form_schema=cfg['forms']['activity'])
            for answer in r['data'].get('form_answers',[]):
                values[answer['key']]=answer['answer']
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
    @app_commands.rename(record='follow_up_to')
    @app_commands.describe(record='Choose an activity/report to ask for help with. Leave empty for a new concern.')
    async def escalate(self,interaction:discord.Interaction,record:str=''):
        await interaction.response.defer(ephemeral=True)
        try:
            actor=await self.actor(interaction.user.id,interaction.guild_id)
            actor.require('activity.create')
            await self.call(self.service.enabled,actor.guild_id)
            await self.call(self.service.resolve_reference, actor, record)
            cfg=await self.call(self.service.settings,actor.guild_id)
            await interaction.followup.send('Tell higher staff what help you need. A new concern needs no existing record.',ephemeral=True,view=OpenForm(interaction.user.id,lambda:ScoutModal(self,interaction.guild_id,'escalate',values={'_form_schema':cfg['forms']['escalation']},related=record,source=str(interaction.id))))
        except (ValueError,PermissionError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)

    @log_activity.autocomplete('related_record')
    @escalate.autocomplete('record')
    @edit_activity.autocomplete('record')
    async def select_record(self, interaction: discord.Interaction, current: str):
        try:
            actor=await asyncio.wait_for(self.actor(interaction.user.id,interaction.guild_id),1.3)
            rows=await asyncio.wait_for(self.call(self.service.reference_choices,actor,current,
                interaction.command.name=='edit'),0.7)
            return [app_commands.Choice(name=r['name'][:100],value=r['value']) for r in rows]
        except (PermissionError, ValueError, asyncio.TimeoutError):
            return []

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

    async def deleted(self,guild_id,channel_id,message_ids,bulk=False,cached_messages=None):
        guild=self.bot.get_guild(guild_id) if guild_id else None
        channel=guild.get_channel_or_thread(channel_id) if guild else None
        if not channel or not await self.call(self.service.capture_allowed,guild_id,channel_id,str(channel.category_id or ''),str(getattr(channel,'parent_id','') or '')):
            return
        ids=sorted(str(m) for m in message_ids)
        key='gateway:'+('bulk:' if bulk else 'delete:')+hashlib.sha256(','.join(ids).encode()).hexdigest()
        await self.call(self.service.system,guild_id,key,dict(action_type='message_bulk_delete' if bulk else 'message_delete',event_at=time.time(),channel_id=str(channel_id),message_ids=ids,cached_messages=[snapshot(m) for m in (cached_messages or [])],attribution='unattributed: deletion events do not identify an actor'))

    @commands.Cog.listener()
    async def on_raw_message_delete(self,payload):
        await self.deleted(payload.guild_id,payload.channel_id,[payload.message_id],cached_messages=[payload.cached_message] if payload.cached_message else [])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self,payload):
        await self.deleted(payload.guild_id,payload.channel_id,payload.message_ids,True,cached_messages=payload.cached_messages)

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
        if not cfg['enabled'] or not cfg['automatic_enabled'] or action not in cfg['action_types']:
            return
        if entry.created_at.timestamp()<cfg.get('capture_since',time.time()):
            return
        moderator=entry.user
        roles=[]
        human=False
        moderator_is_bot=bool(getattr(moderator,'bot',False))
        if moderator:
            try:
                moderator=await guild.fetch_member(moderator.id)
                roles=sorted(str(r.id) for r in moderator.roles if str(r.id) in cfg['scout_roles'])
                human=not moderator.bot
                moderator_is_bot=moderator.bot
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
                  moderator_id=str(entry.user.id) if entry.user else '',moderator_name=str(getattr(moderator,'display_name','')),
                  scout_roles=roles,role_snapshot_at=time.time(),
                  confirmed_scout=bool(human and roles),reason=entry.reason or '',
                  moderator_is_bot=moderator_is_bot,
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
        state.update(readiness=readiness,scouts=roster,notification_layout=CARD_LAYOUT_VERSION,short_record_ids=True,forms_version=1)
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
        refreshing = job['purpose'].startswith('layout:')
        if refreshing and job['status']=='pending' and job['version'] != r['version']:
            await self.sql("UPDATE scout_jobs SET status='cancelled',error='Newer record already queued for delivery.' WHERE id=? AND status='pending'", (job['id'],))
            return
        if r['kind']=='system':
            if not self.service.confirmed_scout_action(r['data'],r['source_key']):
                if refreshing:
                    # Existing historical cards may be restyled, never republished
                    # or promoted to confirmed Scout activity. The edit-only path
                    # below requires a sent receipt and never recreates a card.
                    pass
                elif job['status']=='pending':
                    await self.sql("UPDATE scout_jobs SET status='cancelled',error='Suppressed: automatic logging requires a confirmed human Scout audit actor.' WHERE id=? AND status='pending'",(job['id'],))
                elif job['status']=='uncertain':
                    # Reconcile previously attempted deliveries, but never resend.
                    pass
                else:
                    return
                if not refreshing and job['status']!='uncertain':
                    return
            elif not cfg['automatic_enabled'] and job['status']=='pending':
                return
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
                    if message.author.id==self.bot.user.id and any(delivery_receipt_matches(e,marker) for e in message.embeds):
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
            if refreshing and (not old or old['destination'] != job['destination']):
                await self.sql("UPDATE scout_jobs SET status='cancelled',error='Original card unavailable; no replacement posted.' WHERE id=?", (job['id'],))
                return
            kwargs=dict(embed=card_embed(r,marker),view=record_view(r),allowed_mentions=discord.AllowedMentions.none())
            role_ids=[]
            if not old and r['kind']=='report' and r['data'].get('notify_roles'):
                role_ids=cfg['report_roles']
            if not old and r['kind']=='escalation' and status_key(r) in ('open','in_progress'):
                role_ids=cfg['escalation_roles']
            content=' '.join('<@&'+rid+'>' for rid in role_ids) or None
            if old and old['destination']==job['destination'] and r['kind']!='checkin':
                try:
                    message=await channel.fetch_message(int(old['message_id']))
                except discord.NotFound:
                    if refreshing:
                        await self.sql("UPDATE scout_jobs SET status='cancelled',error='Original card was removed; no replacement posted.' WHERE id=?", (job['id'],))
                        return
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

    @tasks.loop(seconds=2)
    async def worker(self):
        try:
            if not self._recovered:
                await self.sql("UPDATE scout_jobs SET status='uncertain',error='Interrupted delivery; reconciling receipt marker.' WHERE status='sending'")
                self._recovered=True
            await self.sql("UPDATE scout_jobs SET status='uncertain',error='Interrupted delivery; reconciling receipt marker.' WHERE status='sending' AND attempted_at<?",(time.time()-180,))
            jobs=await self.call(self.service.rows,"SELECT j.* FROM scout_jobs j JOIN scout_settings s ON s.guild_id=j.guild_id JOIN scout_records r ON r.id=j.record_id AND r.guild_id=j.guild_id WHERE json_extract(s.data,'$.enabled')=1 AND (j.status='pending' OR (j.status='uncertain' AND j.checked_at<?)) ORDER BY CASE WHEN j.purpose LIKE 'layout:%' THEN 1 ELSE 0 END,CASE j.status WHEN 'pending' THEN 0 ELSE 1 END,CASE r.kind WHEN 'checkin' THEN 1 ELSE 0 END,j.checked_at,j.created_at LIMIT 50", (time.time()-60,))
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

    @tasks.loop(seconds=15)
    async def audit_worker(self):
        # Audit history can require multiple HTTP requests. Keep that work off the
        # serialized delivery worker so it cannot hold up manual logs or reports.
        try:
            settings = await self.call(self.service.rows, 'SELECT guild_id FROM scout_settings')
            for row in settings:
                guild = self.bot.get_guild(int(row['guild_id']))
                if not guild:
                    continue
                try:
                    cfg = await self.call(self.service.settings, guild.id)
                    await self.heartbeat(guild, cfg)
                    await self.call(self.service.prune, guild.id)
                    checkpoint = await self.audit_poll(guild, cfg) if cfg['enabled'] else None
                    if checkpoint:
                        await self.heartbeat(guild, cfg, checkpoint)
                except Exception:
                    log.exception('Scout audit worker failed guild=%s', guild.id)
        except Exception:
            log.exception('Scout audit worker iteration failed')

    @audit_worker.before_loop
    async def before_audit_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ScoutsCog(bot))
