"""Compatibility adapters for centralized role-stat report renderers."""

from datetime import datetime
from typing import Dict

from utils.stats_visuals import (
    render_error_result,
    render_missingrole_result,
    render_rolecompare_result,
)


def render_rolecompare_report(
    *,
    title: str,
    body: str,
    role_1_name: str,
    role_2_name: str,
    counts: Dict[str, int],
    updated_at: datetime,
    accent_color: int,
) -> bytes:
    return render_rolecompare_result(
        title=title,
        body=body,
        role_1_name=role_1_name,
        role_2_name=role_2_name,
        counts=counts,
        updated_at=updated_at,
        accent_color=accent_color,
    ).pages[0].png


def render_missingrole_report(
    *,
    title: str,
    body: str,
    has_role_name: str,
    missing_role_name: str,
    has_role_total: int,
    missing_role_total: int,
    missing_count: int,
    missing_percent: float,
    updated_at: datetime,
    accent_color: int,
) -> bytes:
    return render_missingrole_result(
        title=title,
        body=body,
        has_role_name=has_role_name,
        missing_role_name=missing_role_name,
        has_role_total=has_role_total,
        missing_role_total=missing_role_total,
        missing_count=missing_count,
        missing_percent=missing_percent,
        updated_at=updated_at,
        accent_color=accent_color,
    ).pages[0].png


def render_report_error(
    *,
    title: str,
    message: str,
    updated_at: datetime,
    accent_color: int,
) -> bytes:
    return render_error_result(
        title=title,
        message=message,
        updated_at=updated_at,
        accent_color=accent_color,
    ).pages[0].png


__all__ = [
    "render_missingrole_report",
    "render_report_error",
    "render_rolecompare_report",
]
