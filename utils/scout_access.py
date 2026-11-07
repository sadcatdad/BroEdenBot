"""Live membership adapters. Scout eligibility and capability grants stay separate."""
from __future__ import annotations

import asyncio
import os
import httpx

from dashboard.rbac import PERMISSION_KEYS, permissions_for_discord_member
from utils.access import configured_owner_user_ids
from utils.scouts import Actor


def member_actor(service, guild, uid, roles, *, pending=False, bot=False):
    uid, guild = str(uid), str(guild)
    roles = {str(r) for r in roles}
    with service.connect() as c:
        table = c.execute("SELECT 1 FROM sqlite_master WHERE name='dashboard_users'").fetchone()
        account = c.execute('SELECT id,status FROM dashboard_users WHERE discord_user_id=?', (uid,)).fetchone() if table else None
    if pending or bot or (account and account['status'] != 'active'):
        return Actor(guild, uid, frozenset(), False)
    # The existing resolver accepts the dashboard primary key, not a Discord snowflake.
    permissions = permissions_for_discord_member(int(account['id']) if account else 0, roles)
    if int(uid) in configured_owner_user_ids():
        permissions = set(PERMISSION_KEYS)
    if not roles & set(service.settings(guild)['scout_roles']):
        permissions -= {'scouts.activity.create', 'scouts.checkins.respond'}
    return Actor(guild, uid, frozenset(permissions))


async def discord_member_actor(service, guild, member):
    return await asyncio.to_thread(member_actor, service, guild, member.id, [r.id for r in member.roles], pending=member.pending, bot=member.bot)


async def fetch_member(guild, uid):
    token = os.getenv('DISCORD_TOKEN', '')
    if not token:
        raise PermissionError('Live Discord membership verification is unavailable.')
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f'https://discord.com/api/v10/guilds/{int(guild)}/members/{int(uid)}', headers={'Authorization': f'Bot {token}'})
            response.raise_for_status()
            member = response.json()
            if not isinstance(member, dict) or 'roles' not in member:
                raise ValueError('Invalid member response')
            return member
    except (httpx.HTTPError, ValueError, TypeError):
        raise PermissionError('Current server membership could not be verified. Try again shortly.') from None


async def dashboard_actor(request, service, guild):
    from dashboard.auth import current_user, current_permissions
    user = current_user(request)
    if not user:
        raise PermissionError('Sign in first.')
    uid = user.get('discord_user_id')
    if not uid:
        # Existing password-based emergency owner/admin can configure and review;
        # writing as a Scout and answering check-ins still require a Discord identity.
        permissions = current_permissions(request)
        if 'access.manage' not in permissions:
            raise PermissionError('Link a Discord account to use Scout Operations.')
        return Actor(str(guild), f"dashboard:{user['id']}", frozenset(permissions - {'scouts.activity.create', 'scouts.checkins.respond'}))
    member = await fetch_member(guild, uid)
    return await asyncio.to_thread(member_actor, service, guild, uid, member['roles'], pending=member.get('pending', False), bot=member.get('user', {}).get('bot', False))
