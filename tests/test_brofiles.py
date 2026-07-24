import asyncio
import io
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from PIL import Image

from dashboard.app import app, required_permission
from dashboard.rbac import initialize_rbac_schema
from dashboard.users import hash_password, initialize_dashboard_users
from utils.brofiles import (
    badge_for_member,
    delete_brofile,
    get_brofile,
    initialize_brofile_schema,
    list_brofiles_for_management,
    media_path,
    save_badge_mapping,
    save_brofile_media,
    update_brofile,
)
from utils.brofile_storage import (
    claim_storage_job,
    complete_upload_job,
    pending_storage_jobs,
    queue_media_upload,
    record_storage_receipt,
)
from utils.discord_metadata import save_discord_metadata_snapshot
from utils.settings import get_setting, initialize_settings_from_env
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
                "BROFILE_ASSET_STORAGE_THREAD_ID": "333333333333333333",
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

    def seed_profile(self, suffix=0, *, directory_visible=True):
        user_id = str(444444444444444440 + suffix)
        return update_brofile(
            self.guild_id,
            user_id,
            identity={
                "user_id": user_id,
                "username": "member{:02d}".format(suffix),
                "display_name": "Garden Member {:02d}".format(suffix),
                "avatar_url": "",
            },
            tagline="Profile number {:02d}".format(suffix),
            about="Member profile",
            interests="Community",
            skills="Helping",
            favorite_things="The Garden",
            proudest_moment="Joining",
            directory_visible=directory_visible,
            accent_color="#7DD3A7",
            background_color_start="#101A18",
            background_color_end="#17231F",
        )

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
        self.assertIn('About this <span class="bro-mark">BRO</span>', public.text)
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
        self.assertEqual(
            required_permission("/brofiles/manage", "GET"),
            "brofiles.manage",
        )

    def test_management_is_role_gated_searchable_and_paged_by_fifteen(self):
        for suffix in range(17):
            self.seed_profile(suffix)

        self.login()
        denied = self.client.get("/brofiles/manage")
        self.assertEqual(denied.status_code, 403)

        self.login("owner", "owner-password")
        first_page = self.client.get("/brofiles/manage")
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.text.count('class="brofile-management-row"'), 15)
        self.assertIn('class="bro-mark">BRO</span>file Management', first_page.text)
        self.assertIn("<strong>BRO EDEN</strong>", first_page.text)
        self.assertIn(
            '<span class="bro-mark">BRO</span>file badge mappings',
            first_page.text,
        )
        storage_saved = self.client.post(
            "/brofiles/manage/storage",
            data={
                "csrf": self.csrf(first_page),
                "storage_thread_id": "777777777777777777",
            },
        )
        self.assertEqual(storage_saved.status_code, 200)
        self.assertEqual(
            get_setting("BROFILE_ASSET_STORAGE_THREAD_ID"),
            "777777777777777777",
        )
        directory = self.client.get("/brofiles")
        self.assertNotIn('id="badge-mappings"', directory.text)

        second_page = self.client.get("/brofiles/manage?page=2")
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.text.count('class="brofile-management-row"'), 2)
        searched = self.client.get("/brofiles/manage?q=Member+07")
        self.assertEqual(searched.status_code, 200)
        self.assertIn("Garden Member 07", searched.text)
        self.assertNotIn("Garden Member 08", searched.text)
        page_data = list_brofiles_for_management(
            self.guild_id,
            query="member07",
            page=1,
            page_size=15,
        )
        self.assertEqual(page_data["total"], 1)

    def test_staff_hide_restore_and_typed_delete_confirmation(self):
        profile = self.seed_profile(30)
        user_id = str(profile["user_id"])
        self.login("owner", "owner-password")
        management = self.client.get("/brofiles/manage?q=Member+30")
        token = self.csrf(management)

        hidden = self.client.post(
            "/brofiles/manage/{}/visibility".format(user_id),
            data={
                "csrf": token,
                "hidden": "yes",
                "return_query": "Member 30",
                "return_page": "1",
            },
        )
        self.assertEqual(hidden.status_code, 200)
        self.assertIn("BROfile hidden from the member directory.", hidden.text)
        self.assertIsNotNone(get_brofile(self.guild_id, user_id)["moderation_hidden_at"])
        self.assertEqual(
            self.client.get("/brofiles/{}".format(user_id)).status_code,
            404,
        )

        token = self.csrf(hidden)
        restored = self.client.post(
            "/brofiles/manage/{}/visibility".format(user_id),
            data={
                "csrf": token,
                "hidden": "no",
                "return_query": "Member 30",
                "return_page": "1",
            },
        )
        self.assertEqual(restored.status_code, 200)
        self.assertIsNone(get_brofile(self.guild_id, user_id)["moderation_hidden_at"])
        self.assertEqual(
            self.client.get("/brofiles/{}".format(user_id)).status_code,
            200,
        )

        confirmation = self.client.get(
            "/brofiles/manage/{}/delete".format(user_id)
        )
        self.assertIn("Type <code>DELETE</code> to confirm", confirmation.text)
        wrong = self.client.post(
            "/brofiles/manage/{}/delete".format(user_id),
            data={
                "csrf": self.csrf(confirmation),
                "confirmation": "delete",
                "return_page": "1",
            },
        )
        self.assertEqual(wrong.status_code, 200)
        self.assertIn("Type DELETE exactly", wrong.text)
        self.assertIsNotNone(get_brofile(self.guild_id, user_id))

        deleted = self.client.post(
            "/brofiles/manage/{}/delete".format(user_id),
            data={
                "csrf": self.csrf(wrong),
                "confirmation": "DELETE",
                "return_page": "1",
            },
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertIn("BROfile permanently deleted.", deleted.text)
        self.assertIsNone(get_brofile(self.guild_id, user_id))

    def test_profile_media_queues_discord_storage_and_remote_cleanup(self):
        profile = self.seed_profile(40)
        user_id = str(profile["user_id"])
        profile = save_brofile_media(
            self.guild_id,
            user_id,
            "banner",
            data=png(),
            filename="banner.png",
            uploaded_by="owner",
            identity={
                "user_id": user_id,
                "username": "member40",
                "display_name": "Garden Member 40",
                "avatar_url": "",
            },
        )
        media = profile["media"]["banner"]
        local_path = media_path(media["storage_key"])
        job_id = queue_media_upload(
            int(media["id"]),
            "owner",
            "333333333333333333",
        )
        record_storage_receipt(
            job_id,
            storage_thread_id="333333333333333333",
            message_id="555555555555555555",
            attachment_url="https://cdn.discordapp.com/attachments/profile.png",
        )
        complete_upload_job(job_id, int(media["id"]))
        self.assertEqual(
            get_brofile(self.guild_id, user_id)["media"]["banner"][
                "discord_sync_status"
            ],
            "ready",
        )

        removed = delete_brofile(
            self.guild_id,
            user_id,
            changed_by="owner",
        )
        self.assertIsNotNone(removed)
        self.assertFalse(local_path.exists())
        delete_jobs = [
            job
            for job in pending_storage_jobs()
            if job["action"] == "delete"
        ]
        self.assertEqual(len(delete_jobs), 1)
        self.assertEqual(
            delete_jobs[0]["message_id"],
            "555555555555555555",
        )

    def test_live_worker_posts_brofile_media_to_configured_thread(self):
        from cogs.visual_assets import VisualAssetDiscordStorage

        profile = self.seed_profile(50)
        user_id = str(profile["user_id"])
        profile = save_brofile_media(
            self.guild_id,
            user_id,
            "spotlight",
            data=png(900, 900),
            filename="spotlight.png",
            uploaded_by="owner",
            identity={
                "user_id": user_id,
                "username": "member50",
                "display_name": "Garden Member 50",
                "avatar_url": "",
            },
        )
        media = profile["media"]["spotlight"]
        job_id = queue_media_upload(
            int(media["id"]),
            "owner",
            "333333333333333333",
        )
        self.assertTrue(claim_storage_job(job_id))
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            job = dict(
                connection.execute(
                    """
                    SELECT * FROM brofile_media_storage_jobs
                    WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
            )
        attachment = SimpleNamespace(
            url="https://cdn.discordapp.com/attachments/brofile-spotlight.png"
        )
        message = SimpleNamespace(
            id=666666666666666666,
            attachments=[attachment],
        )
        thread = SimpleNamespace(
            archived=False,
            send=AsyncMock(return_value=message),
        )
        cog = VisualAssetDiscordStorage(SimpleNamespace())
        with patch.object(
            cog,
            "_resolve_thread",
            AsyncMock(return_value=thread),
        ):
            asyncio.run(cog._upload_brofile(job))
        thread.send.assert_awaited_once()
        stored = get_brofile(self.guild_id, user_id)["media"]["spotlight"]
        self.assertEqual(stored["discord_sync_status"], "ready")
        self.assertEqual(
            stored["discord_attachment_url"],
            "https://cdn.discordapp.com/attachments/brofile-spotlight.png",
        )


if __name__ == "__main__":
    unittest.main()
