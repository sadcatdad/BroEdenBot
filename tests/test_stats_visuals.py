import io
import os
import random
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from PIL import Image, ImageDraw

from utils.stats_visuals import (
    CompactRosterItem,
    ImageSizeLimitError,
    RankedGraphicItem,
    RankedGraphicSection,
    format_date_range,
    format_number,
    format_percent,
    image_target_bytes,
    render_compact_roster_result,
    render_missingrole_result,
    render_ranked_graphic_result,
)
from utils.stats_visuals.avatars import AvatarFetchResult, prepare_avatar
from utils.stats_visuals.components import base_canvas, draw_trend_indicator
from utils.stats_visuals.models import RenderState
from utils.stats_visuals.output import build_render_result
from utils.stats_visuals.text import load_font, truncate_text
from utils.stats_visuals.theme import COLORS, SQUARE_SUMMARY, LayoutProfile


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def ranked_items(count):
    return [
        RankedGraphicItem(
            label="Member {:03d}".format(index),
            value="{:,}".format(10_000_000 - index),
            subtitle="@member{:03d}".format(index),
            score=float(10_000_000 - index),
        )
        for index in range(count)
    ]


class StatsVisualFormattingTests(unittest.TestCase):
    def test_number_and_percent_formatting(self):
        self.assertEqual("0", format_number(0))
        self.assertEqual("1,234,567", format_number(1_234_567))
        self.assertEqual("1.2M", format_number(1_234_567, compact=True))
        self.assertEqual("-1.2K", format_number(-1_200, compact=True))
        self.assertEqual("+12.5%", format_percent(12.5, include_sign=True))
        self.assertEqual("-4.0%", format_percent(-4))

    def test_date_range_formatting(self):
        self.assertEqual("All time", format_date_range(None, None))
        self.assertEqual(
            "Jul 1, 2026 – Jul 13, 2026",
            format_date_range(date(2026, 7, 1), date(2026, 7, 13)),
        )

    def test_username_truncation_is_width_bounded(self):
        image = Image.new("RGB", (500, 100))
        draw = ImageDraw.Draw(image)
        font = load_font("username")
        state = RenderState()
        result = truncate_text(
            draw,
            "A very long username that cannot fit in the assigned row",
            font,
            180,
            state,
        )
        self.assertTrue(result.endswith("…"))
        self.assertLessEqual(draw.textlength(result, font=font), 180)
        self.assertEqual(1, state.truncated_text_count)

    def test_unicode_fallback_avoids_missing_glyph_boxes(self):
        image = Image.new("RGB", (500, 100))
        draw = ImageDraw.Draw(image)
        font = load_font("username")
        result = truncate_text(draw, "Pride 🏳️‍🌈 café 😀", font, 480)
        self.assertIn("🌈", result)
        self.assertNotIn("🏳", result)

    def test_invalid_image_target_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"STATS_IMAGE_TARGET_BYTES": "invalid"}):
            self.assertEqual(8_000_000, image_target_bytes())

    def test_broken_avatar_bytes_use_placeholder_path(self):
        self.assertIsNone(prepare_avatar(b"not an image", 64))
        source = Image.new("RGB", (100, 50), (240, 49, 155))
        data = io.BytesIO()
        source.save(data, "PNG")
        avatar = prepare_avatar(data.getvalue(), 64)
        self.assertIsNotNone(avatar)
        self.assertEqual((64, 64), avatar.size)

    def test_negative_trend_component_renders_without_special_glyph_dependency(self):
        state = RenderState()
        canvas = base_canvas(
            1200,
            1200,
            SQUARE_SUMMARY,
            state,
            COLORS.accent,
        )
        draw_trend_indicator(canvas, -12.5, 48, 48, "from prior period")
        self.assertIsNotNone(canvas.image.getbbox())


class StatsVisualPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def render_ranked(self, count):
        return await render_ranked_graphic_result(
            title="Test leaderboard",
            subtitle="Deterministic fixture",
            sections=[RankedGraphicSection("Members", ranked_items(count))],
            updated_at=NOW,
            accent_color=0xF0319B,
        )

    async def test_empty_one_three_and_full_page_states(self):
        for count in (0, 1, 3, 10):
            with self.subTest(count=count):
                result = await self.render_ranked(count)
                self.assertEqual(1, len(result.pages))
                self.assertGreater(result.pages[0].byte_size, 0)
                self.assertLessEqual(result.pages[0].byte_size, 8_000_000)

    async def test_one_over_full_page_paginates(self):
        result = await self.render_ranked(11)
        self.assertEqual(2, len(result.pages))
        self.assertEqual([1, 2], [page.page_number for page in result.pages])
        self.assertEqual([2, 2], [page.page_count for page in result.pages])
        self.assertEqual(
            ["leaderboard_1.png", "leaderboard_2.png"],
            result.attachment_names("leaderboard.png"),
        )

    async def test_hundred_member_stress_fixture_has_stable_pages(self):
        result = await self.render_ranked(100)
        self.assertEqual(10, len(result.pages))
        self.assertTrue(all(page.width == 1200 for page in result.pages))
        self.assertTrue(all(page.height == 1500 for page in result.pages))

    async def test_tied_scores_preserve_supplied_rank_sequence(self):
        items = [
            RankedGraphicItem("Alpha", "100", score=100),
            RankedGraphicItem("Beta", "100", score=100),
            RankedGraphicItem("Gamma", "50", score=50),
        ]
        result = await render_ranked_graphic_result(
            title="Ties",
            subtitle="Existing order is preserved",
            sections=[RankedGraphicSection("Members", items, rank_start=4)],
            updated_at=NOW,
            accent_color=0xF0319B,
        )
        self.assertEqual(1, len(result.pages))

    @mock.patch(
        "utils.stats_visuals.renderers.fetch_avatars",
        new_callable=mock.AsyncMock,
    )
    async def test_missing_avatar_uses_initials_and_reports_fallback(self, fetch):
        fetch.return_value = AvatarFetchResult(
            data={},
            failed_urls=("https://example.invalid/broken.png",),
        )
        result = await render_ranked_graphic_result(
            title="Missing avatar",
            subtitle="Fallback fixture",
            sections=[
                RankedGraphicSection(
                    "Members",
                    [
                        RankedGraphicItem(
                            "Duplicate Name",
                            "0",
                            avatar_url="https://example.invalid/broken.png",
                            score=0,
                        ),
                        RankedGraphicItem("Duplicate Name", "0", score=0),
                    ],
                )
            ],
            updated_at=NOW,
            accent_color=0xF0319B,
        )
        self.assertEqual(1, result.diagnostics.avatar_fallback_count)
        self.assertGreater(result.pages[0].byte_size, 0)

    async def test_roster_one_over_page_and_long_name(self):
        items = [
            CompactRosterItem(
                "Very long Unicode display name {:02d} — café 🌈".format(index)
            )
            for index in range(13)
        ]
        result = await render_compact_roster_result(
            title="Role roster",
            body="Long-name stress fixture",
            role_name="Rangers",
            items=items,
            updated_at=NOW,
            accent_color=0xF0319B,
            include_avatars=False,
        )
        self.assertEqual(2, len(result.pages))
        self.assertEqual(2, result.pages[-1].page_number)


class StatsVisualOutputTests(unittest.TestCase):
    def test_every_report_page_is_checked_against_target(self):
        result = render_missingrole_result(
            title="Missing role",
            body="Coverage fixture",
            has_role_name="Member",
            missing_role_name="Verified",
            has_role_total=100,
            missing_role_total=80,
            missing_count=20,
            missing_percent=20,
            updated_at=NOW,
            accent_color=0xF0319B,
        )
        self.assertLessEqual(result.pages[0].byte_size, image_target_bytes())

    def test_palette_or_dimension_fallback_reaches_target(self):
        profile = LayoutProfile("test", 360, 360, 280, 280, 0, 0, 0)

        def factory(width, height):
            randomizer = random.Random(42)
            image = Image.new("RGB", (width, height))
            pixels = image.load()
            for y in range(height):
                for x in range(width):
                    pixels[x, y] = (
                        randomizer.randrange(256),
                        randomizer.randrange(256),
                        randomizer.randrange(256),
                    )
            return image

        result = build_render_result(
            graphic_type="optimization_test",
            profile=profile,
            factories=[factory],
            target_bytes=130_000,
        )
        self.assertLessEqual(result.pages[0].byte_size, 130_000)
        self.assertTrue(result.pages[0].optimized)

    def test_impossible_byte_target_fails_instead_of_uploading(self):
        profile = LayoutProfile("tiny", 100, 100, 100, 100, 0, 0, 0)

        with self.assertRaises(ImageSizeLimitError):
            build_render_result(
                graphic_type="too_large",
                profile=profile,
                factories=[lambda width, height: Image.new("RGB", (width, height))],
                target_bytes=1,
            )


if __name__ == "__main__":
    unittest.main()
