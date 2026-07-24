import io
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from dashboard.app import app, required_permission
from dashboard.rbac import initialize_rbac_schema
from dashboard.users import hash_password, initialize_dashboard_users
from utils.brofiles import (
    badge_for_member,
    get_brofile,
    initialize_brofile_schema,
    save_badge_mapping,
)
from utils.discord_metadata import save_discord_metadata_snapshot
from utils.settings import initialize_settings_from_env
from utils.visual_studio.repository import initialize_visual_studio_schema
from utils.visual_studio.storage import archive_asset, get_asset, save_asset


def png(width=1200, height=600, color=(38, 120, 84), alpha=False):
    output = io.BytesIO()
    mode = "RGBA" if alpha else "RGB"
    fill = tuple(color) + (255,) if alpha else color
    Image.new(mode, (width, height), fill).save(output, "PNG")
    return output.getvalue()


class BrofileTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database = root / "data.db"
        self.assets = root / "visual-assets"
        self.guild_id = "999999999999999999"
        self.user_id = "111111111111111111"
        self.role_id = "222222222222222222"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "VISUAL_ASSET_DIR": str(self.assets),
                "GUILD_ID": self.guild_id,
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "owner-password",
                "DASHBOARD_SECRET_KEY": "brofile-test-key",
                "DASHBOARD_AUTH_MODE": "password",
            },
            clear=False,
        )
        self.environment.start()
        initialize_settings_from_env()
        initialize_dashboard_users()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                INSERT INTO dashboard_users (
                    username, password_hash, discord_user_id, discord_username,
                    discord_global_name, discord_avatar, role, status,
                    auth_provider, discord_guild_id, discord_role_ids_json,
                    discord_verification_status, access_source
                ) VALUES (?, ?, ?, ?, ?, ?, 'verified_events_member', 'active',
                          'password', ?, ?, 'not_required', 'legacy')
                """,
                (
                    "garden-member",
                    hash_password("member-password"),
                    self.user_id,
                    "greenbro",
                    "Green BRO",
                    "avatarhash",
                    self.guild_id,
                    '["{}"]'.format(self.role_id),
                ),
            )
            connection.commit()
        initialize_rbac_schema()
        initialize_visual_studio_schema()
        initialize_brofile_schema()
        save_discord_metadata_snapshot(
            guild_id=self.guild_id,
            guild_name="Bro Eden",
            roles=[
                {
                    "id": self.role_id,
                    "name": "Founding BRO",
                    "color": "#7dd3a7",
                    "position": 12,
                    "managed": False,
                    "mentionable": True,
                    "hoist": True,
                    "member_count": 1,
                    "is_bot_role": False,
                }
            ],
            categories=[],
            channels=[],
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def login(self, username="garden-member", password="member-password"):
        page = self.client.get("/login")
        token = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/login",
            data={"username": username, "password": password, "csrf": token},
        )
        self.assertEqual(response.status_code, 200)

    def csrf(self, response):
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    def test_member_can_create_publish_and_browse_brofile(self):
        self.login()
        editor = self.client.get("/my-brofile")
        self.assertEqual(editor.status_code, 200)
        self.assertIn("My <span class=\"bro-mark\">BRO</span>file", editor.text)
        self.assertIn("About me", editor.text)
        self.assertNotIn("Songbook", editor.text)
        self.assertNotIn("Artist Profile", editor.text)

        saved = self.client.post(
            "/my-brofile",
            data={
                "csrf": self.csrf(editor),
                "tagline": "Plant dad and cozy gamer",
                "about": "Here for friendship, game nights, and good conversation.",
                "interests": "Plants, RPGs, cooking",
                "skills": "Illustration and making soup",
                "favorite_things": "Rainy mornings",
                "proudest_moment": "Starting over in a new city",
                "directory_visible": "yes",
                "accent_color": "#6EE7B7",
                "background_color_start": "#10231B",
                "background_color_end": "#17352A",
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertIn("Your BROfile details and colors were saved.", saved.text)
        profile = get_brofile(self.guild_id, self.user_id)
        self.assertEqual(profile["tagline"], "Plant dad and cozy gamer")
        self.assertEqual(profile["directory_visible"], 1)

        directory = self.client.get("/brofiles")
        self.assertEqual(directory.status_code, 200)
        self.assertIn("Green BRO", directory.text)
        self.assertIn("Plant dad and cozy gamer", directory.text)
        public = self.client.get("/brofiles/{}".format(self.user_id))
        self.assertEqual(public.status_code, 200)
        self.assertIn("About this BRO", public.text)
        self.assertIn("A moment I&#39;m proud of".replace("&#39;", "'"), public.text)

    def test_profile_media_is_normalized_and_private_drafts_are_hidden(self):
        self.login()
        editor = self.client.get("/my-brofile")
        upload = self.client.post(
            "/my-brofile/media/banner/upload",
            data={"csrf": self.csrf(editor)},
            files={"image": ("garden.png", png(), "image/png")},
        )
        self.assertEqual(upload.status_code, 200)
        profile = get_brofile(self.guild_id, self.user_id)
        banner = profile["media"]["banner"]
        self.assertEqual((banner["width"], banner["height"]), (1600, 500))
        image = self.client.get(
            "/brofiles/{}/media/banner".format(self.user_id)
        )
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")

        editor = self.client.get("/my-brofile")
        hidden = self.client.post(
            "/my-brofile",
            data={
                "csrf": self.csrf(editor),
                "tagline": "Private draft",
                "about": "",
                "interests": "",
                "skills": "",
                "favorite_things": "",
                "proudest_moment": "",
                "accent_color": "#7DD3A7",
                "background_color_start": "#101A18",
                "background_color_end": "#17231F",
            },
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(
            self.client.get("/brofiles/{}".format(self.user_id)).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/brofiles/{}/media/banner".format(self.user_id)).status_code,
            200,
        )

    def test_role_badge_uses_asset_library_and_blocks_asset_archive(self):
        asset_id, _inspection = save_asset(
            png(512, 512, (220, 180, 60), alpha=True),
            filename="founder.png",
            name="Founding BRO badge",
            asset_type="badge",
            actor="owner",
        )
        mapping = save_badge_mapping(
            self.guild_id,
            role_id=self.role_id,
            label="Founding BRO",
            asset_id=asset_id,
            priority=100,
        )
        badge = badge_for_member(self.guild_id, self.user_id)
        self.assertEqual(badge["id"], mapping["id"])
        self.assertEqual(badge["label"], "Founding BRO")
        self.assertEqual(get_asset(asset_id)["usage_count"], 1)
        with self.assertRaisesRegex(ValueError, "actively referenced"):
            archive_asset(asset_id, "owner")

        self.login()
        image = self.client.get(
            "/brofiles/badges/{}/image".format(mapping["id"])
        )
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/png")

    def test_routes_have_explicit_brofile_permissions(self):
        self.assertEqual(required_permission("/my-brofile", "GET"), "brofiles.edit")
        self.assertEqual(required_permission("/my-brofile", "POST"), "brofiles.edit")
        self.assertEqual(required_permission("/brofiles", "GET"), "brofiles.view")
        self.assertEqual(
            required_permission("/brofiles/badges", "POST"),
            "brofiles.manage",
        )


if __name__ == "__main__":
    unittest.main()
