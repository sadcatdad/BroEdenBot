import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from cogs.disboard_bumps import (
    BUMP_FOOTER,
    BUMP_LEADERBOARD_NAME,
    BUMP_SUBTITLE,
    LEGACY_BUMP_LEADERBOARD_NAME,
    PREVIOUS_BRANDED_BUMP_LEADERBOARD_NAME,
    PREVIOUS_BUMP_LEADERBOARD_NAME,
    DisboardBumps,
)


class DummyRole:
    def __init__(self, role_id, position=1):
        self.id = role_id
        self.position = position
        self.managed = False
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other):
        return self.position >= other.position


class DummyMember:
    def __init__(self, member_id, guild):
        self.id = member_id
        self.guild = guild
        self.bot = False
        self.display_name = "Bumper"
        self.name = "bumper"
        self.mention = f"<@{member_id}>"
        self.display_avatar = SimpleNamespace(
            replace=lambda **_kwargs: SimpleNamespace(url="https://example/avatar.png")
        )
        self.roles = []
        self.add_roles = AsyncMock()
        self.remove_roles = AsyncMock()


class DummyGuild:
    def __init__(self):
        self.id = 1
        self.reward_role = DummyRole(500, 10)
        self.ping_role = DummyRole(600, 10)
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True),
            top_role=DummyRole(999, 100),
        )
        self.members = {}

    def get_member(self, member_id):
        return self.members.get(member_id)

    async def fetch_member(self, member_id):
        return self.members[member_id]

    def get_role(self, role_id):
        if role_id == self.reward_role.id:
            return self.reward_role
        if role_id == self.ping_role.id:
            return self.ping_role
        return None


class DummyChannel:
    def __init__(self, channel_id=99):
        self.id = channel_id
        self.send = AsyncMock(return_value=SimpleNamespace(id=800))


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.guilds = []
        self.guild = None
        self.channel = None

    async def wait_until_ready(self):
        return None

    def get_cog(self, _name):
        return None

    def get_guild(self, guild_id):
        return self.guild if self.guild and self.guild.id == guild_id else None

    def get_channel(self, channel_id):
        return self.channel if self.channel and self.channel.id == channel_id else None

    async def fetch_channel(self, channel_id):
        if self.channel and self.channel.id == channel_id:
            return self.channel
        raise RuntimeError("channel unavailable")


class DisboardBumpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        self.bot = DummyBot(self.database)
        self.cog = DisboardBumps(self.bot)
        await self.cog.cog_load()
        self.cog.weekly_publisher.cancel()
        self.cog.reminder_worker.cancel()
        self.guild = DummyGuild()
        self.member = DummyMember(42, self.guild)
        self.guild.members[42] = self.member
        self.channel = DummyChannel()
        self.bot.guild = self.guild
        self.bot.channel = self.channel

    async def asyncTearDown(self):
        self.cog.weekly_publisher.cancel()
        self.cog.reminder_worker.cancel()
        await self.database.close()

    def message(
        self,
        *,
        message_id=100,
        content="Bump done!",
    ):
        return SimpleNamespace(
            id=message_id,
            guild=self.guild,
            author=SimpleNamespace(id=302),
            channel=self.channel,
            content=content,
            embeds=[],
            created_at=datetime.now(timezone.utc),
            interaction=SimpleNamespace(id=77, name="bump", user=SimpleNamespace(id=42)),
            interaction_metadata=None,
            mentions=[],
            reply=AsyncMock(return_value=SimpleNamespace(id=700)),
        )

    @staticmethod
    def settings(key, default=""):
        return {
            "DISBOARD_BOT_USER_ID": "302",
            "BUMP_REWARD_ROLE_ID": "500",
            "BUMP_PING_ROLE_ID": "600",
        }.get(key, default)

    async def test_verified_bump_awards_points_role_and_record_once(self):
        message = self.message()
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            self.assertTrue(await self.cog._process_bump(message))
            self.assertFalse(await self.cog._process_bump(message))

        cursor = await self.database.execute(
            "SELECT points FROM points WHERE id = ? AND leaderboard = ?",
            (42, BUMP_LEADERBOARD_NAME),
        )
        self.assertEqual((await cursor.fetchone())[0], 1000)
        await cursor.close()
        cursor = await self.database.execute(
            """
            SELECT member_id, channel_id, response_message_id, points_awarded,
                   role_status
            FROM disboard_bump_events
            """
        )
        self.assertEqual(
            await cursor.fetchone(),
            ("42", "99", "100", 1000, "awarded"),
        )
        await cursor.close()
        self.member.add_roles.assert_awaited_once_with(
            self.guild.reward_role,
            reason="Verified DISBOARD bump reward pulse",
        )
        message.reply.assert_awaited_once()
        prompt_args = message.reply.await_args
        self.assertIn("Thanks for bumping our server, <@42>!", prompt_args.args[0])
        self.assertIn("+ 1,000 Bump Points", prompt_args.args[0])
        self.assertIn("reward role was awarded", prompt_args.args[0])
        self.assertNotIn("Riff", prompt_args.args[0])
        self.assertEqual(
            [item.label for item in prompt_args.kwargs["view"].children],
            ["Bump Leaderboard"],
        )
        cursor = await self.database.execute(
            """
            SELECT status, prompt_message_id
            FROM disboard_bump_reminders
            WHERE response_message_id = '100'
            """
        )
        self.assertEqual(await cursor.fetchone(), ("scheduled", "700"))
        await cursor.close()

    async def test_success_response_uses_selected_asset_and_four_template_buttons(self):
        message = self.message(message_id=107)

        payload = {
            "content": "Great bump, {user.feature}! You earned {points} points.\n{reward_status}\nRole: {role.feature}",
            "embeds": [
                {
                    "title": "Bump successful",
                    "description": "Your reward has been recorded.",
                    "color": "#f0319b",
                    "fields": [],
                },
                {
                    "title": "Next bump",
                    "description": "Come back in two hours.",
                    "color": "#25b8b8",
                    "fields": [],
                },
            ],
            "buttons": [
                {
                    "label": f"Template {index}",
                    "action": "url",
                    "style": "link",
                    "url": f"https://example.com/{index}",
                }
                for index in range(1, 6)
            ],
        }
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
            patch.object(
                self.cog,
                "_configured_success_payload",
                new=AsyncMock(return_value=payload),
            ),
        ):
            self.assertTrue(await self.cog._process_bump(message))

        prompt_args = message.reply.await_args
        self.assertEqual(
            prompt_args.args[0],
            "Great bump, <@42>! You earned 1,000 points.\n"
            "- Your configured bump reward role was awarded\nRole: <@&500>",
        )
        self.assertEqual(prompt_args.kwargs["embeds"][0].title, "Bump successful")
        self.assertEqual(prompt_args.kwargs["embeds"][1].title, "Next bump")
        self.assertEqual(
            [item.label for item in prompt_args.kwargs["view"].children],
            ["Template 1", "Template 2", "Template 3", "Template 4", "Bump Leaderboard"],
        )

    async def test_failed_reward_role_is_reported_without_losing_points(self):
        message = self.message(message_id=103)
        def missing_role_settings(key, default=""):
            if key == "BUMP_REWARD_ROLE_ID":
                return "999"
            return self.settings(key, default)

        with (
            patch(
                "cogs.disboard_bumps.get_setting",
                side_effect=missing_role_settings,
            ),
            patch("cogs.disboard_bumps.get_int_setting", return_value=250),
        ):
            self.assertTrue(await self.cog._process_bump(message))

        prompt = message.reply.await_args.args[0]
        self.assertIn("+ 250 Bump Points", prompt)
        self.assertIn("staff can check the reward role setup", prompt)
        cursor = await self.database.execute(
            "SELECT points FROM points WHERE id = ? AND leaderboard = ?",
            (42, BUMP_LEADERBOARD_NAME),
        )
        self.assertEqual((await cursor.fetchone())[0], 250)
        await cursor.close()

    async def test_legacy_leaderboard_schema_is_upgraded_before_insert(self):
        database = await aiosqlite.connect(":memory:")
        await database.execute(
            "CREATE TABLE leaderboards (name TEXT PRIMARY KEY)"
        )
        await database.commit()
        bot = DummyBot(database)
        cog = DisboardBumps(bot)
        await cog.cog_load()
        cog.weekly_publisher.cancel()
        cog.reminder_worker.cancel()
        cursor = await database.execute("PRAGMA table_info(leaderboards)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        self.assertTrue(
            {"header", "description", "image_url", "image_data", "accent_color"}
            <= columns
        )
        await database.close()

    async def test_yes_choice_is_private_and_persistently_schedules_reminder(self):
        message = self.message()
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            await self.cog._process_bump(message)
        await self.database.execute(
            "UPDATE disboard_bump_reminders SET status = 'pending_choice' "
            "WHERE response_message_id = '100'"
        )
        await self.database.commit()
        interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            response=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        before = datetime.now(timezone.utc)
        await self.cog._handle_reminder_choice(interaction, "yes", "100")

        interaction.response.send_message.assert_awaited_once_with(
            "✅ Great! We will remind you in 2 hours.", ephemeral=True,
        )
        cursor = await self.database.execute(
            "SELECT status, due_at FROM disboard_bump_reminders WHERE response_message_id = '100'"
        )
        status, due_at = await cursor.fetchone()
        await cursor.close()
        self.assertEqual(status, "scheduled")
        self.assertGreaterEqual(
            datetime.fromisoformat(due_at), before + timedelta(hours=2),
        )
        edited_view = interaction.message.edit.await_args.kwargs["view"]
        self.assertFalse(edited_view.children[0].disabled)

    async def test_other_member_cannot_choose_bumpers_reminder(self):
        message = self.message()
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            await self.cog._process_bump(message)
        interaction = SimpleNamespace(
            guild=self.guild,
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        await self.cog._handle_reminder_choice(interaction, "yes", "100")
        interaction.response.send_message.assert_awaited_once_with(
            "Only the member who bumped can choose this reminder.", ephemeral=True,
        )
        cursor = await self.database.execute(
            "SELECT status FROM disboard_bump_reminders WHERE response_message_id = '100'"
        )
        self.assertEqual((await cursor.fetchone())[0], "scheduled")
        await cursor.close()

    async def test_no_choice_declines_without_scheduling(self):
        message = self.message()
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            await self.cog._process_bump(message)
        await self.database.execute(
            "UPDATE disboard_bump_reminders SET status = 'pending_choice' "
            "WHERE response_message_id = '100'"
        )
        await self.database.commit()
        interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            response=SimpleNamespace(send_message=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        await self.cog._handle_reminder_choice(interaction, "no", "100")
        interaction.response.send_message.assert_awaited_once_with(
            "❤️ No problem! You can also ignore voting to default to No.",
            ephemeral=True,
        )
        cursor = await self.database.execute(
            "SELECT status FROM disboard_bump_reminders WHERE response_message_id = '100'"
        )
        self.assertEqual((await cursor.fetchone())[0], "declined")
        await cursor.close()

    async def test_prompt_leaderboard_button_publishes_only_once(self):
        message = self.message()
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            await self.cog._process_bump(message)
        interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        graphic = (
            SimpleNamespace(filename="bump-champions.png"),
            SimpleNamespace(children=[]),
        )
        with patch.object(self.cog, "_graphic_page", new=AsyncMock(return_value=graphic)):
            await self.cog._handle_prompt_leaderboard(interaction, "100")

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            file=graphic[0], view=graphic[1],
        )
        edited_view = interaction.message.edit.await_args.kwargs["view"]
        self.assertTrue(edited_view.children[0].disabled)

        second = SimpleNamespace(
            guild=self.guild,
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(edit=AsyncMock()),
        )
        await self.cog._handle_prompt_leaderboard(second, "100")
        second.response.send_message.assert_awaited_once_with(
            "The Bump Leaderboard button has already been used.", ephemeral=True,
        )
        second.response.defer.assert_not_awaited()

    async def test_due_reminder_pings_member_and_configured_role(self):
        message = self.message()
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            await self.cog._process_bump(message)
            await self.database.execute(
                """
                UPDATE disboard_bump_reminders
                SET status = 'scheduled', due_at = ?
                WHERE response_message_id = '100'
                """,
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
            )
            await self.database.commit()
            await self.cog._process_due_reminders()

        self.channel.send.assert_awaited_once()
        send_args = self.channel.send.await_args
        self.assertEqual(send_args.args[0], "<@&600>")
        self.assertIn("BUMP TIME", send_args.kwargs["embeds"][0].description)
        self.assertIsNone(send_args.kwargs["view"])
        cursor = await self.database.execute(
            "SELECT status, reminder_message_id FROM disboard_bump_reminders WHERE response_message_id = '100'"
        )
        self.assertEqual(await cursor.fetchone(), ("sent", "800"))
        await cursor.close()

    async def test_due_reminder_uses_selected_asset_content_embed_and_buttons(self):
        message = self.message(message_id=106)

        payload = {
            "content": "Reminder for {user.feature}: {role.feature}",
            "embed": {
                "title": "Custom bump reminder",
                "description": "Use `/bump` now.",
                "color": "#f0319b",
                "fields": [],
            },
            "buttons": [
                {
                    "label": "Template link",
                    "action": "url",
                    "style": "link",
                    "url": "https://example.com/",
                }
            ],
        }
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
            patch.object(
                self.cog,
                "_configured_reminder_payload",
                new=AsyncMock(return_value=payload),
            ),
        ):
            await self.cog._process_bump(message)
            await self.database.execute(
                "UPDATE disboard_bump_reminders SET due_at = ? WHERE response_message_id = '106'",
                ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
            )
            await self.database.commit()
            await self.cog._process_due_reminders()

        send_args = self.channel.send.await_args
        self.assertEqual(send_args.args[0], "Reminder for <@42>: <@&600>")
        self.assertEqual(send_args.kwargs["embeds"][0].title, "Custom bump reminder")
        self.assertEqual(
            [button.label for button in send_args.kwargs["view"].children],
            ["Template link"],
        )
        self.assertEqual(send_args.kwargs["view"].children[0].url, "https://example.com/")

    async def test_bump_message_never_falls_back_to_template_content(self):
        with patch("cogs.disboard_bumps.get_setting", return_value=""):
            content = self.cog._reminder_content(self.member, self.guild.ping_role)
        self.assertEqual(content, self.guild.ping_role.mention)

    async def test_embed_role_button_assigns_manageable_role_privately(self):
        interaction = SimpleNamespace(
            guild=self.guild,
            user=self.member,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch("cogs.disboard_bumps.discord.Member", DummyMember):
            await self.cog._handle_embed_role(interaction, "add", 600)
        self.member.add_roles.assert_awaited_once_with(
            self.guild.ping_role,
            reason="Self-service embed role button",
        )
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])

    async def test_untrusted_or_unsuccessful_message_is_ignored(self):
        with patch("cogs.disboard_bumps.get_setting", side_effect=self.settings):
            self.assertFalse(
                await self.cog._process_bump(self.message(content="Try again later"))
            )
            untrusted = self.message(message_id=101)
            untrusted.author.id = 999
            self.assertFalse(await self.cog._process_bump(untrusted))

    async def test_verified_bump_can_resolve_member_from_disboard_mention(self):
        message = self.message(message_id=102)
        message.interaction = None
        message.interaction_metadata = None
        message.mentions = [self.member]
        with (
            patch("cogs.disboard_bumps.discord.Member", DummyMember),
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            self.assertTrue(await self.cog._process_bump(message))

    async def test_modern_interaction_metadata_registers_only_bump_command(self):
        message = self.message(message_id=104)
        message.interaction = None
        message.interaction_metadata = SimpleNamespace(
            id=88,
            name="bump",
            user=SimpleNamespace(id=42),
        )
        with (
            patch("cogs.disboard_bumps.get_setting", side_effect=self.settings),
            patch("cogs.disboard_bumps.get_int_setting", return_value=1000),
        ):
            self.assertTrue(await self.cog._process_bump(message))
            wrong_command = self.message(message_id=105)
            wrong_command.interaction = None
            wrong_command.interaction_metadata = SimpleNamespace(
                id=89,
                name="help",
                user=SimpleNamespace(id=42),
            )
            self.assertFalse(await self.cog._process_bump(wrong_command))

        cursor = await self.database.execute(
            "SELECT points FROM points WHERE id = ? AND leaderboard = ?",
            (42, BUMP_LEADERBOARD_NAME),
        )
        self.assertEqual((await cursor.fetchone())[0], 1000)
        await cursor.close()

    async def test_member_leave_removes_bump_champions_points(self):
        await self.database.execute(
            "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
            (42, BUMP_LEADERBOARD_NAME, 1000),
        )
        await self.database.commit()

        await self.cog.on_member_remove(self.member)

        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM points WHERE id = ? AND leaderboard = ?",
            (42, BUMP_LEADERBOARD_NAME),
        )
        self.assertEqual((await cursor.fetchone())[0], 0)
        await cursor.close()

    async def test_legacy_named_leaderboards_are_preserved_during_install(self):
        await self.database.execute(
            "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
            (42, LEGACY_BUMP_LEADERBOARD_NAME, 2000),
        )
        await self.database.execute(
            "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
            (42, BUMP_LEADERBOARD_NAME, 1000),
        )
        await self.database.execute(
            "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
            (42, PREVIOUS_BUMP_LEADERBOARD_NAME, 500),
        )
        await self.database.execute(
            "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
            (42, PREVIOUS_BRANDED_BUMP_LEADERBOARD_NAME, 250),
        )
        await self.database.execute(
            """
            INSERT INTO leaderboards (name, header, description, accent_color)
            VALUES (?, ?, ?, ?)
            """,
            (LEGACY_BUMP_LEADERBOARD_NAME, "Leaderboard", "Old", "auto"),
        )
        await self.database.commit()

        await self.cog._migrate_legacy_leaderboard_name()

        cursor = await self.database.execute(
            "SELECT points FROM points WHERE id = ? AND leaderboard = ?",
            (42, BUMP_LEADERBOARD_NAME),
        )
        self.assertEqual((await cursor.fetchone())[0], 1000)
        await cursor.close()
        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM points WHERE leaderboard = ?",
            (LEGACY_BUMP_LEADERBOARD_NAME,),
        )
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()
        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM points WHERE leaderboard = ?",
            (PREVIOUS_BRANDED_BUMP_LEADERBOARD_NAME,),
        )
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()
        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM points WHERE leaderboard = ?",
            (PREVIOUS_BUMP_LEADERBOARD_NAME,),
        )
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()
        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM leaderboards WHERE name = ?",
            (LEGACY_BUMP_LEADERBOARD_NAME,),
        )
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()

    async def test_internal_leaderboard_name_uses_crown_branding(self):
        self.assertEqual(BUMP_LEADERBOARD_NAME, "👑 BUMP LEGENDS 👑")
        cursor = await self.database.execute(
            "SELECT COUNT(*) FROM leaderboards WHERE name = ?",
            (BUMP_LEADERBOARD_NAME,),
        )
        self.assertEqual((await cursor.fetchone())[0], 1)
        await cursor.close()

    async def test_graphic_pages_ten_members_at_a_time(self):
        for member_id in range(1, 46):
            self.guild.members[member_id] = DummyMember(member_id, self.guild)
            await self.database.execute(
                "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
                (member_id, BUMP_LEADERBOARD_NAME, 1000 - member_id),
            )
        await self.database.commit()

        with (
            patch(
                "cogs.disboard_bumps.render_ranked_graphic",
                new=AsyncMock(return_value=b"png"),
            ) as render,
            patch(
                "cogs.disboard_bumps._bump_background_bytes",
                return_value=b"background",
            ),
        ):
            _file, view = await self.cog._graphic_page(self.guild, page=1)

        section = render.await_args.kwargs["sections"][0]
        self.assertEqual(
            render.await_args.kwargs["title"],
            "👑 Bump Legends Leaderboard",
        )
        self.assertEqual(render.await_args.kwargs["subtitle"], BUMP_SUBTITLE)
        self.assertEqual(render.await_args.kwargs["footer_text"], BUMP_FOOTER)
        self.assertEqual(render.await_args.kwargs["background_bytes"], b"background")
        self.assertEqual(render.await_args.kwargs["accent_color"], 0x25B8B8)
        self.assertEqual(len(section.items), 10)
        self.assertEqual(section.rank_start, 11)
        self.assertEqual([item.label for item in view.children], [
            "Previous", "Page 2 of 5", "Next",
        ])

    async def test_graphic_subtitle_uses_configured_points_value(self):
        with (
            patch("cogs.disboard_bumps.get_int_setting", return_value=250),
            patch(
                "cogs.disboard_bumps.render_ranked_graphic",
                new=AsyncMock(return_value=b"png"),
            ) as render,
        ):
            await self.cog._graphic_page(self.guild, page=0)

        self.assertIn("1 bump = 250 points", render.await_args.kwargs["subtitle"])

    async def test_managed_renderer_reuses_bumpscores_graphic_page(self):
        expected = (SimpleNamespace(filename="bump-champions.png"), object())
        with patch.object(
            self.cog,
            "_graphic_page",
            new=AsyncMock(return_value=expected),
        ) as graphic_page:
            actual = await self.cog.render_managed_leaderboard_page(
                self.guild,
                1,
            )

        self.assertIs(actual, expected)
        graphic_page.assert_awaited_once_with(self.guild, 1)


if __name__ == "__main__":
    unittest.main()
