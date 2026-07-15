import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import discord

from cogs.streaks import (
    STREAK_FOOTER,
    STREAK_PAGE_SIZE,
    Streaks,
    compute_streaks,
    is_streak_milestone,
)


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.guilds = []
        self.guild = None
        self.channel = None

    async def wait_until_ready(self):
        return None

    def get_guild(self, guild_id):
        return self.guild if self.guild and self.guild.id == guild_id else None

    def get_channel(self, channel_id):
        return (
            self.channel
            if self.channel and self.channel.id == channel_id
            else None
        )


class DummyMember:
    def __init__(self, member_id=42, roles=()):
        self.id = member_id
        self.bot = False
        self.roles = list(roles)
        self.display_name = "Member"
        self.name = "member42"
        self.mention = f"<@{member_id}>"
        self.display_avatar = DummyAvatar(member_id)


class DummyAvatar:
    def __init__(self, member_id):
        self.url = f"https://example.com/{member_id}.png"

    def replace(self, **_kwargs):
        return self


class DummyChannel:
    id = 99
    name = "general"
    parent = None

    def permissions_for(self, _role):
        return SimpleNamespace(view_channel=True)


class DummyRole:
    def __init__(self, role_id, *, staff_permission=False, managed=False):
        self.id = role_id
        self.managed = managed
        self.permissions = SimpleNamespace(
            administrator=False,
            manage_guild=staff_permission,
            manage_channels=False,
            manage_roles=False,
            kick_members=False,
            ban_members=False,
            moderate_members=False,
        )


class RoleGatedChannel(DummyChannel):
    def __init__(self, visible_role_ids):
        self.visible_role_ids = set(visible_role_ids)

    def permissions_for(self, role):
        return SimpleNamespace(
            view_channel=getattr(role, "id", None) in self.visible_role_ids
        )


class DummyHistoryChannel(DummyChannel):
    def __init__(self, messages):
        self.messages = messages

    def history(self, **_kwargs):
        async def generate():
            for message in self.messages:
                yield message

        return generate()


class StreakTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        self.bot = DummyBot(self.database)
        self.cog = Streaks(self.bot)
        await self.cog.cog_load()
        self.cog.weekly_refresh.cancel()
        self.cog.heartbeat_worker.cancel()
        self.cog.restore_worker.cancel()

    async def asyncTearDown(self):
        self.cog.weekly_refresh.cancel()
        self.cog.heartbeat_worker.cancel()
        self.cog.restore_worker.cancel()
        await self.database.close()

    def test_compute_streaks_tracks_current_and_longest(self):
        today = date(2026, 7, 11)
        days = [
            today - timedelta(days=5),
            today - timedelta(days=4),
            today - timedelta(days=1),
            today,
        ]
        self.assertEqual(compute_streaks(days, today), (2, 2))

    def test_missed_day_resets_current_but_preserves_longest(self):
        today = date(2026, 7, 11)
        days = [today - timedelta(days=4), today - timedelta(days=3)]
        self.assertEqual(compute_streaks(days, today), (0, 2))

    def test_configured_and_rolling_milestones(self):
        expected = {7, 14, 30, 45, 60, 100, 150, 200, 250, 300, 350}
        self.assertTrue(all(is_streak_milestone(days) for days in expected))
        self.assertFalse(any(
            is_streak_milestone(days)
            for days in {1, 6, 8, 29, 46, 99, 125, 275}
        ))

    async def test_only_first_qualifying_message_counts_each_day(self):
        guild = SimpleNamespace(id=1, default_role=object())
        author = DummyMember()
        message = SimpleNamespace(
            id=100,
            guild=guild,
            author=author,
            webhook_id=None,
            channel=DummyChannel(),
            content="This is a useful message",
        )
        second = SimpleNamespace(**{**vars(message), "id": 101, "content": "Another useful daily message"})
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
        ):
            first_result = await self.cog._qualify_message(message)
            second_result = await self.cog._qualify_message(second)
        self.assertEqual(first_result, (1, False))
        self.assertIsNone(second_result)

    async def test_message_creation_time_controls_recovered_activity_date(self):
        guild = SimpleNamespace(id=1, default_role=object())
        message = SimpleNamespace(
            id=105,
            guild=guild,
            author=DummyMember(),
            webhook_id=None,
            channel=DummyChannel(),
            content="This was posted while the bot was offline",
            created_at=datetime(2026, 7, 10, 23, 30, tzinfo=timezone.utc),
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
        ):
            await self.cog._qualify_message(message)
        cursor = await self.database.execute(
            "SELECT activity_date FROM streak_days WHERE message_id = '105'"
        )
        self.assertEqual((await cursor.fetchone())[0], "2026-07-10")
        await cursor.close()

    async def test_manual_remove_blocks_later_history_reimport(self):
        guild = SimpleNamespace(id=1, default_role=object())
        today = self.cog._today()
        await self.database.execute(
            """
            INSERT INTO streak_adjustments (
                guild_id, user_id, activity_date, action, reason,
                changed_by, created_at
            ) VALUES ('1', '42', ?, 'remove', 'Operator correction', 'admin', ?)
            """,
            (today.isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        await self.database.commit()
        message = SimpleNamespace(
            id=107,
            guild=guild,
            author=DummyMember(),
            webhook_id=None,
            channel=DummyChannel(),
            content="This otherwise qualifying historical message stays excluded",
            created_at=datetime.now(timezone.utc),
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
        ):
            self.assertIsNone(await self.cog._qualify_message(message))
        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM streak_days WHERE message_id = '107'"
        )
        self.assertEqual((await cursor.fetchone())[0], 0)
        await cursor.close()

    async def test_heartbeat_gap_queues_automatic_restore(self):
        guild = SimpleNamespace(id=1)
        self.bot.guilds = [guild]
        previous = datetime.now(timezone.utc) - timedelta(hours=2)
        await self.database.execute(
            """
            INSERT INTO streak_runtime_state (
                guild_id, last_heartbeat_at, updated_at
            ) VALUES ('1', ?, ?)
            """,
            (previous.isoformat(), previous.isoformat()),
        )
        await self.database.commit()
        with (
            patch("cogs.streaks.get_bool_setting", return_value=True),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
        ):
            await self.cog._record_heartbeats()
        cursor = await self.database.execute(
            """
            SELECT request_source, status FROM streak_restore_requests
            WHERE guild_id = '1'
            """
        )
        self.assertEqual(await cursor.fetchone(), ("automatic", "pending"))
        await cursor.close()

    async def test_restore_request_backfills_history_and_completes(self):
        guild = SimpleNamespace(id=1, default_role=object(), threads=[])
        author = DummyMember()
        message = SimpleNamespace(
            id=106,
            guild=guild,
            author=author,
            webhook_id=None,
            content="A qualifying message from bot downtime",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        channel = DummyHistoryChannel([message])
        message.channel = channel
        guild.text_channels = [channel]
        self.bot.guild = guild
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        end = datetime.now(timezone.utc)
        await self.database.execute(
            """
            INSERT INTO streak_restore_requests (
                guild_id, start_at_utc, end_at_utc, requested_by,
                request_source, status, created_at
            ) VALUES ('1', ?, ?, 'dashboard', 'dashboard', 'pending', ?)
            """,
            (start.isoformat(), end.isoformat(), start.isoformat()),
        )
        await self.database.commit()
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
        ):
            self.assertTrue(await self.cog._process_restore_request())
        cursor = await self.database.execute(
            """
            SELECT status, messages_scanned, days_restored, members_restored
            FROM streak_restore_requests
            """
        )
        self.assertEqual(await cursor.fetchone(), ("completed", 1, 1, 1))
        await cursor.close()

    async def test_short_command_and_private_messages_do_not_count(self):
        guild = SimpleNamespace(id=1, default_role=object())
        author = DummyMember()
        channel = DummyChannel()
        messages = [
            SimpleNamespace(id=1, guild=guild, author=author, webhook_id=None, channel=channel, content="hi"),
            SimpleNamespace(id=2, guild=guild, author=author, webhook_id=None, channel=channel, content="!points now please"),
        ]
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
        ):
            self.assertIsNone(await self.cog._qualify_message(messages[0]))
            self.assertIsNone(await self.cog._qualify_message(messages[1]))

    async def test_private_channels_are_excluded_but_staff_can_count_in_public(self):
        guild = SimpleNamespace(id=1, default_role=object())
        author = DummyMember()
        private_channel = DummyChannel()
        private_channel.permissions_for = lambda _role: SimpleNamespace(
            view_channel=False
        )
        message = SimpleNamespace(
            id=3,
            guild=guild,
            author=author,
            webhook_id=None,
            channel=private_channel,
            content="This message has enough words",
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
        ):
            self.assertIsNone(await self.cog._qualify_message(message))
        message.channel = DummyChannel()
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
        ):
            self.assertEqual(await self.cog._qualify_message(message), (1, False))

    async def test_configured_category_excludes_its_channels(self):
        guild = SimpleNamespace(id=1, default_role=object())
        channel = DummyChannel()
        channel.category_id = 555
        message = SimpleNamespace(
            id=8,
            guild=guild,
            author=DummyMember(),
            webhook_id=None,
            channel=channel,
            content="This message would otherwise qualify today",
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch(
                "cogs.streaks.get_csv_ids_setting",
                side_effect=lambda key: [555] if key == "STREAK_EXCLUDED_CATEGORY_IDS" else [],
            ),
        ):
            self.assertIsNone(await self.cog._qualify_message(message))

    async def test_verified_member_role_makes_role_gated_channel_public(self):
        everyone = DummyRole(1)
        verified = DummyRole(2)
        guild = SimpleNamespace(id=1, default_role=everyone)
        message = SimpleNamespace(
            id=4,
            guild=guild,
            author=DummyMember(roles=[everyone, verified]),
            webhook_id=None,
            channel=RoleGatedChannel({verified.id}),
            content="This verified member message qualifies",
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.configured_staff_role_ids", return_value=set()),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
        ):
            self.assertEqual(await self.cog._qualify_message(message), (1, False))

    async def test_staff_role_alone_does_not_make_private_channel_public(self):
        everyone = DummyRole(1)
        staff = DummyRole(3)
        guild = SimpleNamespace(id=1, default_role=everyone)
        message = SimpleNamespace(
            id=5,
            guild=guild,
            author=DummyMember(roles=[everyone, staff]),
            webhook_id=None,
            channel=RoleGatedChannel({staff.id}),
            content="This private staff message stays excluded",
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.configured_staff_role_ids", return_value={staff.id}),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
        ):
            self.assertIsNone(await self.cog._qualify_message(message))

    async def test_privileged_role_alone_does_not_make_private_channel_public(self):
        everyone = DummyRole(1)
        moderator = DummyRole(4, staff_permission=True)
        guild = SimpleNamespace(id=1, default_role=everyone)
        message = SimpleNamespace(
            id=6,
            guild=guild,
            author=DummyMember(roles=[everyone, moderator]),
            webhook_id=None,
            channel=RoleGatedChannel({moderator.id}),
            content="This private moderator message stays excluded",
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.configured_staff_role_ids", return_value=set()),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
        ):
            self.assertIsNone(await self.cog._qualify_message(message))

    async def test_recent_exact_duplicate_does_not_count_on_another_day(self):
        guild = SimpleNamespace(id=1, default_role=object())
        author = DummyMember()
        channel = DummyChannel()
        message = SimpleNamespace(
            id=10,
            guild=guild,
            author=author,
            webhook_id=None,
            channel=channel,
            content="This is my repeated message",
        )
        first_day = date(2026, 7, 10)
        second_day = date(2026, 7, 11)
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch("cogs.streaks.get_setting", side_effect=lambda _key, default="": default),
            patch.object(self.cog, "_today", return_value=first_day),
        ):
            self.assertEqual(await self.cog._qualify_message(message), (1, False))
        message.id = 11
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch.object(self.cog, "_today", return_value=second_day),
        ):
            self.assertIsNone(await self.cog._qualify_message(message))

    async def test_only_milestones_receive_a_party_reaction(self):
        guild = SimpleNamespace(id=1)
        member = DummyMember()
        message = SimpleNamespace(
            guild=guild,
            author=member,
            add_reaction=AsyncMock(),
        )
        self.cog._send_milestone_notification = AsyncMock()
        with patch.object(self.cog, "_qualify_message", return_value=(7, True)):
            await self.cog.on_message(message)
        message.add_reaction.assert_awaited_once_with("🎉")
        self.cog._send_milestone_notification.assert_awaited_once_with(
            guild,
            member,
            7,
        )

        message.add_reaction.reset_mock()
        self.cog._send_milestone_notification.reset_mock()
        with patch.object(self.cog, "_qualify_message", return_value=(3, False)):
            await self.cog.on_message(message)
        message.add_reaction.assert_not_awaited()
        self.cog._send_milestone_notification.assert_not_awaited()

    async def test_milestone_notification_uses_selected_message_asset(self):
        guild = SimpleNamespace(id=1)
        member = DummyMember()
        channel = SimpleNamespace(
            id=999,
            guild=guild,
            send=AsyncMock(),
        )
        self.bot.channel = channel

        with (
            patch(
                "cogs.streaks.get_setting",
                side_effect=lambda key, default="": "999" if key == "STREAK_MILESTONE_CHANNEL_ID" else default,
            ),
            patch.object(
                self.cog,
                "_milestone_asset_payload",
                new=AsyncMock(return_value={
                    "content": "Way to go <@42>: 14 days!",
                    "embed": {},
                    "buttons": [],
                }),
            ),
        ):
            await self.cog._send_milestone_notification(guild, member, 14)

        channel.send.assert_awaited_once()
        self.assertEqual(
            channel.send.await_args.args[0],
            "Way to go <@42>: 14 days!",
        )
        self.assertIn("allowed_mentions", channel.send.await_args.kwargs)

    async def test_milestone_embed_uses_selected_message_asset_content(self):
        member = DummyMember()
        with patch.object(
            self.cog,
            "_milestone_asset_payload",
            new=AsyncMock(return_value={
                "content": "Proud of <@42> for reaching 30 days!",
                "embed": {},
                "buttons": [],
            }),
        ):
            embed = await self.cog._milestone_embed(1, member, 30)
        self.assertEqual(
            embed.description,
            "Proud of <@42> for reaching 30 days!",
        )

    async def test_seventh_day_creates_unread_milestone_once(self):
        today = date(2026, 7, 13)
        for offset in range(6, 0, -1):
            day = today - timedelta(days=offset)
            await self.database.execute(
                """
                INSERT INTO streak_days (
                    guild_id, user_id, activity_date, message_id,
                    channel_id, message_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("1", "42", day.isoformat(), str(100 + offset), "99", f"h{offset}", day.isoformat()),
            )
        await self.database.commit()
        guild = SimpleNamespace(id=1, default_role=object())
        message = SimpleNamespace(
            id=200,
            guild=guild,
            author=DummyMember(),
            webhook_id=None,
            channel=DummyChannel(),
            content="This is today's qualifying message",
        )
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch("cogs.streaks.get_csv_ids_setting", return_value=[]),
            patch("cogs.streaks.get_int_setting", side_effect=lambda _key, default: default),
            patch.object(self.cog, "_today", return_value=today),
        ):
            self.assertEqual(await self.cog._qualify_message(message), (7, True))
        self.assertEqual(await self.cog._unread_milestone(1, 42), 7)
        cursor = await self.database.execute(
            "SELECT source_message_id, seen_at FROM streak_milestones"
        )
        self.assertEqual(await cursor.fetchone(), ("200", None))
        await cursor.close()

    def test_weekly_tracker_buttons_are_persistent_and_private_capable(self):
        view = self.cog._streak_view(123)
        self.assertIsNone(view.timeout)
        self.assertEqual(
            [item.label for item in view.children],
            ["My Streak", "View Leaderboard", "Streak Rules"],
        )
        self.assertEqual(
            [item.custom_id for item in view.children],
            [
                "streakpanel|me|123",
                "streakpanel|leaderboard|123",
                "streakpanel|rules|123",
            ],
        )

    def test_streak_command_buttons_are_persistent(self):
        view = self.cog._streak_command_view(123)
        self.assertIsNone(view.timeout)
        self.assertEqual(
            [item.label for item in view.children],
            ["Streak Leaderboard", "Rules"],
        )
        self.assertEqual(
            [item.custom_id for item in view.children],
            [
                "streakpanel|leaderboard|123",
                "streakpanel|rules|123",
            ],
        )

    async def test_prefix_streak_attaches_leaderboard_and_rules_buttons(self):
        member = DummyMember()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=member,
            send=AsyncMock(),
        )
        with patch("cogs.streaks.discord.Member", DummyMember):
            await self.cog.streak_prefix.callback(self.cog, ctx)
        view = ctx.send.await_args.kwargs["view"]
        self.assertEqual(
            [item.label for item in view.children],
            ["Streak Leaderboard", "Rules"],
        )

    async def test_prefix_streak_publicly_shows_and_marks_unread_milestone(self):
        member = DummyMember()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=member,
            send=AsyncMock(),
        )
        member_embed = SimpleNamespace(title="Member streak")
        milestone_embed = SimpleNamespace(title="Milestone")
        with (
            patch("cogs.streaks.discord.Member", DummyMember),
            patch.object(self.cog, "_member_embed", new=AsyncMock(return_value=member_embed)),
            patch.object(self.cog, "_unread_milestone", new=AsyncMock(return_value=14)),
            patch.object(self.cog, "_milestone_embed", new=AsyncMock(return_value=milestone_embed)),
            patch.object(self.cog, "_mark_milestones_seen", new=AsyncMock()) as mark_seen,
        ):
            await self.cog.streak_prefix.callback(self.cog, ctx)
        self.assertEqual(
            ctx.send.await_args.kwargs["embeds"],
            [milestone_embed, member_embed],
        )
        mark_seen.assert_awaited_once_with(1, 42, 14)

    async def test_streak_graphics_use_background_days_and_bump_layout(self):
        today = self.cog._today().isoformat()
        rows = [
            ("1", str(user_id), 25 - user_id, 50 - user_id, today, today)
            for user_id in range(1, 22)
        ]
        await self.database.executemany(
            """
            INSERT INTO member_streaks (
                guild_id, user_id, current_streak, longest_streak,
                last_qualified_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self.database.commit()
        guild = SimpleNamespace(
            id=1,
            get_member=lambda user_id: DummyMember(user_id),
        )
        with patch(
            "cogs.streaks.render_ranked_graphic",
            new=AsyncMock(return_value=b"png"),
        ) as render:
            current_file, current_view = await self.cog._leaderboard_page(
                guild,
                "current",
            )
            current_call = render.await_args.kwargs
            longest_file, _longest_view = await self.cog._leaderboard_page(
                guild,
                "longest",
            )
            longest_call = render.await_args.kwargs

        self.assertEqual(STREAK_PAGE_SIZE, 10)
        self.assertEqual(current_file.filename, "streak-leaderboard.png")
        self.assertEqual(longest_file.filename, "streak-leaderboard.png")
        self.assertEqual(current_call["title"], "🔥 Streak Leaderboard")
        self.assertEqual(longest_call["title"], "🔥 Longest Streak Leaderboard")
        self.assertEqual(current_call["layout"], "leaderboard")
        self.assertEqual(current_call["force_columns"], 2)
        self.assertEqual(current_call["footer_text"], STREAK_FOOTER)
        self.assertIsNotNone(current_call["background_bytes"])
        self.assertEqual(len(current_call["sections"][0].items), 10)
        self.assertTrue(current_call["sections"][0].items[0].value.endswith(" days"))
        self.assertEqual(current_view.children[1].label, "Page 1 of 3")

    async def test_streak_embeds_use_command_footer_without_timestamp(self):
        member = await self.cog._member_embed(1, DummyMember())
        self.assertEqual(member.footer.text, STREAK_FOOTER)
        self.assertEqual(
            STREAK_FOOTER,
            "!streak = see your streak | /streak leaderboard = see all",
        )
        self.assertIsNone(member.timestamp)

    def test_rules_embed_explains_public_channel_and_word_rules(self):
        embed = self.cog._rules_embed()
        self.assertIn("more than 3 words", embed.description)
        self.assertIn("Staff-only channels and Bot Center", embed.description)
        self.assertEqual(embed.footer.text, STREAK_FOOTER)

    async def test_streak_buttons_reply_privately_to_any_clicking_member(self):
        guild = SimpleNamespace(id=1, get_member=lambda _user_id: None)
        self.bot.guild = guild
        leaderboard_file = SimpleNamespace(filename="streak-leaderboard.png")
        leaderboard_view = SimpleNamespace()
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": "streakpanel|leaderboard|1"},
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(
            self.cog,
            "_leaderboard_page",
            new=AsyncMock(return_value=(leaderboard_file, leaderboard_view)),
        ):
            await self.cog.on_interaction(interaction)
        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        interaction.followup.send.assert_awaited_once_with(
            file=leaderboard_file,
            view=leaderboard_view,
            ephemeral=True,
        )

        interaction.data = {"custom_id": "streakpanel|rules|1"}
        interaction.response.send_message.reset_mock()
        await self.cog.on_interaction(interaction)
        rules = interaction.response.send_message.await_args.kwargs["embed"]
        self.assertEqual(rules.title, "🔥 STREAK RULES")
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_streak_graphic_page_buttons_preserve_selected_mode(self):
        guild = SimpleNamespace(id=1)
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": "streakboard|longest|next|0"},
            guild=guild,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        result_file = SimpleNamespace(filename="streak-leaderboard.png")
        result_view = SimpleNamespace()
        with patch.object(
            self.cog,
            "_leaderboard_page",
            new=AsyncMock(return_value=(result_file, result_view)),
        ) as page:
            await self.cog.on_interaction(interaction)

        page.assert_awaited_once_with(guild, "longest", page=1)
        interaction.edit_original_response.assert_awaited_once_with(
            attachments=[result_file],
            view=result_view,
        )

    async def test_my_streak_button_privately_reveals_unread_milestone(self):
        member = DummyMember(99)
        guild = SimpleNamespace(id=1, get_member=lambda _user_id: member)
        self.bot.guild = guild
        interaction = SimpleNamespace(
            type=discord.InteractionType.component,
            data={"custom_id": "streakpanel|me|1"},
            user=member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        member_embed = SimpleNamespace(title="Member streak")
        milestone_embed = SimpleNamespace(title="Milestone")
        with (
            patch.object(self.cog, "_member_embed", new=AsyncMock(return_value=member_embed)),
            patch.object(self.cog, "_unread_milestone", new=AsyncMock(return_value=30)),
            patch.object(self.cog, "_milestone_embed", new=AsyncMock(return_value=milestone_embed)),
            patch.object(self.cog, "_mark_milestones_seen", new=AsyncMock()) as mark_seen,
        ):
            await self.cog.on_interaction(interaction)
        interaction.response.send_message.assert_awaited_once_with(
            embeds=[milestone_embed, member_embed],
            ephemeral=True,
        )
        mark_seen.assert_awaited_once_with(1, 99, 30)

    async def test_deleted_qualifying_message_is_removed(self):
        today = self.cog._today().isoformat()
        await self.database.execute(
            """
            INSERT INTO streak_days (
                guild_id, user_id, activity_date, message_id, channel_id,
                message_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("1", "42", today, "100", "99", "hash", today),
        )
        await self.database.execute(
            """
            INSERT INTO streak_milestones (
                guild_id, user_id, milestone_days, source_message_id, earned_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("1", "42", 7, "100", today),
        )
        await self.database.commit()
        payload = SimpleNamespace(guild_id=1, message_id=100)

        await self.cog.on_raw_message_delete(payload)

        cursor = await self.database.execute("SELECT COUNT(*) FROM streak_days")
        self.assertEqual((await cursor.fetchone())[0], 0)
        await cursor.close()
        cursor = await self.database.execute("SELECT COUNT(*) FROM streak_milestones")
        self.assertEqual((await cursor.fetchone())[0], 0)
        await cursor.close()


if __name__ == "__main__":
    unittest.main()
