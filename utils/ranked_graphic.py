"""Compatibility adapter for the centralized Bro Eden stats visual system."""

from datetime import datetime
from typing import Iterable, Optional

from utils.stats_visuals import (
    RankedGraphicItem,
    RankedGraphicSection,
    RenderResult,
    render_ranked_graphic_result,
)


async def render_ranked_graphic(
    *,
    title: str,
    subtitle: str,
    sections: Iterable[RankedGraphicSection],
    updated_at: datetime,
    accent_color: int,
    total_entries: Optional[int] = None,
) -> bytes:
    """Return the first page for legacy callers.

    New and migrated callers should use ``render_ranked_graphic_result`` so
    every density-limited page can be uploaded in order.
    """
    result = await render_ranked_graphic_result(
        title=title,
        subtitle=subtitle,
        sections=sections,
        updated_at=updated_at,
        accent_color=accent_color,
        total_entries=total_entries,
    )
    return result.pages[0].png


__all__ = [
    "RankedGraphicItem",
    "RankedGraphicSection",
    "RenderResult",
    "render_ranked_graphic",
    "render_ranked_graphic_result",
]
