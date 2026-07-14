#!/usr/bin/env python3
"""Generate deterministic PNG fixtures for manual stats-visual review."""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.stats_visuals import (
    RankedGraphicItem,
    RankedGraphicSection,
    RenderResult,
    render_missingrole_result,
    render_ranked_graphic_result,
    render_rolecompare_result,
)


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
ACCENT = 0xF0319B


def ranked_members(count: int, *, long_names: bool = False):
    return [
        RankedGraphicItem(
            label=(
                "A deliberately long display name {:03d} — café 🌈".format(index)
                if long_names
                else "Member {:03d}".format(index)
            ),
            value="{:,}".format(1_250_000 - index * 1_337),
            subtitle="@member{:03d}".format(index),
            score=float(1_250_000 - index * 1_337),
        )
        for index in range(count)
    ]


def save_result(output: Path, name: str, result: RenderResult) -> None:
    for filename, payload in result.attachments("{}.png".format(name)):
        (output / filename).write_bytes(payload)
    sizes = ", ".join("{:,}".format(page.byte_size) for page in result.pages)
    print("{}: {} page(s), {} bytes".format(name, len(result.pages), sizes))


async def generate(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    save_result(
        output,
        "stats-overview",
        render_rolecompare_result(
            title="Community Overview",
            body="A compact summary of current role participation",
            role_1_name="Rangers",
            role_2_name="Campers",
            counts={
                "role_1_total": 240,
                "role_2_total": 198,
                "both": 162,
                "role_1_only": 78,
                "role_2_only": 36,
            },
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )
    save_result(
        output,
        "text-activity",
        await render_ranked_graphic_result(
            title="Top Text Participants",
            subtitle="Last 30 days • all activity",
            sections=[RankedGraphicSection("Member leaderboard", ranked_members(10))],
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )
    save_result(
        output,
        "vc-activity",
        await render_ranked_graphic_result(
            title="Voice Activity Leaders",
            subtitle="Last 30 days • completed VC sessions",
            sections=[
                RankedGraphicSection(
                    "Top voice channels",
                    [
                        RankedGraphicItem(
                            "Campfire {}".format(index + 1),
                            "{}h {}m".format(72 - index * 4, index * 3),
                            "Voice channel",
                            score=float(100 - index * 4),
                        )
                        for index in range(10)
                    ],
                ),
                RankedGraphicSection("Top voice members", ranked_members(10)),
            ],
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )
    save_result(
        output,
        "role-statistics",
        render_missingrole_result(
            title="Verified Role Coverage",
            body="Members who still need the required access role",
            has_role_name="Member",
            missing_role_name="Verified",
            has_role_total=240,
            missing_role_total=221,
            missing_count=19,
            missing_percent=7.9,
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )
    leaderboard = await render_ranked_graphic_result(
        title="Community Challenge",
        subtitle="All-time points",
        sections=[RankedGraphicSection("Member leaderboard", ranked_members(25))],
        updated_at=NOW,
        accent_color=ACCENT,
    )
    save_result(output, "leaderboard", leaderboard)
    save_result(
        output,
        "empty-state",
        await render_ranked_graphic_result(
            title="Activity Leaderboard",
            subtitle="Last 7 days",
            sections=[RankedGraphicSection("Members", [])],
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )
    save_result(
        output,
        "long-name-stress",
        await render_ranked_graphic_result(
            title="Long-name Stress Test",
            subtitle="Unicode, emoji, and bounded truncation",
            sections=[
                RankedGraphicSection("Members", ranked_members(10, long_names=True))
            ],
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )
    save_result(
        output,
        "high-volume-stress",
        await render_ranked_graphic_result(
            title="100-member Stress Test",
            subtitle="Stable ten-row pagination",
            sections=[RankedGraphicSection("Members", ranked_members(100))],
            updated_at=NOW,
            accent_color=ACCENT,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dev-output/stats-visuals"),
        help="Ignored output directory for generated PNGs",
    )
    args = parser.parse_args()
    asyncio.run(generate(args.output))


if __name__ == "__main__":
    main()
