"""Rendering helpers that turn structured AI context-summary JSON into clean,
staff-friendly Discord embeds for `/context user` and `/context channel`.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import discord

from utils.message_context import parse_timestamp, safe_discord_jump_url
from utils.privacy import redact_sensitive_text
from utils.ui import branded_embed, truncate


FOOTER_TEXT = "AI-generated staff summary • Review before taking action"
PUBLIC_EVALUATION_FOOTER_TEXT = (
    "AI-generated community evaluation • Based on available server activity"
)
DESCRIPTION_LIMIT = 500
FALLBACK_DESCRIPTION_LIMIT = 3_500
FIELD_VALUE_LIMIT = 900
EMBED_TOTAL_LIMIT = 5_500

_MENTION_PATTERN = re.compile(r"<#(\d+)>")
_HEADING_PATTERN = re.compile(r"(?m)^ {0,3}#{1,6}\s*")
_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_UNDERLINE_BOLD_PATTERN = re.compile(r"__(.+?)__")
_CODE_FENCE_PATTERN = re.compile(r"`{1,3}")
_BLANK_RUN_PATTERN = re.compile(r"\n{3,}")
_TRAILING_SPACE_PATTERN = re.compile(r"[ \t]+\n")
_JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL
)


def clean_markdown(text: object) -> str:
    """Strip markdown headings/bold/code fences and raw channel mentions, and
    redact obvious credentials as a defense-in-depth safety net (message
    content is redacted at ingestion too, but AI output isn't guaranteed to
    only echo already-redacted text)."""
    value = redact_sensitive_text(text)
    value = _HEADING_PATTERN.sub("", value)
    value = _BOLD_PATTERN.sub(r"\1", value)
    value = _UNDERLINE_BOLD_PATTERN.sub(r"\1", value)
    value = _CODE_FENCE_PATTERN.sub("", value)
    value = _MENTION_PATTERN.sub(r"#\1", value)
    value = _TRAILING_SPACE_PATTERN.sub("\n", value)
    value = _BLANK_RUN_PATTERN.sub("\n\n", value)
    return value.strip()


def _strip_json_code_fences(text: str) -> str:
    stripped = text.strip()
    fenced = _JSON_FENCE_PATTERN.fullmatch(stripped)
    if fenced:
        return fenced.group(1).strip()
    stripped = re.sub(r"^\s*```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    return stripped.strip()


def _salvage_truncated_json_object(cleaned: str) -> Optional[dict[str, object]]:
    """Best-effort recovery of a JSON object that was cut off before it finished
    (e.g. the model hit its output-token limit mid-write). Keeps every complete
    top-level key/value pair and discards the trailing incomplete one, so a
    truncated summary still renders as a structured embed (missing fields simply
    show as empty) instead of dumping raw JSON. Returns None if nothing usable
    can be recovered."""
    start = cleaned.find("{")
    if start == -1:
        return None
    text = cleaned[start:]
    depth = 0
    in_string = False
    escaped = False
    closers: list[str] = []
    last_complete = None  # index just past the last complete top-level value
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            closers.append("}" if char == "{" else "]")
        elif char in "}]":
            depth -= 1
            if closers:
                closers.pop()
            if depth == 1:
                last_complete = index + 1
            elif depth == 0:
                last_complete = index + 1
                break
        elif char == "," and depth == 1:
            last_complete = index

    candidates = []
    # Attempt 1: close the currently-open string (if any) plus every open
    # bracket. Recovers the most content, including a partial trailing value
    # such as a summary string that was cut mid-sentence.
    repaired = text + ('"' if in_string else "") + "".join(reversed(closers))
    candidates.append(repaired)
    # Attempt 2: drop the trailing incomplete pair and keep only the top-level
    # key/value pairs that finished cleanly. Handles truncation after a key's
    # colon or inside a non-string value, where closing brackets alone fails.
    if last_complete is not None:
        trimmed = text[:last_complete].rstrip().rstrip(",")
        if not trimmed.endswith("}"):
            trimmed += "}"
        candidates.append(trimmed)

    best: Optional[dict[str, object]] = None
    for candidate in candidates:
        try:
            recovered = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(recovered, dict) and (best is None or len(recovered) > len(best)):
            best = recovered
    return best


def parse_ai_json_response(text: str) -> dict[str, object]:
    """Parse a JSON object out of an AI response, tolerating code fences and
    leading/trailing prose. Falls back to recovering a truncated object before
    giving up. Raises ValueError if no JSON object can be recovered."""
    cleaned = _strip_json_code_fences(text)
    try:
        result = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        result = None
    if isinstance(result, dict):
        return result
    # Recover a truncated or trailing-prose top-level object before the
    # promiscuous brace scan below, so we return the real (outer) object rather
    # than a complete nested fragment (e.g. one messageReferences entry).
    salvaged = _salvage_truncated_json_object(cleaned)
    if salvaged is not None:
        return salvaged
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", cleaned):
        try:
            candidate, _ = decoder.raw_decode(cleaned[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    raise ValueError("AI response was not valid JSON.")


def format_readable_date(value: object, *, include_year: bool = False) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return "Unknown time"
    hour12 = parsed.strftime("%I:%M %p").lstrip("0")
    if include_year:
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}, {hour12} UTC"
    return f"{parsed.strftime('%b')} {parsed.day}, {hour12}"


def format_readable_date_only(value: object) -> str:
    parsed = parse_timestamp(value)
    if parsed is None:
        return "Unknown date"
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def format_timeframe(
    after_value: object,
    before_value: object,
    message_count: int,
    *,
    total_count: Optional[int] = None,
) -> str:
    start = format_readable_date_only(after_value)
    end = format_readable_date_only(before_value)
    if total_count is not None and total_count > message_count:
        return (
            f"{start} – {end}\n"
            f"{message_count:,} of {total_count:,} matching messages "
            "reviewed (most recent)"
        )
    return f"{start} – {end}\n{message_count:,} messages reviewed"


def format_bullet_list(
    items: Optional[list],
    *,
    max_items: int = 5,
    max_chars: int = FIELD_VALUE_LIMIT,
    empty_text: str = "None noted.",
) -> str:
    cleaned_items = []
    for item in items or []:
        text = clean_markdown(str(item)).strip()
        if text:
            cleaned_items.append(truncate(text, 180, text))
    if not cleaned_items:
        return empty_text
    shown = cleaned_items[:max_items]
    remaining = len(cleaned_items) - len(shown)
    lines = [f"• {item}" for item in shown]
    if remaining > 0:
        lines.append(f"…and {remaining} more.")
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = truncate(result, max_chars, empty_text)
    return result


def format_message_references(
    refs: Optional[list],
    *,
    secondary_field: str,
    secondary_template: str,
    max_items: int = 5,
    max_chars: int = FIELD_VALUE_LIMIT,
    empty_text: str = "None noted.",
) -> str:
    entries = [ref for ref in (refs or []) if isinstance(ref, dict)]
    if not entries:
        return empty_text
    shown = entries[:max_items]
    remaining = len(entries) - len(shown)
    lines = []
    for ref in shown:
        label = clean_markdown(str(ref.get("label") or "")).strip()
        descriptor = format_readable_date(ref.get("timestamp"))
        secondary_value = clean_markdown(str(ref.get(secondary_field) or "")).strip()
        if secondary_value:
            descriptor = f"{descriptor} {secondary_template.format(value=secondary_value)}"
        jump_url = safe_discord_jump_url(ref.get("jumpUrl"))
        text = f"{descriptor} — {label}" if label else descriptor
        if jump_url:
            lines.append(f"• [{descriptor}]({jump_url})" + (f" — {label}" if label else ""))
        else:
            lines.append(f"• {text}")
    if remaining > 0:
        lines.append(f"…and {remaining} more.")
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = truncate(result, max_chars, empty_text)
    return result


_TRIM_PRIORITY = (
    "Limitations",
    "Suggested Follow-up",
    "Note",
    "Useful References",
    "Recurring Patterns",
    "Potential Concerns",
    "Staff-Relevant Concerns",
    "Members Involved",
    "Positive Contributions",
    "Main Topics",
    "Activity Overview",
    "Timeframe",
)


def _embed_total_length(embed: discord.Embed) -> int:
    length = len(embed.title or "") + len(embed.description or "")
    if embed.footer and embed.footer.text:
        length += len(embed.footer.text)
    for field in embed.fields:
        length += len(field.name or "") + len(field.value or "")
    return length


def truncate_embed(embed: discord.Embed, *, max_total: int = EMBED_TOTAL_LIMIT) -> discord.Embed:
    """Trim the least-important fields first so the embed stays within Discord
    limits. Never sends an invalid (over-limit) embed."""
    guard = 0
    while _embed_total_length(embed) > max_total and guard < 100:
        guard += 1
        trimmed = False
        for name in _TRIM_PRIORITY:
            for index, field in enumerate(embed.fields):
                if field.name == name and len(field.value) > 60:
                    new_value = truncate(field.value, max(60, len(field.value) - 200), field.value)
                    embed.set_field_at(index, name=field.name, value=new_value, inline=field.inline)
                    trimmed = True
                    break
            if trimmed:
                break
        if not trimmed:
            break
    overage = _embed_total_length(embed) - max_total
    if overage > 0 and embed.description:
        new_length = max(0, len(embed.description) - overage)
        embed.description = truncate(embed.description, new_length, "")
    return embed


def build_user_context_embed(data: dict, metadata: dict) -> discord.Embed:
    summary = clean_markdown(str(data.get("summary") or ""))
    embed = branded_embed(
        metadata["title"],
        description=truncate(summary, DESCRIPTION_LIMIT, "No summary available."),
        footer=FOOTER_TEXT,
    )
    embed.add_field(name="Timeframe", value=metadata["timeframe_text"], inline=False)
    embed.add_field(
        name="Activity Overview",
        value=format_bullet_list(data.get("activityOverview")),
        inline=False,
    )
    embed.add_field(
        name="Positive Contributions",
        value=format_bullet_list(data.get("positiveContributions")),
        inline=False,
    )
    embed.add_field(
        name="Staff-Relevant Concerns",
        value=format_bullet_list(data.get("staffRelevantConcerns")),
        inline=False,
    )
    embed.add_field(
        name="Recurring Patterns",
        value=format_bullet_list(data.get("recurringPatterns")),
        inline=False,
    )
    embed.add_field(
        name="Useful References",
        value=format_message_references(
            data.get("messageReferences"),
            secondary_field="channelName",
            secondary_template="in #{value}",
        ),
        inline=False,
    )
    embed.add_field(
        name="Suggested Follow-up",
        value=format_bullet_list(
            data.get("suggestedFollowUp"),
            empty_text="No immediate staff action suggested.",
        ),
        inline=False,
    )
    limitations = clean_markdown(str(data.get("limitations") or ""))
    embed.add_field(
        name="Limitations",
        value=truncate(limitations, FIELD_VALUE_LIMIT, "No limitations noted."),
        inline=False,
    )
    return truncate_embed(embed)


def _community_score(value: object) -> int:
    """Return a display-safe whole-number community score.

    The model is instructed to produce an integer from 0 through 100, but this
    guard keeps an unexpected response from rendering an implausible score.
    """
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def build_public_user_evaluation_embed(data: dict, metadata: dict) -> discord.Embed:
    """Render the deliberately high-level, public `/context user` result.

    The caller deliberately requested public source context, so this can show
    a small set of representative quotes and their Discord jump links. Do not
    add staff concerns, staff follow-up, or a raw synthesis fallback here.
    """
    summary = clean_markdown(str(data.get("summary") or ""))
    score = _community_score(data.get("communityContributionScore"))
    embed = branded_embed(
        metadata["title"],
        description=truncate(summary, DESCRIPTION_LIMIT, "No evaluation available."),
        footer=PUBLIC_EVALUATION_FOOTER_TEXT,
    )
    embed.add_field(
        name="Community Contribution Score",
        value=f"**{score}/100**",
        inline=False,
    )
    embed.add_field(name="Timeframe", value=metadata["timeframe_text"], inline=False)
    embed.add_field(
        name="Strengths Observed",
        value=format_bullet_list(data.get("strengths")),
        inline=False,
    )
    embed.add_field(
        name="Growth Opportunities",
        value=format_bullet_list(
            data.get("growthOpportunities"),
            empty_text="No specific growth opportunities noted.",
        ),
        inline=False,
    )
    embed.add_field(
        name="Representative Context",
        value=format_message_references(
            [
                {**quote, "label": quote.get("quote", "")}
                for quote in data.get("contextQuotes", [])
                if isinstance(quote, dict)
            ],
            secondary_field="channelName",
            secondary_template="in #{value}",
            empty_text="No representative quotes were available.",
        ),
        inline=False,
    )
    limitations = clean_markdown(str(data.get("limitations") or ""))
    embed.add_field(
        name="Limitations",
        value=truncate(limitations, FIELD_VALUE_LIMIT, "No limitations noted."),
        inline=False,
    )
    return truncate_embed(embed)


def build_channel_context_embed(data: dict, metadata: dict) -> discord.Embed:
    summary = clean_markdown(str(data.get("summary") or ""))
    embed = branded_embed(
        metadata["title"],
        description=truncate(summary, DESCRIPTION_LIMIT, "No summary available."),
        footer=FOOTER_TEXT,
    )
    embed.add_field(name="Timeframe", value=metadata["timeframe_text"], inline=False)
    embed.add_field(
        name="Main Topics",
        value=format_bullet_list(data.get("mainTopics")),
        inline=False,
    )
    embed.add_field(
        name="Members Involved",
        value=format_bullet_list(data.get("membersInvolved")),
        inline=False,
    )
    embed.add_field(
        name="Potential Concerns",
        value=format_bullet_list(data.get("potentialConcerns")),
        inline=False,
    )
    embed.add_field(
        name="Useful References",
        value=format_message_references(
            data.get("messageReferences"),
            secondary_field="author",
            secondary_template="by {value}",
        ),
        inline=False,
    )
    embed.add_field(
        name="Suggested Follow-up",
        value=format_bullet_list(
            data.get("suggestedFollowUp"),
            empty_text="No immediate staff action suggested.",
        ),
        inline=False,
    )
    limitations = clean_markdown(str(data.get("limitations") or ""))
    embed.add_field(
        name="Limitations",
        value=truncate(limitations, FIELD_VALUE_LIMIT, "No limitations noted."),
        inline=False,
    )
    return truncate_embed(embed)


def build_fallback_context_embed(title: str, raw_text: str, metadata: dict) -> discord.Embed:
    """Used when the AI response could not be parsed as JSON. Renders a
    cleaned plain-text summary instead of crashing."""
    cleaned = clean_markdown(raw_text)
    embed = branded_embed(
        title,
        description=truncate(cleaned, FALLBACK_DESCRIPTION_LIMIT, "No summary available."),
        footer=f"{FOOTER_TEXT} (formatting fallback)",
    )
    timeframe_text = metadata.get("timeframe_text")
    if timeframe_text:
        embed.add_field(name="Timeframe", value=timeframe_text, inline=False)
    return truncate_embed(embed)
