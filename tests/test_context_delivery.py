"""Channel delivery keeps private defaults and checks access at publication."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.message_context import MessageContext


class ContextDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = Mock(spec=discord.Member)
        self.member.id = 10
        self.member.roles = [SimpleNamespace(id=30)]
        self.bot_member = Mock(spec=discord.Member)
        self.bot_member.id = 20
        self.guild = SimpleNamespace(
            id=42,
            fetch_member=AsyncMock(
                side_effect=lambda member_id: (
                    self.member if member_id == 10 else self.bot_member
                )
            ),
            fetch_channel=AsyncMock(),
        )
        self.destination = Mock(spec=discord.TextChannel)
        self.destination.id = 100
        self.destination.guild = self.guild
        self.destination.mention = '<#100>'
        self.destination.send = AsyncMock(
            return_value=SimpleNamespace(jump_url='https://discord.com/channels/42/100/1')
        )
        self.member_permissions = discord.Permissions(
            view_channel=True, send_messages=True
        )
        self.bot_permissions = discord.Permissions(
            view_channel=True, send_messages=True, embed_links=True
        )
        self.destination.permissions_for.side_effect = lambda member: (
            self.member_permissions if member.id == 10 else self.bot_permissions
        )
        self.guild.fetch_channel.return_value = self.destination
        self.interaction = SimpleNamespace(
            user=self.member,
            guild=self.guild,
            guild_id=42,
            channel_id=99,
            response=SimpleNamespace(
                is_done=lambda: True,
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        self.cog = MessageContext(SimpleNamespace(user=self.bot_member))
        settings = patch(
            'cogs.message_context.get_csv_ids_setting',
            side_effect=lambda key: [30] if key == 'MESSAGE_CONTEXT_ALLOWED_ROLE_IDS' else [],
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.embed = discord.Embed(title='Staff context', description='Archive summary')

    async def test_default_result_stays_ephemeral(self):
        await self.cog._send_result(self.interaction, self.embed)
        self.assertTrue(self.interaction.followup.send.await_args.kwargs['ephemeral'])
        self.destination.send.assert_not_awaited()
        self.guild.fetch_channel.assert_not_awaited()

    async def test_selected_channel_gets_embed_and_private_confirmation(self):
        await self.cog._send_result(
            self.interaction, self.embed, post_to=self.destination
        )
        payload = self.destination.send.await_args.kwargs
        self.assertIs(payload['embed'], self.embed)
        self.assertEqual(payload['allowed_mentions'].to_dict(), {'parse': []})
        confirmation = self.interaction.followup.send.await_args
        self.assertTrue(confirmation.kwargs['ephemeral'])
        self.assertIn('https://discord.com/channels/42/100/1', confirmation.args[0])
        self.assertNotIn('embed', confirmation.kwargs)

    async def test_permissions_and_membership_are_reloaded_before_posting(self):
        self.assertTrue(await self.cog._check_post_to(self.interaction, self.destination))
        # Access removed after the preflight check, while the AI is running.
        self.member.roles = []
        await self.cog._send_result(
            self.interaction, self.embed, post_to=self.destination
        )
        self.destination.send.assert_not_awaited()
        self.assertIn('no longer have access', self.interaction.followup.send.await_args.args[0])
        self.assertTrue(self.interaction.followup.send.await_args.kwargs['ephemeral'])
        self.assertEqual(self.guild.fetch_member.await_args_list[-1].args, (10,))

    async def test_posting_requires_invoker_and_bot_channel_permissions(self):
        for actor, permission in (
            ('member', 'view_channel'), ('member', 'send_messages'),
            ('bot', 'view_channel'), ('bot', 'send_messages'), ('bot', 'embed_links'),
        ):
            with self.subTest(actor=actor, permission=permission):
                permissions = getattr(self, actor + '_permissions')
                setattr(permissions, permission, False)
                self.assertFalse(
                    await self.cog._check_post_to(self.interaction, self.destination)
                )
                self.destination.send.assert_not_awaited()
                self.assertTrue(self.interaction.followup.send.await_args.kwargs['ephemeral'])
                setattr(permissions, permission, True)

    async def test_cross_server_destination_is_rejected(self):
        self.destination.guild = SimpleNamespace(id=43)
        self.assertFalse(await self.cog._check_post_to(self.interaction, self.destination))
        self.guild.fetch_member.assert_not_awaited()
        self.destination.send.assert_not_awaited()

    async def test_non_text_destination_is_rejected(self):
        self.guild.fetch_channel.return_value = Mock(spec=discord.ForumChannel)
        self.assertFalse(await self.cog._check_post_to(self.interaction, self.destination))
        self.destination.send.assert_not_awaited()

    async def test_discord_send_failure_is_reported_privately(self):
        self.destination.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'Missing Permissions'
        )
        await self.cog._send_result(
            self.interaction, self.embed, post_to=self.destination
        )
        payload = self.interaction.followup.send.await_args
        self.assertTrue(payload.kwargs['ephemeral'])
        self.assertIn('could not post', payload.args[0])
        self.assertNotIn('embed', payload.kwargs)

    async def test_all_five_commands_deliver_to_destination(self):
        self.cog._deny = AsyncMock(return_value=False)
        self.cog._staff_ai_cooldown = AsyncMock(return_value=False)
        self.cog._synthesize = AsyncMock(return_value='Neutral recap.')
        self.cog._synthesize_structured = AsyncMock(
            return_value=({'summary': 'Neutral recap.'}, 'raw private recap')
        )
        rows = [{
            'timestamp': '2026-11-25T00:00:00+00:00',
            'channel_id': '99', 'channel_name': 'general',
            'author_display_name': 'Member', 'author_name': 'Member',
            'author_id': '10', 'content': 'Hello',
            'jump_url': None, 'source': 'live_discord',
        }]
        self.cog._search_rows = AsyncMock(return_value=rows)
        self.cog._range_rows = AsyncMock(return_value=rows)
        self.cog._count_rows = AsyncMock(return_value=1)
        source_channel = SimpleNamespace(id=99, name='general')
        cases = [
            ('search', {'query': 'Hello', 'channel': source_channel}),
            ('summarize', {'after': 'today', 'channel': source_channel}),
            ('timeline', {'after': 'today', 'channel': source_channel}),
            ('channel', {'channel': source_channel, 'timeframe': '24h'}),
            ('user', {'user': SimpleNamespace(id=15, display_name='Member'), 'timeframe': '24h'}),
        ]
        with patch('cogs.message_context.set_ai_cooldown', new=AsyncMock()):
            for name, kwargs in cases:
                with self.subTest(command=name):
                    self.destination.send.reset_mock()
                    self.guild.fetch_channel.reset_mock()
                    command = getattr(MessageContext, 'context_' + name)
                    await command.callback(
                        self.cog, self.interaction, post_to=self.destination, **kwargs
                    )
                    self.destination.send.assert_awaited_once()
                    self.assertEqual(self.guild.fetch_channel.await_count, 2)
                    self.assertTrue(self.interaction.response.defer.await_args.kwargs['ephemeral'])
                    self.assertTrue(self.interaction.followup.send.await_args.kwargs['ephemeral'])
                    if name == 'search':
                        self.assertEqual(self.cog._search_rows.await_args.kwargs['channel_id'], 99)
                    elif name != 'user':
                        self.assertEqual(self.cog._range_rows.await_args.kwargs['channel_id'], 99)

    async def test_invalid_destination_stops_before_archive_retrieval(self):
        self.cog._deny = AsyncMock(return_value=False)
        self.cog._search_rows = AsyncMock()
        self.member_permissions.send_messages = False
        await MessageContext.context_search.callback(
            self.cog, self.interaction, query='Hello', post_to=self.destination
        )
        self.cog._search_rows.assert_not_awaited()
        self.destination.send.assert_not_awaited()

    async def test_no_matches_and_generation_failure_never_post(self):
        for rows in ([], [object()]):
            with self.subTest(rows=bool(rows)):
                self.cog._synthesize = AsyncMock(side_effect=RuntimeError('AI failed'))
                await self.cog._send_analysis(
                    self.interaction, rows, title='Summary', task='Recap', request='Today',
                    post_to=self.destination,
                )
                self.destination.send.assert_not_awaited()
                self.assertTrue(self.interaction.followup.send.await_args.kwargs['ephemeral'])

    async def test_public_user_fallback_stays_private_with_destination(self):
        self.cog._synthesize_structured = AsyncMock(return_value=(None, 'private raw recap'))
        await self.cog._send_structured_analysis(
            self.interaction, [object()], kind='public_user', title='Evaluation',
            task='Evaluate', request='Today', schema={}, timeframe_text='Today',
            total_matching_count=1, target_label='user=15', timeframe_label='24h',
            source_command='/context user', task_type='public_context_user',
            public_result=True, post_to=self.destination,
        )
        self.destination.send.assert_not_awaited()
        payload = self.interaction.followup.send.await_args
        self.assertTrue(payload.kwargs['ephemeral'])
        self.assertNotIn('private raw recap', str(payload))
