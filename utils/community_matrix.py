"""Deterministic rubric matrix PNGs, drawn from verified scores without AI calls."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from utils.community_contribution import BASELINE, CATEGORIES, Scorecard, scorecard_label
from utils.context_render import clean_markdown
from utils.stats_visuals.text import load_font, plain_text, truncate_text, wrap_text


MATRIX_FILENAME = "community-contribution.png"
WIDTH = 1_280
BACKGROUND = "#0e141b"
SURFACE = "#16202b"
SURFACE_ALT = "#192532"
BORDER = "#2a3948"
TEXT = "#eef4f7"
MUTED = "#9cabb9"
ACCENT = "#85dfbc"
SELECTED = "#203d37"
WARNING = "#edc17c"


def _text(value: object) -> str:
    return plain_text(clean_markdown(value))


def matrix_alt_text(scorecard: Scorecard) -> str:
    """Discord attachment descriptions are limited to 1024 characters."""
    lines = [f"Community contribution: {scorecard.score}/100." if scorecard.score is not None
             else scorecard_label(scorecard) + "."]
    if scorecard.status != "review_required":
        lines.extend(f"{c.name}: {c.rating}/4, +{c.bonus:g}/{c.maximum}. {_text(c.explanation)[:85]}"
                     for c in scorecard.categories)
    if scorecard.status == "scored":
        lines.append(f"{BASELINE} baseline + {scorecard.bonus:g} bonuses - {scorecard.penalty} deductions.")
    lines.append(f"{scorecard.confidence} confidence; {scorecard.exchanges} sessions. Missing activity is not a deduction.")
    return " ".join(lines)[:1_024]


def render_community_matrix(scorecard: Scorecard, metadata: dict) -> bytes:
    """Render a bounded, portable matrix with real numbers and no source quotes."""
    fonts = {size: load_font("supporting_stat", family="Calibri Regular", size_override=size)
             for size in (18, 20, 22, 24, 26, 28, 40, 48, 76)}
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    scored = scorecard.status == "scored"
    review = scorecard.status == "review_required"
    by_key = {c.key: c for c in scorecard.categories} if not review else {}
    deduction_lines = []
    if scored:
        for points, reason in scorecard.deductions[:4]:
            deduction_lines.extend(wrap_text(measure, f"−{points}  {_text(reason)}", fonts[20], 1_120, 2))
        if len(scorecard.deductions) > 4:
            remainder = sum(points for points, _ in scorecard.deductions[4:])
            deduction_lines.append(f"{len(scorecard.deductions) - 4} additional incidents · −{remainder} combined")
        if not deduction_lines:
            deduction_lines = ["No supported conduct deductions."]
    else:
        deduction_lines = list(wrap_text(measure, _text(scorecard.explanation), fonts[22], 1_120, 3))
    notes = list(wrap_text(measure, _text(scorecard.explanation), fonts[18], 1_140, 2)) if scored else []
    coverage_lines = str(metadata.get("timeframe_text") or "Selected archived activity").splitlines()[:3]
    # Allocate the complete text layout before painting, including long
    # coverage/deduction cases; never crop a warning or calculation.
    height = (827 + 25 + (145 if scored else 32) + len(deduction_lines) * 25
              + 20 + 18 + 29 + len(coverage_lines) * 25 + 12 + len(notes) * 21 + 68)
    image = Image.new("RGB", (WIDTH, height), BACKGROUND)
    draw = ImageDraw.Draw(image)

    def write(x, y, value, size=22, fill=TEXT, width=None, anchor=None):
        value = _text(value)
        if width is not None:
            value = truncate_text(draw, value, fonts[size], width)
        draw.text((x, y), value, font=fonts[size], fill=fill, anchor=anchor)

    draw.rounded_rectangle((44, 35, 49, 185), radius=2, fill=ACCENT)
    write(70, 35, "BRO EDEN  /  COMMUNITY CONTRIBUTION", 18, ACCENT)
    write(70, 67, "Contribution rubric", 48, width=850)
    member = str(metadata.get("title") or "Member").removeprefix("Community Evaluation:").strip()
    write(72, 126, member, 28, width=845)
    write(72, 169, scorecard_label(scorecard), 22, ACCENT if scored else WARNING, width=845)
    draw.rounded_rectangle((1_001, 35, 1_236, 198), radius=20, fill=SELECTED if scored else SURFACE,
                           outline=ACCENT if scored else BORDER, width=1)
    write(1_119, 59, str(scorecard.score) if scorecard.score is not None else "N/A", 76,
          ACCENT if scored else MUTED, anchor="mt")
    write(1_119, 148, "OUT OF 100" if scored else "SCORE WITHHELD", 18, MUTED, anchor="mt")

    # All rating columns are equally spaced; one highlighted cell per rated row.
    left, right, top, header, row_height = 44, 1_236, 230, 77, 104
    matrix_bottom = top + header + 5 * row_height
    draw.rounded_rectangle((left, top, right, matrix_bottom), radius=18, fill=SURFACE, outline=BORDER)
    write(65, top + 28, "CONTRIBUTION", 18, MUTED)
    rating_centers = [447 + 132 * index for index in range(5)]
    levels = [("Not", "observed"), ("One", "example"), ("Separate", "exchanges"),
              ("Across", "dates"), ("Across", "weeks")]
    for center, (line1, line2) in zip(rating_centers, levels):
        write(center, top + 13, line1, 20, MUTED, anchor="mt")
        write(center, top + 36, line2, 20, MUTED, anchor="mt")
    write(1_153, top + 28, "EARNED", 18, MUTED, anchor="mt")
    short_names = {"connection": "Connection & conversation", "inclusion": "Welcoming & inclusion",
                   "support": "Support & helpfulness", "initiative": "Activities & initiative",
                   "conduct": "Constructive conduct"}
    for index, (key, _, maximum) in enumerate(CATEGORIES):
        y = top + header + index * row_height
        if index % 2 == 0:
            draw.rectangle((left + 1, y, right - 1, y + row_height - 1), fill=SURFACE_ALT)
        draw.line((left + 1, y, right - 1, y), fill=BORDER)
        write(65, y + 12, short_names[key], 24, width=310)
        category = by_key.get(key)
        reason = (category.explanation if category else
                  "Awaiting staff review." if review else "No supported rating available.")
        for line_index, line in enumerate(wrap_text(draw, _text(reason), fonts[18], 307, 2)):
            write(65, y + 45 + line_index * 21, line, 18, MUTED, width=307)
        for rating, center in enumerate(rating_centers):
            selected = category is not None and category.rating == rating
            color = ACCENT if selected and rating > 0 else MUTED if selected else "#546575"
            draw.rounded_rectangle((center - 43, y + 21, center + 43, y + 79), radius=12,
                                   fill=SELECTED if selected and rating > 0 else SURFACE,
                                   outline=color if selected else BORDER, width=2 if selected else 1)
            write(center, y + 31, str(rating), 28, color, anchor="mt")
        write(1_153, y + 20, f"+{category.bonus:g}" if category else "—", 28,
              ACCENT if category else MUTED, anchor="mt")
        write(1_153, y + 58, f"of {maximum} bonus", 18, MUTED, anchor="mt")

    y = matrix_bottom + 25
    if scored:
        labels = [("BASELINE", str(BASELINE)), ("EARNED BONUSES", f"+{scorecard.bonus:g}"),
                  ("CONDUCT DEDUCTIONS", f"−{scorecard.penalty}")]
        for index, (label, value) in enumerate(labels):
            x = 44 + index * 405
            draw.rounded_rectangle((x, y, x + 382, y + 90), radius=14, fill=SURFACE, outline=BORDER)
            write(x + 20, y + 15, label, 18, MUTED)
            write(x + 350, y + 35, value, 40, ACCENT if index == 1 else TEXT, anchor="rt")
        y += 109
        detail = f"Rounded final score: {scorecard.score}/100"
        if scorecard.ceiling is not None:
            detail += f"  ·  Applicable ceiling: {scorecard.ceiling}/100"
        write(48, y, detail, 20, MUTED)
        y += 36
    else:
        write(48, y, "NUMERICAL SCORE WITHHELD", 18, WARNING)
        y += 32
    for line in deduction_lines:
        write(48, y, line, 20 if scored else 22, MUTED, width=1_165)
        y += 25
    y += 20
    draw.line((44, y, 1_236, y), fill=BORDER)
    y += 18
    write(48, y, f"{scorecard.confidence.upper()} CONFIDENCE  ·  {scorecard.exchanges:,} interpretable sessions", 20, ACCENT)
    y += 29
    # Keep time range and reviewed/available counts separate and legible.
    for line in coverage_lines:
        write(48, y, line, 20, MUTED, width=1_165)
        y += 25
    y += 12
    for line in notes:
        write(48, y, line, 18, MUTED, width=1_140)
        y += 21
    write(48, height - 36, "RUBRIC v1  ·  Highlighted cell = observed rating  ·  0 = not observed, no deduction", 18, MUTED, width=1_165)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
