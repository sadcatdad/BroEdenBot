import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite

os.environ.setdefault("DISCORD_TOKEN", "test-token")

from cogs.checklist import (
    ChecklistCog,
    ChecklistItemSelectView,
    PostedChecklistView,
    parse_id_set,
)


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.user = SimpleNamespace(id=999)


class FakeGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.me = object()

    def get_member(self, _user_id):
        return self.me


class FakeChannel:
    id = 123
    mention = "<#123>"

    def __init__(self, guild, **permissions):
        self.guild = guild
        self.permissions = SimpleNamespace(**permissions)

    def permissions_for(self, _member):
        return self.permissions


class FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.guild_id = guild.id


class ChecklistHelperTests(unittest.TestCase):
    def test_id_parser_ignores_invalid_and_nonpositive_values(self):
        self.assertEqual(parse_id_set("12, nope 34 -1 0"), {12, 34})

    def test_item_selector_uses_discord_safe_component_emoji(self):
        view = ChecklistItemSelectView(
            None,
            1,
            [{"id": 2, "content": "Task", "position": 1, "status": "open"}],
            "toggle",
        )
        option = view.to_components()[0]["components"][0]["options"][0]
        self.assertEqual(option["emoji"]["name"], "⬜")

    def test_posted_controls_are_persistent(self):
        view = PostedChecklistView(
            object(),
            {"id": 1, "status": "active"},
        )
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.children), 7)
        self.assertTrue(all(item.custom_id for item in view.children))

    def test_refresh_fallback_command_is_registered(self):
        command_names = {command.name for command in ChecklistCog.checklist.commands}
        self.assertIn("refresh", command_names)


class ChecklistPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_permission_message_names_missing_channel_permission(self):
        cog = ChecklistCog(DummyBot(None))
        guild = FakeGuild()
        interaction = FakeInteraction(guild)
        channel = FakeChannel(
            guild,
            view_channel=True,
            send_messages=False,
            embed_links=True,
        )

        message = cog.permission_failure_message(interaction, channel, action="send")

        self.assertEqual(
            message,
            "I need Send Messages in <#123> to post the checklist.",
        )

    async def test_update_permission_message_names_read_history_requirement(self):
        cog = ChecklistCog(DummyBot(None))
        guild = FakeGuild()
        interaction = FakeInteraction(guild)
        channel = FakeChannel(
            guild,
            view_channel=True,
            read_message_history=False,
            embed_links=True,
        )

        message = cog.permission_failure_message(interaction, channel, action="update")

        self.assertEqual(
            message,
            "I need Read Message History in <#123> to update the checklist post.",
        )


class ChecklistDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        with patch.dict(
            os.environ,
            {
                "CHECKLIST_ALLOWED_ROLE_IDS": "10,20",
                "BOT_OWNER_USER_IDS": "30",
            },
            clear=False,
        ):
            self.cog = ChecklistCog(DummyBot(self.database))
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.database.close()

    async def test_schema_and_rendering_preserve_backend_state(self):
        now = "2026-06-22T17:30:00+00:00"
        cursor = await self.database.execute(
            """
            INSERT INTO checklists (
                guild_id, name, description, created_by_user_id,
                created_by_name, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("1", "Event Prep", "Internal tasks", "30", "Owner", now, now),
        )
        checklist_id = cursor.lastrowid
        await cursor.close()
        await self.database.executemany(
            """
            INSERT INTO checklist_items (
                checklist_id, content, status, position,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (checklist_id, "Book venue", "complete", 1, now, now),
                (checklist_id, "@everyone bring snacks", "open", 2, now, now),
            ],
        )
        await self.database.commit()

        embed = await self.cog.render_checklist(checklist_id)
        self.assertEqual(embed.title, "Checklist: Event Prep")
        self.assertIn("1/2 complete", embed.fields[0].value)
        self.assertIn("☑ ~~Book venue~~", embed.fields[1].value)
        self.assertIn("☐ @everyone bring snacks", embed.fields[1].value)
        self.assertLessEqual(len(embed.fields[1].value), 1_024)

        await self.cog.soft_delete_checklist(checklist_id, 30)
        row = await self.cog.fetch_one(
            "SELECT status, deleted_by_user_id FROM checklists WHERE id = ?",
            (checklist_id,),
        )
        items = await self.cog.fetch_all(
            "SELECT status FROM checklist_items WHERE checklist_id = ?",
            (checklist_id,),
        )
        self.assertEqual(row["status"], "deleted")
        self.assertEqual(row["deleted_by_user_id"], "30")
        self.assertEqual({item["status"] for item in items}, {"deleted"})


if __name__ == "__main__":
    unittest.main()
