import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.rbac import (
    assign_direct_role,
    initialize_rbac_schema,
    list_audit_events,
    list_roles,
    permissions_for_user,
    record_audit,
    remove_direct_role,
    replace_discord_role_mappings,
    save_custom_role,
    set_user_permission_override,
    set_user_status,
)
from dashboard.users import initialize_dashboard_users, list_dashboard_users, upsert_discord_user


class DashboardRBACTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "owner-password",
                "DASHBOARD_SECRET_KEY": "rbac-test-key",
                "DASHBOARD_AUTH_MODE": "password",
                "DASHBOARD_DISCORD_ALLOWED_USER_IDS": "",
                "DASHBOARD_DISCORD_ALLOWED_ROLE_IDS": "",
                "GUILD_ID": "999999999999999999",
            },
            clear=False,
        )
        self.environment.start()
        initialize_dashboard_users()
        initialize_rbac_schema()
        self.owner = next(item for item in list_dashboard_users() if item["username"] == "owner")

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def role(self, role_key):
        return next(item for item in list_roles() if item["role_key"] == role_key)

    def create_user(self, name="person"):
        with sqlite3.connect(self.database) as connection:
            cursor = connection.execute(
                "INSERT INTO dashboard_users(username, role, status, auth_provider) VALUES (?, 'viewer', 'active', 'password')",
                (name,),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def discord_identity(self, user_id, roles):
        return {
            "id": user_id,
            "username": "mapped-user",
            "global_name": "Mapped User",
            "_guild_id": "999999999999999999",
            "_guild_member": {"roles": roles, "user": {"id": user_id}},
        }

    def test_system_roles_seed_capabilities_and_owner_has_all(self):
        roles = {item["role_key"]: item for item in list_roles()}
        self.assertEqual(set(roles), {"owner", "administrator", "moderator", "party_captain", "viewer"})
        owner_permissions = permissions_for_user(self.owner["id"])
        self.assertIn("access.manage", owner_permissions)
        self.assertIn("bot.restart", owner_permissions)
        self.assertNotIn("access.manage", roles["administrator"]["permissions"])
        self.assertEqual(
            set(roles["viewer"]["permissions"]),
            {"dashboard.view", "analytics.view", "bot.status.view"},
        )
        self.assertIn("events.create", roles["party_captain"]["permissions"])
        self.assertNotIn("ask.view", roles["party_captain"]["permissions"])
        self.assertNotIn("bank.view", roles["party_captain"]["permissions"])

    def test_custom_role_assignment_and_deny_override(self):
        user_id = self.create_user()
        custom_id = save_custom_role(
            role_id=None,
            name="Content Helper",
            description="Can work with reusable messages.",
            permissions=["dashboard.view", "message_studio.view", "message_studio.manage"],
            changed_by="owner",
        )
        assign_direct_role(user_id, custom_id, changed_by="owner")
        self.assertIn("message_studio.manage", permissions_for_user(user_id))
        set_user_permission_override(
            user_id, "message_studio.manage", "deny", changed_by="owner"
        )
        self.assertNotIn("message_studio.manage", permissions_for_user(user_id))
        set_user_permission_override(
            user_id, "message_studio.manage", "inherit", changed_by="owner"
        )
        self.assertIn("message_studio.manage", permissions_for_user(user_id))

    def test_discord_role_mapping_is_revalidated_and_removed_access_is_rejected(self):
        discord_role = "777777777777777777"
        moderator = self.role("moderator")
        replace_discord_role_mappings([discord_role], moderator["id"], changed_by="owner")
        identity = self.discord_identity("555555555555555555", [discord_role])
        user = upsert_discord_user(identity)
        self.assertEqual(user["access_source"], "discord_role")
        self.assertIn("knowledge.view", permissions_for_user(user["id"]))

        replace_discord_role_mappings([], moderator["id"], changed_by="owner")
        with self.assertRaisesRegex(PermissionError, "no longer grant"):
            upsert_discord_user(identity)
        with sqlite3.connect(self.database) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM dashboard_user_role_assignments WHERE user_id = ? AND source = 'discord'",
                (user["id"],),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_removed_direct_allowlist_access_is_rejected(self):
        user_id = "555555555555555555"
        identity = self.discord_identity(user_id, [])
        with patch.dict(
            os.environ, {"DASHBOARD_DISCORD_ALLOWED_USER_IDS": user_id}
        ):
            user = upsert_discord_user(identity)
        self.assertEqual(user["access_source"], "direct")
        with self.assertRaisesRegex(PermissionError, "no longer grant"):
            upsert_discord_user(identity)

    def test_guild_membership_mismatch_fails_closed(self):
        discord_role = "777777777777777777"
        replace_discord_role_mappings([discord_role], self.role("viewer")["id"], changed_by="owner")
        identity = self.discord_identity("555555555555555555", [discord_role])
        identity["_guild_member"]["user"]["id"] = "666666666666666666"
        with self.assertRaisesRegex(PermissionError, "mismatched"):
            upsert_discord_user(identity)

    def test_unverifiable_guild_membership_fails_closed(self):
        identity = {"id": "555555555555555555", "username": "missing-member"}
        with self.assertRaisesRegex(PermissionError, "could not be verified"):
            upsert_discord_user(identity)

    def test_final_owner_cannot_be_removed_disabled_or_denied(self):
        owner_role = self.role("owner")
        with self.assertRaisesRegex(ValueError, "final active owner"):
            remove_direct_role(self.owner["id"], owner_role["id"], changed_by="owner")
        with self.assertRaisesRegex(ValueError, "final active owner"):
            set_user_status(self.owner["id"], "disabled", changed_by="owner")
        with self.assertRaisesRegex(ValueError, "Owner permissions"):
            set_user_permission_override(
                self.owner["id"], "access.manage", "deny", changed_by="owner"
            )

    def test_audit_log_is_append_only_and_redacts_secret_fields(self):
        record_audit(
            actor_label="owner",
            action="configuration.changed",
            target_type="settings_group",
            target_id="ask",
            after={"ASK_MODEL": "gemini", "API_KEY": "do-not-store"},
        )
        event = list_audit_events(action="configuration.changed")[0]
        self.assertIn('"API_KEY": "[redacted]"', event["after_json"])
        self.assertNotIn("do-not-store", event["after_json"])
        with sqlite3.connect(self.database) as connection:
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("UPDATE dashboard_audit_log SET actor_label = 'changed'")
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute("DELETE FROM dashboard_audit_log")


if __name__ == "__main__":
    unittest.main()
