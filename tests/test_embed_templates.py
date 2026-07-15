import json
import os
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
    save_embed_template,
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
        view = discord_view_from_payload(saved["payload"], subscribe_role_id=int(ROLE_ID))
        self.assertEqual(
            [button.label for button in view.children],
            ["Get reminders", "DISBOARD", "Subscribe to Bump Reminders"],
        )
        self.assertEqual(view.children[-1].custom_id, f"embedrole|add|{ROLE_ID}")

    def test_used_template_cannot_be_deleted(self):
        template_id = save_embed_template(
            name="In Use",
            payload_json=json.dumps(sample_payload()),
            updated_by="owner",
        )
        set_setting("BUMP_REMINDER_EMBED_ID", str(template_id), changed_by="owner")
        with self.assertRaisesRegex(ValueError, "Bump reminders"):
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

