import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.embed_templates import (
    delete_embed_template,
    discord_embed_from_payload,
    discord_view_from_payload,
    get_embed_template,
    list_embed_templates,
    render_feature_payload,
    save_embed_template,
    validate_asset_payload,
    validate_embed_payload,
)
from utils.settings import initialize_settings_from_env, set_setting


ROLE_ID = "111111111111111111"


def sample_payload():
    return {
        "content": "Reminder {role}",
        "embed": {
            "author_name": "Bro Eden",
            "title": "Bump time",
            "description": "Use `/bump` to support the server.",
            "color": "#25b8b8",
            "fields": [
                {"name": "Reward", "value": "1,000 points", "inline": True}
            ],
        },
        "buttons": [
            {
                "label": "Get reminders",
                "emoji": "🔔",
                "style": "success",
                "action": "add_role",
                "role_id": ROLE_ID,
            },
            {
                "label": "DISBOARD",
                "style": "secondary",
                "action": "url",
                "url": "https://disboard.org/",
            },
        ],
    }


class EmbedTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "data.db"
        self.environment = patch.dict(
            os.environ,
            {"DATABASE_PATH": str(self.database)},
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_saved_templates_are_searchable_sortable_and_renderable(self):
        template_id = save_embed_template(
            name="Bump Reminder",
            payload_json=json.dumps(sample_payload()),
            updated_by="owner",
        )
        saved = get_embed_template(template_id)
        self.assertEqual(saved["name"], "Bump Reminder")
        self.assertEqual(saved["payload"]["embed"]["fields"][0]["name"], "Reward")
        self.assertEqual(list_embed_templates("bump", "name", "asc")[0]["id"], template_id)

        embed = discord_embed_from_payload(saved["payload"])
        self.assertEqual(embed.title, "Bump time")
        self.assertEqual(embed.fields[0].value, "1,000 points")
        self.assertEqual(saved["asset_type"], "embed")
        view = discord_view_from_payload(saved["payload"])
        self.assertEqual(
            [button.label for button in view.children],
            ["Get reminders", "DISBOARD"],
        )
        self.assertEqual(view.children[0].custom_id, f"embedrole|add|{ROLE_ID}")

    def test_existing_embed_rows_migrate_to_embed_asset_type(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE embed_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT 'dashboard'
            )
            """
        )
        connection.execute(
            "INSERT INTO embed_templates (name, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("Legacy Embed", json.dumps(sample_payload()), "now", "now"),
        )
        connection.commit()
        connection.close()

        migrated = get_embed_template(1)
        self.assertEqual(migrated["asset_type"], "embed")

    def test_message_assets_and_feature_placeholders_share_the_asset_pipeline(self):
        payload = sample_payload()
        payload["content"] = "Hello {user.feature} — ping {role.feature}: {points}"
        payload["embed"] = {}
        message_id = save_embed_template(
            name="Bump Confirmation Message",
            asset_type="message",
            payload_json=json.dumps(payload),
            updated_by="owner",
        )
        saved = get_embed_template(message_id)
        self.assertEqual(saved["asset_type"], "message")
        rendered = render_feature_payload(
            saved["payload"],
            user_mention="<@42>",
            role_mentions=["<@&10>", "<@&20>"],
            placeholders={"points": "1,000"},
        )
        self.assertEqual(
            rendered["content"],
            "Hello <@42> — ping <@&10> <@&20>: 1,000",
        )
        self.assertEqual(list_embed_templates(sort="type", order="asc")[0]["asset_type"], "message")

        embed_payload = sample_payload()
        embed_payload["embed"]["description"] = "Triggered by {user.feature} for {role.feature}"
        embed_payload["embed"]["fields"][0]["value"] = "Award: {points}"
        embed_payload["buttons"][0]["label"] = "Help {user.feature}"
        rendered_embed = render_feature_payload(
            embed_payload,
            user_mention="<@42>",
            role_mentions=["<@&10>"],
            placeholders={"points": "1,000"},
        )
        self.assertEqual(
            rendered_embed["embed"]["description"],
            "Triggered by <@42> for <@&10>",
        )
        self.assertEqual(rendered_embed["embed"]["fields"][0]["value"], "Award: 1,000")
        self.assertEqual(rendered_embed["buttons"][0]["label"], "Help <@42>")

        invalid = sample_payload()
        with self.assertRaisesRegex(ValueError, "cannot contain embed fields"):
            validate_asset_payload(invalid, "message")

    def test_used_template_cannot_be_deleted(self):
        template_id = save_embed_template(
            name="In Use",
            payload_json=json.dumps(sample_payload()),
            updated_by="owner",
        )
        set_setting("BUMP_REMINDER_ASSET_ID", str(template_id), changed_by="owner")
        set_setting("BUMP_SUCCESS_ASSET_ID", str(template_id), changed_by="owner")
        set_setting("STREAK_MILESTONE_ASSET_ID", str(template_id), changed_by="owner")
        self.assertEqual(
            list_embed_templates("In Use")[0]["features"],
            ["Successful bump response", "Bump reminders", "Streak milestones"],
        )
        with self.assertRaisesRegex(
            ValueError,
            "Successful bump response, Bump reminders, Streak milestones",
        ):
            delete_embed_template(template_id)
        self.assertEqual(get_embed_template(template_id)["name"], "In Use")

    def test_discord_limits_and_role_targets_are_validated(self):
        payload = sample_payload()
        payload["embed"]["description"] = "x" * 4097
        with self.assertRaisesRegex(ValueError, "4,096"):
            validate_embed_payload(payload)
        payload = sample_payload()
        payload["buttons"][0]["role_id"] = "123"
        with self.assertRaisesRegex(ValueError, "Discord role"):
            validate_embed_payload(payload)
