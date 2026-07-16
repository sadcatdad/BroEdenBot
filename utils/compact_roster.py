"""Compatibility adapter for role-roster stats graphics."""

from datetime import datetime
from typing import Iterable, Optional

from utils.stats_visuals import (
    CompactRosterItem,
    RenderResult,
    render_compact_roster_result,
)


async def render_compact_roster_pngs(
    *,
    title: str,
    body: str,
    role_name: str,
    items: Iterable[CompactRosterItem],
    updated_at: datetime,
    accent_color: int,
    include_avatars: bool = True,
    banner_bytes: Optional[bytes] = None,
    template_key: str = "role_roster",
) -> list:
    result = await render_compact_roster_result(
        title=title,
        body=body,
        role_name=role_name,
        items=items,
        updated_at=updated_at,
        accent_color=accent_color,
        include_avatars=include_avatars,
        banner_bytes=banner_bytes,
        template_key=template_key,
    )
    return [page.png for page in result.pages]


__all__ = [
    "CompactRosterItem",
    "RenderResult",
    "render_compact_roster_pngs",
    "render_compact_roster_result",
]
