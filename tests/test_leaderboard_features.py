import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from cogs.leaderboards import (
    Leaderboards,
    PAGE_SIZE,
    normalize_accent_color,
)


class DummyRole:
    def __init__(self, role_id, position=10):
        self.id = role_id
        self.position = position
        self.managed = False
        self.members = []
        self.mention = f"<@&{role_id}>"

    def __ge__(self, other):
        return self.position >= other.position


class DummyMember:
    def __init__(self, member_id, roles=None, position=50):
        self.id = member_id
        self.roles = list(roles or [])
        self.bot = False
        self.display_name = f"Member {member_id}"
        self.name = f"member{member_id}"
        self.display_avatar = SimpleNamespace(
            replace=lambda **_kwargs: SimpleNamespace(
                url=f"https://example.com/{member_id}.png"
            )
        )
        self.top_role = DummyRole(member_id + 1000, position)

    async def add_roles(self, role, *, reason=None):
        if role not in self.roles:
            self.roles.append(role)
            role.members.append(self)

    async def remove_roles(self, role, *, reason=None):
        if role in self.roles:
            self.roles.remove(role)
        if self in role.members:
            role.members.remove(self)


class DummyGuild:
    def __init__(self, member, reward_role):
        self.id = 123
        self.owner_id = 1
        self.default_role = DummyRole(123, 0)
        self.me = DummyMember(999, position=100)
        self.me.guild_permissions = SimpleNamespace(manage_roles=True)
        self._members = {member.id: member}
        self._roles = {reward_role.id: reward_role}

    def get_member(self, member_id):
        return self._members.get(member_id)

    def get_role(self, role_id):
        return self._roles.get(role_id)


class DummyBot:
    def __init__(self, database):
        self.db = database
        self.users = {}

    def get_user(self, user_id):
        return self.users.get(user_id)

    def get_cog(self, _name):
        return None


class LeaderboardFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = await aiosqlite.connect(":memory:")
        await self.database.execute(
            "CREATE TABLE leaderboards (name TEXT PRIMARY KEY)"
        )
        self.bot = DummyBot(self.database)
        self.cog = Leaderboards(self.bot)
        await self.cog.cog_load()

    async def asyncTearDown(self):
        await self.database.close()

    async def test_schema_adds_presentation_and_milestone_columns(self):
        cursor = await self.database.execute("PRAGMA table_info(leaderboards)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        self.assertTrue(
            {"header", "description", "image_url", "image_data", "accent_color"}
            <= columns
        )
        cursor = await self.database.execute(
            "PRAGMA table_info(leaderboard_role_milestones)"
        )
        milestone_columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        self.assertTrue(
            {"guild_id", "leaderboard", "role_id", "threshold"}
            <= milestone_columns
        )

    async def test_banner_configuration_is_persisted_and_rendered(self):
        created = await self.cog._save_leaderboard_presentation(
            name="Weekly Wins",
            header="Leaderboard",
            description="This week's champions",
            image_url="https://example.com/banner.png",
            image_data=b"banner-bytes",
            accent_color="#F97316",
            editing=False,
        )
        self.assertTrue(created)
        for user_id in range(1, 13):
            self.bot.users[user_id] = DummyMember(user_id)
            await self.database.execute(
                "INSERT INTO points (id, leaderboard, points) VALUES (?, ?, ?)",
                (user_id, "Weekly Wins", 100 - user_id),
            )
        await self.database.commit()

        with patch(
            "cogs.leaderboards.render_ranked_graphic",
            new=AsyncMock(return_value=b"png"),
        ) as render:
            file, view = await self.cog.get_leaderboard_banner("Weekly Wins", 1)

        call = render.await_args.kwargs
        self.assertEqual(PAGE_SIZE, 10)
        self.assertEqual(file.filename, "leaderboard.png")
        self.assertEqual(call["banner_bytes"], b"banner-bytes")
        self.assertEqual(call["accent_color"], 0xF97316)
        self.assertEqual(call["sections"][0].rank_start, 11)
        self.assertEqual(len(call["sections"][0].items), 2)
        self.assertEqual(view.children[1].label, "Page 2 of 2")

    async def test_milestone_role_tracks_points_in_both_directions(self):
        await self.database.execute(
            "INSERT INTO leaderboards (name, header) VALUES ('Blitz', 'Leaderboard')"
        )
        await self.database.execute(
            "INSERT INTO points (id, leaderboard, points) VALUES (42, 'Blitz', 100)"
        )
        await self.database.execute(
            """
            INSERT INTO leaderboard_role_milestones (
                guild_id, leaderboard, role_id, threshold, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("123", "Blitz", "500", 75, datetime.now(timezone.utc).isoformat()),
        )
        await self.database.commit()
        member = DummyMember(42)
        role = DummyRole(500)
        guild = DummyGuild(member, role)

        result = await self.cog._reconcile_member_milestones(guild, member, "Blitz")
        self.assertEqual(result["added"], 1)
        self.assertIn(role, member.roles)

        await self.database.execute(
            "UPDATE points SET points = 50 WHERE id = 42 AND leaderboard = 'Blitz'"
        )
        await self.database.commit()
        result = await self.cog._reconcile_member_milestones(guild, member, "Blitz")
        self.assertEqual(result["removed"], 1)
        self.assertNotIn(role, member.roles)

    def test_commands_and_accent_validation_are_registered(self):
        names = {command.name for command in Leaderboards.leaderboard.commands}
        self.assertTrue({"create", "edit", "delete", "reset", "add", "remove"} <= names)
        self.assertEqual(
            {command.name for command in Leaderboards.leaderboard_roles.commands},
            {"add", "remove", "list", "sync"},
        )
        self.assertEqual(normalize_accent_color("#f97316"), "#F97316")
        self.assertEqual(normalize_accent_color("auto"), "auto")
        self.assertIsNone(normalize_accent_color("purple"))


if __name__ == "__main__":
    unittest.main()
