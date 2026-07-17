import asyncio
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient

from dashboard.app import app
from utils.stats_visuals import RankedGraphicItem, RankedGraphicSection
from utils.stats_visuals.renderers import render_ranked_graphic_result
from utils.visual_studio.preview import render_preview
from utils.visual_studio.registry import (
    REGISTRY,
    AssetSlot,
    SafeArea,
    TemplateDefinition,
    TemplateRegistry,
)
from utils.visual_studio.repository import (
    export_configuration,
    delete_schedule,
    duplicate_theme,
    get_visual_template,
    import_configuration_as_drafts,
    initialize_visual_studio_schema,
    list_global_schedules,
    list_themes,
    publish_template,
    resolve_published_configuration,
    restore_template_version,
    save_global_settings,
    save_schedule,
    save_template_draft,
    save_theme,
    save_variant,
    set_schedule_enabled,
)
from utils.visual_studio.runtime import load_runtime_customization_sync
from utils.visual_studio.storage import (
    archive_asset,
    asset_path,
    delete_asset,
    get_asset,
    inspect_upload,
    save_asset,
)


def png(width=1600, height=900, *, alpha=False):
    output = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", (width, height), (40, 90, 160, 180) if alpha else (40, 90, 160)).save(output, "PNG")
    return output.getvalue()


class VisualStudioTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database = root / "data.db"
        self.assets = root / "visual-assets"
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(self.database),
                "VISUAL_ASSET_DIR": str(self.assets),
            },
            clear=False,
        )
        self.environment.start()
        initialize_visual_studio_schema()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_registry_has_every_discovered_generator_and_exact_canvases(self):
        expected = {
            "activity_leaderboard": (1200, 1500),
            "vc_leaderboard": (1200, 1500),
            "custom_leaderboard": (1200, 1500),
            "bump_leaderboard": (1200, 1500),
            "streak_leaderboard": (1200, 1500),
            "role_roster": (1200, 1500),
            "role_comparison": (1600, 900),
            "missing_role": (1600, 900),
            "stats_error": (1600, 900),
            "queue_next": (1024, 258),
        }
        self.assertEqual({item.key: (item.width, item.height) for item in REGISTRY.all()}, expected)
        for definition in REGISTRY.all():
            background = definition.slot("background")
            self.assertEqual((background.recommended_width, background.recommended_height), (definition.width, definition.height))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM visual_schema_migrations"
                ).fetchone()[0],
                2,
            )
            usage_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(visual_asset_usage)"
                ).fetchall()
            }
        self.assertIn("global_settings_id", usage_columns)

    def test_registry_rejects_duplicate_keys(self):
        item = TemplateDefinition(
            key="sample",
            display_name="Sample",
            description="Sample",
            category="other",
            renderer="sample",
            width=100,
            height=100,
            supported_settings=(),
            asset_slots=(AssetSlot("background", "Background", "background", 100, 100, 50, 50, 200, 200, safe_area=SafeArea(1, 1, 1, 1)),),
            defaults={},
            command_source="test",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            TemplateRegistry((item, item))

    def test_schema_and_resolution_precedence(self):
        save_global_settings({"accent_color": "#112233", "panel_opacity": 0.8}, "owner")
        theme_id = save_theme(
            name="Seasonal",
            description="test",
            settings={"accent_color": "#334455", "text_color": "#eeeeee"},
            actor="owner",
        )
        save_template_draft(
            "activity_leaderboard",
            {"accent_color": "#abcdef", "maximum_rows": 8},
            theme_id=theme_id,
            actor="owner",
        )
        version = publish_template("activity_leaderboard", actor="owner", change_summary="First")
        self.assertEqual(version, 1)
        resolved = resolve_published_configuration("activity_leaderboard", use_cache=False)
        self.assertEqual(resolved["settings"]["accent_color"], "#abcdef")
        self.assertEqual(resolved["settings"]["text_color"], "#eeeeee")
        self.assertEqual(resolved["settings"]["panel_opacity"], 0.8)
        self.assertEqual(resolved["settings"]["maximum_rows"], 8)

    def test_draft_publish_history_and_restore_to_draft(self):
        save_template_draft("vc_leaderboard", {"title": "First"}, theme_id=None, actor="owner")
        publish_template("vc_leaderboard", actor="owner", change_summary="First")
        save_template_draft("vc_leaderboard", {"title": "Second"}, theme_id=None, actor="owner")
        publish_template("vc_leaderboard", actor="owner", change_summary="Second")
        item = get_visual_template("vc_leaderboard")
        self.assertEqual(item["published_version"], 2)
        self.assertGreaterEqual(len(item["versions"]), 2)
        restore_template_version("vc_leaderboard", 1, "owner")
        restored = get_visual_template("vc_leaderboard")
        self.assertEqual(restored["draft_settings"]["title"], "First")
        self.assertEqual(restored["published_settings"]["title"], "Second")

    def test_upload_validation_spoof_corrupt_dimensions_and_crop(self):
        with self.assertRaisesRegex(ValueError, "extension does not match"):
            inspect_upload(png(), filename="fake.jpg", asset_type="background")
        with self.assertRaisesRegex(ValueError, "could not be decoded"):
            inspect_upload(b"not-an-image", filename="bad.png", asset_type="background")
        inspection = inspect_upload(
            png(1080, 1080),
            filename="square.png",
            asset_type="background",
            template_key="activity_leaderboard",
            slot_key="background",
        )
        self.assertTrue(inspection["wrong_aspect"])
        with self.assertRaisesRegex(ValueError, "Confirm the aspect-ratio adjustment"):
            save_asset(
                png(1080, 1080),
                filename="square.png",
                name="Square",
                asset_type="background",
                actor="owner",
                template_key="activity_leaderboard",
                slot_key="background",
            )
        asset_id, _ = save_asset(
            png(1080, 1080),
            filename="square.png",
            name="Square",
            asset_type="background",
            actor="owner",
            template_key="activity_leaderboard",
            slot_key="background",
            allow_crop=True,
            acknowledge_quality=True,
        )
        asset = get_asset(asset_id)
        self.assertEqual((asset["width"], asset["height"]), (1200, 1500))
        self.assertTrue(asset_path(asset["storage_key"]).is_file())

    def test_transparency_preserved_for_overlay(self):
        asset_id, _ = save_asset(
            png(1200, 1500, alpha=True),
            filename="overlay.png",
            name="Overlay",
            asset_type="overlay",
            actor="owner",
            template_key="activity_leaderboard",
            slot_key="background",
        )
        with Image.open(asset_path(get_asset(asset_id)["storage_key"])) as image:
            self.assertEqual(image.mode, "RGBA")

    def test_referenced_asset_cannot_be_archived_or_deleted(self):
        asset_id, _ = save_asset(
            png(1200, 1500),
            filename="background.png",
            name="Background",
            asset_type="background",
            actor="owner",
            template_key="activity_leaderboard",
            slot_key="background",
        )
        save_template_draft("activity_leaderboard", {"assets": {"background": asset_id}}, theme_id=None, actor="owner")
        publish_template("activity_leaderboard", actor="owner", change_summary="Asset")
        with self.assertRaisesRegex(ValueError, "actively referenced"):
            archive_asset(asset_id, "owner")

    def test_global_asset_usage_is_tracked_and_protected(self):
        asset_id, _ = save_asset(
            png(1024, 1024, alpha=True),
            filename="logo.png",
            name="Global logo",
            asset_type="logo",
            actor="owner",
        )
        save_global_settings({"assets": {"logo": asset_id}}, "owner")
        asset = get_asset(asset_id)
        self.assertEqual(asset["usage_count"], 1)
        self.assertEqual(asset["usages"][0]["global_settings_id"], 1)
        with self.assertRaisesRegex(ValueError, "actively referenced"):
            archive_asset(asset_id, "owner")

    def test_runtime_missing_file_falls_back_without_crashing(self):
        asset_id, _ = save_asset(
            png(1200, 1500),
            filename="background.png",
            name="Background",
            asset_type="background",
            actor="owner",
            template_key="activity_leaderboard",
            slot_key="background",
        )
        save_template_draft("activity_leaderboard", {"assets": {"background": asset_id}}, theme_id=None, actor="owner")
        publish_template("activity_leaderboard", actor="owner", change_summary="Asset")
        asset_path(get_asset(asset_id)["storage_key"]).unlink()
        customization = load_runtime_customization_sync("activity_leaderboard")
        self.assertIsNone(customization.background_bytes)
        self.assertTrue(customization.warnings)

    def test_preview_all_templates_has_registered_size_and_is_bounded(self):
        for definition in REGISTRY.all():
            data = render_preview(definition.key, safe_area=True)
            self.assertLessEqual(len(data), definition.max_output_bytes)
            with Image.open(io.BytesIO(data)) as image:
                self.assertEqual(image.size, (definition.width, definition.height))

    def test_schedule_precedence_and_portable_import_as_drafts(self):
        theme_id = save_theme(name="Scheduled", description="", settings={"accent_color": "#123456"}, actor="owner")
        now = datetime.now(timezone.utc)
        save_schedule(
            template_key="activity_leaderboard",
            theme_id=theme_id,
            variant_id=None,
            starts_at=(now - timedelta(minutes=5)).isoformat(),
            ends_at=(now + timedelta(minutes=5)).isoformat(),
            timezone_name="America/Chicago",
            priority=10,
            actor="owner",
        )
        resolved = resolve_published_configuration("activity_leaderboard", use_cache=False)
        self.assertEqual(resolved["settings"]["accent_color"], "#123456")
        exported = export_configuration("activity_leaderboard")
        imported = import_configuration_as_drafts(exported, "owner")
        self.assertEqual(imported, ["activity_leaderboard"])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            import_configuration_as_drafts({"schema": "bad", "schema_version": 1}, "owner")

    def test_schedule_timezone_toggle_delete_and_theme_duplication(self):
        built_in = next(theme for theme in list_themes() if theme["is_builtin"])
        with self.assertRaisesRegex(ValueError, "read-only"):
            save_theme(
                name="Changed default",
                description="",
                settings={},
                actor="owner",
                theme_id=built_in["id"],
            )
        duplicate_id = duplicate_theme(built_in["id"], "owner")
        duplicate = next(theme for theme in list_themes() if theme["id"] == duplicate_id)
        self.assertFalse(duplicate["is_builtin"])
        schedule_id = save_schedule(
            template_key=None,
            theme_id=duplicate_id,
            variant_id=None,
            starts_at="2026-12-01T10:00",
            ends_at="2026-12-02T10:00",
            timezone_name="America/Chicago",
            priority=4,
            actor="owner",
        )
        schedule = list_global_schedules()[0]
        self.assertEqual(schedule["id"], schedule_id)
        self.assertTrue(schedule["starts_at"].endswith("+00:00"))
        self.assertEqual(schedule["starts_at"][:16], "2026-12-01T16:00")
        self.assertIsNone(set_schedule_enabled(schedule_id, False, "owner"))
        self.assertFalse(list_global_schedules()[0]["enabled"])
        self.assertIsNone(delete_schedule(schedule_id, "owner"))
        self.assertEqual(list_global_schedules(), [])

    def test_shared_asset_type_mismatch_is_rejected(self):
        asset_id, _ = save_asset(
            png(1600, 900),
            filename="background.png",
            name="Background",
            asset_type="background",
            actor="owner",
        )
        with self.assertRaisesRegex(ValueError, "logo slot requires"):
            save_global_settings({"assets": {"logo": asset_id}}, "owner")

    def test_published_settings_change_live_renderer_and_pagination(self):
        items = [
            RankedGraphicItem(
                label="Member {}".format(index),
                value=str(100 - index),
                score=float(100 - index),
            )
            for index in range(6)
        ]

        async def render():
            return await render_ranked_graphic_result(
                title="Legacy Title",
                subtitle="Legacy Subtitle",
                sections=[RankedGraphicSection("Members", items)],
                updated_at=datetime.now(timezone.utc),
                accent_color=0xF0319B,
                template_key="activity_leaderboard",
            )

        original = asyncio.run(render())
        save_template_draft(
            "activity_leaderboard",
            {
                "title": "Published Studio Title",
                "accent_color": "#123456",
                "maximum_rows": 5,
                "avatar_shape": "rounded",
                "high_contrast": True,
                "title_size": 48,
            },
            theme_id=None,
            actor="owner",
        )
        publish_template(
            "activity_leaderboard",
            actor="owner",
            change_summary="Renderer integration",
        )
        customized = asyncio.run(render())
        self.assertEqual(len(original.pages), 1)
        self.assertEqual(len(customized.pages), 2)
        self.assertNotEqual(original.pages[0].png, customized.pages[0].png)
        self.assertLessEqual(
            max(page.byte_size for page in customized.pages),
            REGISTRY.get("activity_leaderboard").max_output_bytes,
        )
        variant_id = save_variant(
            "activity_leaderboard",
            name="Compact 960",
            description="Scaled same-ratio variant",
            settings={"maximum_rows": 3},
            width=960,
            height=1200,
            theme_id=None,
            actor="owner",
        )
        built_in = next(theme for theme in list_themes() if theme["is_builtin"])
        now = datetime.now(timezone.utc)
        save_schedule(
            template_key="activity_leaderboard",
            theme_id=built_in["id"],
            variant_id=variant_id,
            starts_at=(now - timedelta(minutes=5)).isoformat(),
            ends_at=(now + timedelta(minutes=5)).isoformat(),
            timezone_name="America/Chicago",
            priority=50,
            actor="owner",
        )
        variant_result = asyncio.run(render())
        self.assertEqual((variant_result.pages[0].width, variant_result.pages[0].height), (960, 1200))
        self.assertEqual(len(variant_result.pages), 2)


class VisualStudioDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_PATH": str(root / "data.db"),
                "VISUAL_ASSET_DIR": str(root / "assets"),
                "DASHBOARD_ENABLED": "true",
                "DASHBOARD_USERNAME": "owner",
                "DASHBOARD_PASSWORD": "visual-test-password",
                "DASHBOARD_SECRET_KEY": "visual-test-session-key",
            },
            clear=False,
        )
        self.environment.start()
        initialize_visual_studio_schema()
        self.client = TestClient(app)
        page = self.client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        self.client.post("/login", data={"username": "owner", "password": "visual-test-password", "csrf": csrf})

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        self.temporary_directory.cleanup()

    def csrf(self, path="/visual"):
        response = self.client.get(path)
        return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)

    def test_navigation_cards_size_guidance_preview_and_api(self):
        page = self.client.get("/visual")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Visual Content Studio", page.text)
        self.assertIn("Queue Up Next Banner", page.text)
        editor = self.client.get("/visual/templates/activity_leaderboard")
        self.assertIn("1200 × 1500 px", editor.text)
        self.assertIn("Safe-area overlay", editor.text)
        reference = self.client.get("/visual/reference")
        self.assertIn("1104×208", reference.text)
        preview = self.client.get("/visual/templates/activity_leaderboard/preview?safe_area=true")
        self.assertEqual(preview.headers["content-type"], "image/png")
        api = self.client.get("/api/visual/templates")
        self.assertEqual(len(api.json()["templates"]), 10)

    def test_save_draft_publish_and_viewer_protection(self):
        csrf = self.csrf("/visual/templates/activity_leaderboard")
        response = self.client.post(
            "/visual/templates/activity_leaderboard/draft",
            data={
                "csrf": csrf,
                "title": "Dashboard Draft",
                "accent_color": "#123456",
                "settings_json": "{}",
                "show_avatars": "on",
                "show_ranks": "on",
                "show_timestamp": "on",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(get_visual_template("activity_leaderboard")["draft_settings"]["title"], "Dashboard Draft")
        csrf = self.csrf("/visual/templates/activity_leaderboard")
        published = self.client.post(
            "/visual/templates/activity_leaderboard/publish",
            data={"csrf": csrf, "change_summary": "Dashboard test"},
            follow_redirects=False,
        )
        self.assertEqual(published.status_code, 303)
        self.assertEqual(get_visual_template("activity_leaderboard")["published_version"], 1)
        unauthenticated = TestClient(app)
        try:
            blocked = unauthenticated.get("/visual", follow_redirects=False)
            self.assertEqual(blocked.status_code, 303)
        finally:
            unauthenticated.close()
        csrf = self.csrf("/visual/templates/activity_leaderboard")
        with patch("dashboard.app.is_admin", return_value=False):
            viewer_write = self.client.post(
                "/visual/templates/activity_leaderboard/draft",
                data={"csrf": csrf, "settings_json": "{}"},
            )
        self.assertEqual(viewer_write.status_code, 403)

    def test_upload_guidance_and_wrong_ratio_message(self):
        page = self.client.get("/visual/assets/upload?template_key=activity_leaderboard&slot_key=background")
        self.assertIn("1600", page.text)  # registry JSON includes wide canvases
        self.assertIn("1200 × 1500 px", page.text)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        response = self.client.post(
            "/visual/assets/upload",
            data={
                "csrf": csrf,
                "name": "Wrong ratio",
                "asset_type": "background",
                "template_key": "activity_leaderboard",
                "slot_key": "background",
            },
            files={"file": ("square.png", png(1080, 1080), "image/png")},
            follow_redirects=True,
        )
        self.assertIn("aspect ratio does not match", response.text)


if __name__ == "__main__":
    unittest.main()
