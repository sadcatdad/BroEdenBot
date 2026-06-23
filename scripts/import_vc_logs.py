#!/usr/bin/env python3
"""Import historical VC sessions from DiscordChatExporter JSON logs."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional
from urllib.parse import quote

import ijson


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.exclusions import env_csv_ids, load_excluded_user_cache
from utils.vc_history import ensure_vc_history_schema


UTC = dt.timezone.utc
UTF8_BOM = b"\xef\xbb\xbf"
DEFAULT_FOLDER = Path("imports/vc_logs")
DEFAULT_DATABASE = Path("data.db")
DEFAULT_LOG_CHANNEL_ID = 1278274747913867347
SKIPPED_FOLDER_NAMES = {
    "archive",
    "archived",
    "local_archive",
    "broken",
    "broken_exports",
    "repaired",
}

USER_MENTION_RE = re.compile(r"<@!?(\d{1,25})>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d{1,25})>")
CHANNEL_LINK_RE = re.compile(
    r"\[([^\]]{1,100})\]\("
    r"(?:https?://(?:www\.)?(?:discord(?:app)?\.com)/channels/"
    r"\d{1,25}/(\d{1,25})[^)]*)\)",
    re.IGNORECASE,
)
CHANNEL_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:discord(?:app)?\.com)/channels/"
    r"\d{1,25}/(\d{1,25})",
    re.IGNORECASE,
)
ID_IN_TEXT_RE = re.compile(r"\b(\d{15,25})\b")
PLAIN_CHANNEL_RE = re.compile(r"(?:^|\s)#([a-zA-Z0-9_-]{1,100})")

MOVE_PATTERNS = (
    re.compile(r"\bmoved voice channels?\b", re.IGNORECASE),
    re.compile(r"\bswitched voice channels?\b", re.IGNORECASE),
    re.compile(r"\bchanged voice channels?\b", re.IGNORECASE),
    re.compile(r"\bvoice channel move\b", re.IGNORECASE),
    re.compile(r"\bmoved\s+from\b.+\bto\b", re.IGNORECASE | re.DOTALL),
)
LEAVE_PATTERNS = (
    re.compile(r"\bleft (?:a )?voice channel\b", re.IGNORECASE),
    re.compile(r"\bdisconnected from voice\b", re.IGNORECASE),
    re.compile(r"\bvoice (?:channel )?(?:leave|disconnect)\b", re.IGNORECASE),
)
JOIN_PATTERNS = (
    re.compile(r"\bjoined (?:a )?voice channel\b", re.IGNORECASE),
    re.compile(r"\bconnected to voice\b", re.IGNORECASE),
    re.compile(r"\bvoice (?:channel )?(?:join|connect)\b", re.IGNORECASE),
)
EVENT_LIKE_RE = re.compile(
    r"\b(?:voice|vc|joined|left|moved|switched|connected|disconnected)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChannelRef:
    channel_id: Optional[int] = None
    name: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.channel_id is not None or bool(self.name)


@dataclass
class VoiceEvent:
    event_type: str
    timestamp: dt.datetime
    source_message_id: str
    source_file: str
    user_id: Optional[int]
    user_name: Optional[str]
    old_channel: ChannelRef = field(default_factory=ChannelRef)
    new_channel: ChannelRef = field(default_factory=ChannelRef)

    @property
    def identity_key(self) -> Optional[str]:
        if self.user_id is not None:
            return f"id:{self.user_id}"
        normalized = normalize_name(self.user_name)
        return f"name:{normalized}" if normalized else None


@dataclass
class ActiveSession:
    user_id: Optional[int]
    user_name: Optional[str]
    channel: ChannelRef
    joined_at: dt.datetime
    source_start_message_id: str


@dataclass
class HistoricalSession:
    guild_id: int
    user_id: Optional[int]
    user_name: Optional[str]
    display_name: Optional[str]
    channel_id: Optional[int]
    channel_name: Optional[str]
    joined_at: dt.datetime
    left_at: dt.datetime
    duration_seconds: int
    confidence: str
    source_file: str
    source_start_message_id: str
    source_end_message_id: str
    is_estimated: bool
    close_reason: str
    dedupe_key: str = ""


@dataclass
class FileResult:
    path: Path
    messages_scanned: int = 0
    events_found: int = 0
    joins_found: int = 0
    leaves_found: int = 0
    moves_found: int = 0
    unknown_events: int = 0
    sessions_reconstructed: int = 0
    sessions_imported: int = 0
    would_import: int = 0
    duplicates_skipped: int = 0
    skipped_too_short: int = 0
    skipped_too_long: int = 0
    unmatched_joins: int = 0
    unmatched_leaves: int = 0
    open_sessions_closed: int = 0
    open_sessions_unclosed: int = 0
    vc_excluded_role_sessions_skipped: int = 0
    vc_excluded_role_duration_skipped: int = 0
    earliest_event: Optional[dt.datetime] = None
    latest_event: Optional[dt.datetime] = None
    confidence: Counter[str] = field(default_factory=Counter)
    accepted_sessions: list[HistoricalSession] = field(default_factory=list)
    new_sessions: list[HistoricalSession] = field(default_factory=list)
    failed: bool = False
    error: Optional[str] = None

    @property
    def all_sessions_were_duplicates(self) -> bool:
        return (
            bool(self.accepted_sessions)
            and self.duplicates_skipped == len(self.accepted_sessions)
            and self.sessions_imported == 0
        )


def parse_bool(value: str) -> bool:
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError("Expected true or false.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct historical VC sessions from DiscordChatExporter JSON."
        )
    )
    parser.add_argument("--file", type=Path)
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument(
        "--log-channel-id",
        type=int,
        default=DEFAULT_LOG_CHANNEL_ID,
        help="Expected source vc-log channel ID.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--archive-completed", action="store_true")
    parser.add_argument("--archive-duplicates", action="store_true")
    parser.add_argument("--archive-folder", type=Path)
    parser.add_argument(
        "--close-open-at-export-end",
        type=parse_bool,
        default=True,
        metavar="true|false",
    )
    parser.add_argument("--min-session-seconds", type=int, default=10)
    parser.add_argument("--max-session-hours", type=float, default=24)
    parser.add_argument(
        "--excluded-user-cache",
        type=Path,
        help=(
            "JSON cache of user IDs resolved from VC_EXCLUDED_ROLE_IDS. "
            "Name-only sessions are not role-excluded."
        ),
    )
    args = parser.parse_args()
    if args.min_session_seconds < 0:
        parser.error("--min-session-seconds cannot be negative")
    if args.max_session_hours <= 0:
        parser.error("--max-session-hours must be greater than zero")
    return args


def vc_excluded_user_ids(args: argparse.Namespace) -> set[int]:
    excluded = env_csv_ids("VC_EXCLUDED_USER_IDS")
    if getattr(args, "excluded_user_cache", None):
        excluded.update(load_excluded_user_cache(args.excluded_user_cache))
    return excluded


def parse_int(value: Any) -> Optional[int]:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def parse_timestamp(value: Any) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for pattern in (
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = dt.datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_name(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def clean_markdown(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = USER_MENTION_RE.sub("", text)
    text = CHANNEL_MENTION_RE.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_~`>|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-:•")
    return text


def plausible_name(value: Optional[str]) -> Optional[str]:
    cleaned = clean_markdown(value)
    if not cleaned or len(cleaned) > 100:
        return None
    lowered = cleaned.casefold()
    generic = {
        "member",
        "user",
        "unknown",
        "voice",
        "voice channel",
        "channel",
        "joined",
        "left",
        "moved",
    }
    if lowered in generic or "voice channel" in lowered:
        return None
    return cleaned


@contextmanager
def open_json_stream(path: Path) -> Iterator[BinaryIO]:
    handle = path.open("rb")
    try:
        if handle.read(len(UTF8_BOM)) != UTF8_BOM:
            handle.seek(0)
        yield handle
    finally:
        handle.close()


def json_root_type(path: Path) -> str:
    with open_json_stream(path) as handle:
        try:
            _, event, _ = next(ijson.parse(handle))
        except StopIteration as exc:
            raise ValueError("JSON export is empty") from exc
    if event == "start_map":
        return "object"
    if event == "start_array":
        return "array"
    raise ValueError("JSON export must contain an object or array")


def export_channel_id(path: Path, root_type: str) -> Optional[int]:
    if root_type != "object":
        return None
    with open_json_stream(path) as handle:
        for prefix, event, value in ijson.parse(handle):
            if prefix == "messages" and event == "start_array":
                break
            if prefix in {"channel.id", "channelId", "channel_id"}:
                parsed = parse_int(value)
                if parsed is not None:
                    return parsed
    return None


def json_messages(path: Path) -> Iterator[dict[str, Any]]:
    root_type = json_root_type(path)
    item_prefix = "messages.item" if root_type == "object" else "item"
    if root_type == "object":
        with open_json_stream(path) as handle:
            has_messages = any(
                prefix == "messages" and event == "start_array"
                for prefix, event, _ in ijson.parse(handle)
            )
        if not has_messages:
            raise ValueError("JSON export does not contain a messages array")
    with open_json_stream(path) as handle:
        for item in ijson.items(handle, item_prefix):
            if isinstance(item, dict):
                yield item


def normalized_label(value: Any) -> str:
    return re.sub(r"[^a-z]+", " ", str(value or "").casefold()).strip()


def embed_fields(message: dict[str, Any]) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    for embed in message.get("embeds") or []:
        if not isinstance(embed, dict):
            continue
        for raw_field in embed.get("fields") or []:
            if not isinstance(raw_field, dict):
                continue
            fields.append(
                (
                    str(raw_field.get("name") or ""),
                    str(raw_field.get("value") or ""),
                )
            )
        for line in str(embed.get("description") or "").splitlines():
            match = re.match(
                r"^\s*\*{0,2}\+?([^:*]{1,40}):\*{0,2}\s*(.+?)\s*$",
                line,
            )
            if match:
                fields.append((match.group(1), match.group(2)))
    return fields


def message_text(message: dict[str, Any]) -> str:
    parts = [str(message.get("content") or "")]
    for embed in message.get("embeds") or []:
        if not isinstance(embed, dict):
            continue
        parts.extend(
            [
                str(embed.get("title") or ""),
                str(embed.get("description") or ""),
            ]
        )
        footer = embed.get("footer")
        if isinstance(footer, dict):
            parts.append(str(footer.get("text") or ""))
        for name, value in embed_fields({"embeds": [embed]}):
            parts.extend([name, value])
    return "\n".join(part for part in parts if part)


def classify_event(text: str) -> Optional[str]:
    if any(pattern.search(text) for pattern in MOVE_PATTERNS):
        return "move"
    if any(pattern.search(text) for pattern in LEAVE_PATTERNS):
        return "leave"
    if any(pattern.search(text) for pattern in JOIN_PATTERNS):
        return "join"
    if EVENT_LIKE_RE.search(text) and re.search(
        r"\b(?:channel|voice|vc)\b", text, re.IGNORECASE
    ):
        return "unknown"
    return None


def channel_refs_in_text(value: str) -> list[ChannelRef]:
    matches: list[tuple[int, ChannelRef]] = []
    occupied_ids: set[int] = set()
    for match in CHANNEL_LINK_RE.finditer(value):
        channel_id = parse_int(match.group(2))
        if channel_id:
            occupied_ids.add(channel_id)
            matches.append(
                (
                    match.start(),
                    ChannelRef(channel_id, plausible_name(match.group(1))),
                )
            )
    for match in CHANNEL_MENTION_RE.finditer(value):
        channel_id = parse_int(match.group(1))
        if channel_id and channel_id not in occupied_ids:
            occupied_ids.add(channel_id)
            matches.append((match.start(), ChannelRef(channel_id, None)))
    for match in CHANNEL_URL_RE.finditer(value):
        channel_id = parse_int(match.group(1))
        if channel_id and channel_id not in occupied_ids:
            occupied_ids.add(channel_id)
            matches.append((match.start(), ChannelRef(channel_id, None)))
    matches.sort(key=lambda item: item[0])
    return [item[1] for item in matches]


def channel_ref_from_value(value: Any) -> ChannelRef:
    text = str(value or "")
    refs = channel_refs_in_text(text)
    if refs:
        first = refs[0]
        if first.name:
            return first
        without_ref = CHANNEL_MENTION_RE.sub("", text)
        without_ref = CHANNEL_URL_RE.sub("", without_ref)
        name = plausible_name(without_ref)
        return ChannelRef(first.channel_id, name)
    plain_match = PLAIN_CHANNEL_RE.search(text)
    if plain_match:
        return ChannelRef(None, plain_match.group(1))
    id_match = ID_IN_TEXT_RE.search(text)
    possible_id = parse_int(id_match.group(1)) if id_match else None
    cleaned = plausible_name(ID_IN_TEXT_RE.sub("", text))
    if cleaned:
        cleaned = re.sub(r"^#\s*", "", cleaned).strip()
    return ChannelRef(possible_id, cleaned)


def semantic_channel_fields(
    fields: list[tuple[str, str]],
) -> tuple[ChannelRef, ChannelRef, ChannelRef]:
    old_ref = ChannelRef()
    new_ref = ChannelRef()
    generic_ref = ChannelRef()
    for raw_label, value in fields:
        label = normalized_label(raw_label)
        if not any(
            word in label.split()
            for word in (
                "channel",
                "voice",
                "old",
                "from",
                "before",
                "previous",
                "new",
                "to",
                "after",
                "current",
            )
        ):
            continue
        ref = channel_ref_from_value(value)
        if not ref.usable:
            continue
        if any(word in label.split() for word in ("old", "from", "before", "previous")):
            old_ref = ref
        elif any(word in label.split() for word in ("new", "to", "after", "current")):
            new_ref = ref
        elif "channel" in label or "voice" in label:
            generic_ref = ref
    return old_ref, new_ref, generic_ref


def user_from_message(
    message: dict[str, Any],
    text: str,
    fields: list[tuple[str, str]],
) -> tuple[Optional[int], Optional[str]]:
    user_id = None
    user_name = None
    for raw_label, value in fields:
        label = normalized_label(raw_label)
        if not any(word in label.split() for word in ("member", "user", "target")):
            continue
        match = USER_MENTION_RE.search(value)
        if match:
            user_id = parse_int(match.group(1))
        candidate = plausible_name(USER_MENTION_RE.sub("", value))
        if candidate:
            user_name = candidate
        if user_id is not None and user_name:
            break

    mentions = [
        mention
        for mention in (message.get("mentions") or [])
        if isinstance(mention, dict)
    ]
    mention_by_id = {
        parse_int(mention.get("id")): mention
        for mention in mentions
        if parse_int(mention.get("id")) is not None
    }
    if user_id is None:
        match = USER_MENTION_RE.search(text)
        if match:
            user_id = parse_int(match.group(1))
    if user_id is None and mentions:
        user_id = parse_int(mentions[0].get("id"))
    if user_id is None:
        for embed in message.get("embeds") or []:
            if not isinstance(embed, dict):
                continue
            footer = embed.get("footer")
            footer_text = (
                str(footer.get("text") or "")
                if isinstance(footer, dict)
                else ""
            )
            match = re.search(r"\bID\s*:\s*(\d{1,25})\b", footer_text, re.I)
            if match:
                user_id = parse_int(match.group(1))
                if user_id is not None:
                    break
    if user_id is not None and not user_name:
        mention = mention_by_id.get(user_id)
        if mention:
            user_name = plausible_name(
                mention.get("nickname")
                or mention.get("displayName")
                or mention.get("name")
            )

    if user_id is None and not user_name:
        for pattern in (
            r"(?:^|\n)(.{1,100}?)\s+joined (?:a )?voice channel",
            r"(?:^|\n)(.{1,100}?)\s+left (?:a )?voice channel",
            r"(?:^|\n)(.{1,100}?)\s+(?:moved|switched) voice channels?",
            r"(?:^|\n)(.{1,100}?)\s+changed voice channels?",
            r"(?:^|\n)(.{1,100}?)\s+(?:connected to|disconnected from) voice",
            r"(?:^|\n)(.{1,100}?)\s+joined\s+",
            r"(?:^|\n)(.{1,100}?)\s+left\s+",
            r"(?:^|\n)(.{1,100}?)\s+(?:moved|changed)\s+from\s+",
        ):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                user_name = plausible_name(match.group(1))
                if user_name:
                    break
            if user_name:
                break

    author = message.get("author")
    if user_id is None and not user_name and isinstance(author, dict):
        is_bot = str(author.get("isBot") or "").strip().casefold() in {
            "1",
            "true",
            "yes",
        }
        if not is_bot:
            user_id = parse_int(author.get("id"))
            user_name = plausible_name(
                author.get("nickname") or author.get("name")
            )
    return user_id, user_name


def phrase_move_channels(text: str) -> tuple[ChannelRef, ChannelRef]:
    match = re.search(
        r"\b(?:moved|changed)(?:\s+voice\s+channels?)?\s+from\s+"
        r"(.{1,150}?)\s+to\s+(.{1,150}?)(?:\n|$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ChannelRef(), ChannelRef()
    return channel_ref_from_value(match.group(1)), channel_ref_from_value(
        match.group(2)
    )


def phrase_channel(text: str, event_type: str) -> ChannelRef:
    verbs = {
        "join": ("joined", "connected to"),
        "leave": ("left", "disconnected from"),
    }.get(event_type, ())
    for line in text.splitlines():
        lowered = clean_markdown(line).casefold()
        if not lowered:
            continue
        for verb in verbs:
            marker = f" {verb} "
            if marker not in f" {lowered} ":
                continue
            before, after = re.split(
                rf"\b{re.escape(verb)}\b",
                line,
                maxsplit=1,
                flags=re.IGNORECASE,
            )
            if not plausible_name(before):
                continue
            after = re.sub(
                r"^\s*(?:the\s+)?(?:voice\s+)?channel\s*",
                "",
                after,
                flags=re.IGNORECASE,
            )
            ref = channel_ref_from_value(after)
            if ref.usable:
                return ref
    return ChannelRef()


def parse_voice_event(
    message: dict[str, Any],
    source_file: str,
) -> Optional[VoiceEvent]:
    text = message_text(message)
    event_type = classify_event(text)
    if event_type is None:
        return None
    fields = embed_fields(message)
    timestamp = parse_timestamp(message.get("timestamp"))
    if timestamp is None:
        for embed in message.get("embeds") or []:
            if isinstance(embed, dict):
                timestamp = parse_timestamp(embed.get("timestamp"))
                if timestamp:
                    break
    if timestamp is None:
        event_type = "unknown"
        timestamp = dt.datetime.min.replace(tzinfo=UTC)

    message_id = str(message.get("id") or "")
    user_id, user_name = user_from_message(message, text, fields)
    old_ref, new_ref, generic_ref = semantic_channel_fields(fields)
    all_refs = channel_refs_in_text(text)

    if event_type == "move":
        phrase_old, phrase_new = phrase_move_channels(text)
        if not old_ref.usable:
            old_ref = phrase_old
        if not new_ref.usable:
            new_ref = phrase_new
        if not old_ref.usable and all_refs:
            old_ref = all_refs[0]
        if not new_ref.usable and len(all_refs) >= 2:
            new_ref = all_refs[1]
        elif not new_ref.usable and generic_ref.usable:
            new_ref = generic_ref
    elif event_type == "join":
        if not new_ref.usable:
            new_ref = generic_ref
        if not new_ref.usable and all_refs:
            new_ref = all_refs[-1]
        if not new_ref.usable:
            new_ref = phrase_channel(text, "join")
    elif event_type == "leave":
        if not old_ref.usable:
            old_ref = generic_ref
        if not old_ref.usable and all_refs:
            old_ref = all_refs[-1]
        if not old_ref.usable:
            old_ref = phrase_channel(text, "leave")

    if (user_id is None and not user_name) or timestamp.year == 1:
        event_type = "unknown"
    return VoiceEvent(
        event_type=event_type,
        timestamp=timestamp,
        source_message_id=message_id,
        source_file=source_file,
        user_id=user_id,
        user_name=user_name,
        old_channel=old_ref,
        new_channel=new_ref,
    )


def session_confidence(
    user_id: Optional[int],
    channel_id: Optional[int],
) -> str:
    if user_id is None:
        return "low"
    if channel_id is None:
        return "medium"
    return "high"


def dedupe_key(session: HistoricalSession) -> str:
    user_identity = (
        str(session.user_id)
        if session.user_id is not None
        else f"name:{normalize_name(session.user_name)}"
    )
    channel_identity = (
        str(session.channel_id)
        if session.channel_id is not None
        else f"name:{normalize_name(session.channel_name)}"
    )
    parts = (
        "imported_vc_log",
        str(session.guild_id),
        quote(user_identity, safe=""),
        quote(channel_identity, safe=""),
        session.joined_at.isoformat(),
        session.left_at.isoformat(),
        quote(session.source_start_message_id or "", safe=""),
        quote(session.source_end_message_id or "", safe=""),
    )
    return ":".join(parts)


def closed_session(
    guild_id: int,
    active: ActiveSession,
    event: VoiceEvent,
    *,
    estimated: bool,
    close_reason: str,
) -> HistoricalSession:
    duration = int((event.timestamp - active.joined_at).total_seconds())
    session = HistoricalSession(
        guild_id=guild_id,
        user_id=active.user_id,
        user_name=active.user_name or event.user_name,
        display_name=active.user_name or event.user_name,
        channel_id=active.channel.channel_id,
        channel_name=active.channel.name,
        joined_at=active.joined_at,
        left_at=event.timestamp,
        duration_seconds=duration,
        confidence=session_confidence(
            active.user_id,
            active.channel.channel_id,
        ),
        source_file=event.source_file,
        source_start_message_id=active.source_start_message_id,
        source_end_message_id=event.source_message_id,
        is_estimated=estimated,
        close_reason=close_reason,
    )
    session.dedupe_key = dedupe_key(session)
    return session


def reconstruct_sessions(
    events: list[VoiceEvent],
    result: FileResult,
    args: argparse.Namespace,
) -> list[HistoricalSession]:
    active: dict[str, ActiveSession] = {}
    reconstructed: list[HistoricalSession] = []
    ordered = sorted(
        (event for event in events if event.event_type != "unknown"),
        key=lambda event: (
            event.timestamp,
            int(event.source_message_id)
            if event.source_message_id.isdigit()
            else 0,
        ),
    )

    for event in ordered:
        identity = event.identity_key
        if identity is None:
            continue
        if event.event_type == "join":
            if identity in active:
                reconstructed.append(
                    closed_session(
                        args.guild_id,
                        active.pop(identity),
                        event,
                        estimated=True,
                        close_reason="closed_by_rejoin",
                    )
                )
                result.unmatched_joins += 1
            if event.new_channel.usable:
                active[identity] = ActiveSession(
                    event.user_id,
                    event.user_name,
                    event.new_channel,
                    event.timestamp,
                    event.source_message_id,
                )
            else:
                result.unmatched_joins += 1
        elif event.event_type == "leave":
            current = active.pop(identity, None)
            if current is None:
                result.unmatched_leaves += 1
                continue
            reconstructed.append(
                closed_session(
                    args.guild_id,
                    current,
                    event,
                    estimated=False,
                    close_reason="leave",
                )
            )
        elif event.event_type == "move":
            current = active.pop(identity, None)
            if current is not None:
                reconstructed.append(
                    closed_session(
                        args.guild_id,
                        current,
                        event,
                        estimated=False,
                        close_reason="move",
                    )
                )
            elif event.old_channel.usable:
                result.unmatched_leaves += 1
            if event.new_channel.usable:
                active[identity] = ActiveSession(
                    event.user_id,
                    event.user_name,
                    event.new_channel,
                    event.timestamp,
                    event.source_message_id,
                )

    if active and args.close_open_at_export_end and result.latest_event:
        end_event = VoiceEvent(
            event_type="leave",
            timestamp=result.latest_event,
            source_message_id="export_end",
            source_file=result.path.name,
            user_id=None,
            user_name=None,
        )
        for current in active.values():
            reconstructed.append(
                closed_session(
                    args.guild_id,
                    current,
                    end_event,
                    estimated=True,
                    close_reason="closed_at_export_end",
                )
            )
            result.open_sessions_closed += 1
    elif active:
        result.open_sessions_unclosed = len(active)
        result.unmatched_joins += len(active)

    result.sessions_reconstructed = len(reconstructed)
    max_seconds = int(args.max_session_hours * 60 * 60)
    accepted = []
    for session in reconstructed:
        if session.duration_seconds < args.min_session_seconds:
            result.skipped_too_short += 1
            continue
        if session.duration_seconds > max_seconds:
            result.skipped_too_long += 1
            continue
        accepted.append(session)
        result.confidence[session.confidence] += 1
    return accepted


def table_exists(
    connection: Optional[sqlite3.Connection],
    table_name: str,
) -> bool:
    if connection is None:
        return False
    return bool(
        connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
    )


def session_exists(
    connection: Optional[sqlite3.Connection],
    key: str,
) -> bool:
    if not table_exists(connection, "vc_imported_sessions"):
        return False
    return bool(
        connection.execute(
            "SELECT 1 FROM vc_imported_sessions WHERE dedupe_key = ?",
            (key,),
        ).fetchone()
    )


def insert_session(
    connection: sqlite3.Connection,
    session: HistoricalSession,
    imported_at: str,
) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO vc_imported_sessions (
            guild_id, user_id, user_name, display_name,
            voice_channel_id, voice_channel_name,
            joined_at, left_at, duration_seconds,
            counted_seconds, reward_eligible, source, confidence,
            source_file, source_start_message_id, source_end_message_id,
            imported_at, dedupe_key, is_imported, is_estimated, close_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'imported_vc_log', ?,
                  ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            session.guild_id,
            session.user_id,
            session.user_name,
            session.display_name,
            session.channel_id,
            session.channel_name,
            session.joined_at.isoformat(),
            session.left_at.isoformat(),
            session.duration_seconds,
            session.confidence,
            session.source_file,
            session.source_start_message_id,
            session.source_end_message_id,
            imported_at,
            session.dedupe_key,
            int(session.is_estimated),
            session.close_reason,
        ),
    )
    return cursor.rowcount > 0


def safe_error(exc: Exception) -> str:
    if isinstance(exc, ijson.common.JSONError):
        return "Invalid or incomplete JSON export."
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, sqlite3.Error):
        return f"SQLite {type(exc).__name__}."
    if isinstance(exc, OSError):
        return f"{type(exc).__name__} while reading the export."
    return f"{type(exc).__name__} while processing the export."


def process_file(
    connection: Optional[sqlite3.Connection],
    path: Path,
    args: argparse.Namespace,
) -> FileResult:
    result = FileResult(path=path)
    events: list[VoiceEvent] = []
    try:
        root_type = json_root_type(path)
        actual_channel_id = export_channel_id(path, root_type)
        if (
            actual_channel_id is not None
            and args.log_channel_id
            and actual_channel_id != args.log_channel_id
        ):
            raise ValueError(
                "Export channel ID does not match the configured vc-log channel."
            )
        for message in json_messages(path):
            result.messages_scanned += 1
            event = parse_voice_event(message, path.name)
            if event is None:
                continue
            if event.event_type == "unknown":
                result.unknown_events += 1
                events.append(event)
                continue
            result.events_found += 1
            if event.event_type == "join":
                result.joins_found += 1
            elif event.event_type == "leave":
                result.leaves_found += 1
            elif event.event_type == "move":
                result.moves_found += 1
            if event.timestamp.year > 1:
                if (
                    result.earliest_event is None
                    or event.timestamp < result.earliest_event
                ):
                    result.earliest_event = event.timestamp
                if (
                    result.latest_event is None
                    or event.timestamp > result.latest_event
                ):
                    result.latest_event = event.timestamp
            events.append(event)
    except Exception as exc:
        result.failed = True
        result.error = safe_error(exc)
        return result

    excluded_user_ids = vc_excluded_user_ids(args)
    accepted = []
    for session in reconstruct_sessions(events, result, args):
        if session.user_id is not None and session.user_id in excluded_user_ids:
            result.vc_excluded_role_sessions_skipped += 1
            result.vc_excluded_role_duration_skipped += session.duration_seconds
            continue
        accepted.append(session)
    result.accepted_sessions = accepted
    imported_at = dt.datetime.now(UTC).isoformat()
    try:
        for session in accepted:
            if session_exists(connection, session.dedupe_key):
                result.duplicates_skipped += 1
                continue
            if args.dry_run:
                result.would_import += 1
                result.new_sessions.append(session)
                continue
            if connection is None:
                raise RuntimeError("Database connection is unavailable.")
            if insert_session(connection, session, imported_at):
                result.sessions_imported += 1
                result.new_sessions.append(session)
            else:
                result.duplicates_skipped += 1
        if connection is not None and not args.dry_run:
            connection.commit()
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        result.failed = True
        result.error = safe_error(exc)
        result.sessions_imported = 0
        result.new_sessions.clear()
    return result


def input_files(args: argparse.Namespace) -> list[Path]:
    if args.file:
        return [args.file]
    if not args.folder.exists():
        return []
    archive = (
        args.archive_folder.resolve()
        if args.archive_folder
        else (args.folder / "archive").resolve()
    )
    files = []
    for root, directory_names, filenames in os.walk(args.folder):
        root_path = Path(root)
        if (
            root_path.name.casefold() in SKIPPED_FOLDER_NAMES
            or root_path.resolve() == archive
        ):
            directory_names[:] = []
            continue
        directory_names[:] = [
            name
            for name in directory_names
            if name.casefold() not in SKIPPED_FOLDER_NAMES
            and (root_path / name).resolve() != archive
        ]
        files.extend(
            root_path / filename
            for filename in filenames
            if Path(filename).suffix.casefold() == ".json"
        )
    return sorted(files)


def archive_file(
    path: Path,
    result: FileResult,
    args: argparse.Namespace,
) -> Optional[Path]:
    if args.dry_run or not args.archive_completed or result.failed:
        return None
    if result.all_sessions_were_duplicates and not args.archive_duplicates:
        return None
    archive_folder = args.archive_folder or path.parent / "archive"
    archive_folder.mkdir(parents=True, exist_ok=True)
    target = archive_folder / path.name
    if target.exists():
        timestamp = dt.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = archive_folder / f"{path.stem}-{timestamp}{path.suffix}"
        counter = 2
        while target.exists():
            target = archive_folder / (
                f"{path.stem}-{timestamp}-{counter}{path.suffix}"
            )
            counter += 1
    shutil.move(str(path), str(target))
    return target


def timestamp_label(value: Optional[dt.datetime]) -> str:
    return value.isoformat() if value else "none"


def print_file_summary(result: FileResult, dry_run: bool) -> None:
    print(f"\nFile: {result.path.name}")
    if result.failed:
        print(f"  status: failed ({result.error})")
    else:
        print("  status: completed")
    print(f"  messages scanned: {result.messages_scanned}")
    print(f"  parseable VC events: {result.events_found}")
    print(
        "  joins / leaves / moves: "
        f"{result.joins_found} / {result.leaves_found} / {result.moves_found}"
    )
    print(f"  unknown event-like messages: {result.unknown_events}")
    print(f"  sessions reconstructed: {result.sessions_reconstructed}")
    if dry_run:
        print(f"  sessions that would import: {result.would_import}")
    else:
        print(f"  sessions imported: {result.sessions_imported}")
    print(f"  duplicate sessions skipped: {result.duplicates_skipped}")
    print(
        "  vc excluded-role sessions skipped: "
        f"{result.vc_excluded_role_sessions_skipped}"
    )
    print(
        "  vc excluded-role duration skipped: "
        f"{format_duration(result.vc_excluded_role_duration_skipped)}"
    )
    print(f"  skipped too short: {result.skipped_too_short}")
    print(f"  skipped too long/suspicious: {result.skipped_too_long}")
    print(f"  unmatched joins: {result.unmatched_joins}")
    print(f"  unmatched leaves: {result.unmatched_leaves}")
    print(f"  open sessions closed at export end: {result.open_sessions_closed}")
    print(f"  open sessions left unclosed: {result.open_sessions_unclosed}")
    print(f"  earliest event: {timestamp_label(result.earliest_event)}")
    print(f"  latest event: {timestamp_label(result.latest_event)}")
    print(
        "  confidence high / medium / low: "
        f"{result.confidence['high']} / "
        f"{result.confidence['medium']} / "
        f"{result.confidence['low']}"
    )


def format_duration(seconds: int) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{remainder}s"


def print_top_totals(
    label: str,
    totals: Counter[tuple[str, str]],
) -> None:
    print(f"\n{label}:")
    if not totals:
        print("  none")
        return
    for index, ((identity, name), seconds) in enumerate(
        totals.most_common(10),
        start=1,
    ):
        display = name or identity
        print(f"  {index}. {display} — {format_duration(seconds)}")


def print_final_summary(
    results: list[FileResult],
    dry_run: bool,
) -> None:
    successful = [result for result in results if not result.failed]
    new_sessions = [
        session for result in successful for session in result.new_sessions
    ]
    user_totals: Counter[tuple[str, str]] = Counter()
    channel_totals: Counter[tuple[str, str]] = Counter()
    for session in new_sessions:
        user_identity = (
            str(session.user_id)
            if session.user_id is not None
            else f"name:{normalize_name(session.user_name)}"
        )
        channel_identity = (
            str(session.channel_id)
            if session.channel_id is not None
            else f"name:{normalize_name(session.channel_name)}"
        )
        user_totals[(user_identity, session.display_name or session.user_name or "")] += (
            session.duration_seconds
        )
        channel_totals[(channel_identity, session.channel_name or "")] += (
            session.duration_seconds
        )

    print("\nFinal summary")
    print(f"  files processed: {len(results)}")
    print(f"  successful files: {len(successful)}")
    print(f"  failed files: {sum(result.failed for result in results)}")
    print(f"  total events parsed: {sum(result.events_found for result in results)}")
    print(
        "  total sessions reconstructed: "
        f"{sum(result.sessions_reconstructed for result in results)}"
    )
    if dry_run:
        print(
            "  total sessions that would import: "
            f"{sum(result.would_import for result in results)}"
        )
    else:
        print(
            "  total sessions imported: "
            f"{sum(result.sessions_imported for result in results)}"
        )
    print(
        "  total duplicate sessions skipped: "
        f"{sum(result.duplicates_skipped for result in results)}"
    )
    print(
        "  vc_excluded_role_sessions_skipped: "
        f"{sum(result.vc_excluded_role_sessions_skipped for result in results)}"
    )
    print(
        "  vc_excluded_role_duration_skipped: "
        f"{format_duration(sum(result.vc_excluded_role_duration_skipped for result in results))}"
    )
    duration_label = (
        "total historical VC duration that would import"
        if dry_run
        else "total historical VC duration imported"
    )
    print(
        f"  {duration_label}: "
        f"{format_duration(sum(session.duration_seconds for session in new_sessions))}"
    )
    top_action = "would-import" if dry_run else "imported"
    print_top_totals(f"Top 10 users by {top_action} VC time", user_totals)
    print_top_totals(
        f"Top 10 voice channels by {top_action} VC time",
        channel_totals,
    )


def open_connection(
    args: argparse.Namespace,
) -> Optional[sqlite3.Connection]:
    if args.dry_run:
        if not args.database.exists():
            return None
        connection = sqlite3.connect(
            f"file:{args.database.resolve()}?mode=ro",
            uri=True,
            timeout=30,
        )
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection
    args.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.database, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    ensure_vc_history_schema(connection)
    return connection


def main() -> int:
    args = parse_args()
    files = input_files(args)
    if not files:
        print("No DiscordChatExporter JSON files were found.")
        return 1

    connection = open_connection(args)
    results = []
    try:
        for path in files:
            result = process_file(connection, path, args)
            results.append(result)
            print_file_summary(result, args.dry_run)
            archived = archive_file(path, result, args)
            if archived:
                print(f"  archived as: {archived.name}")
    finally:
        if connection is not None:
            connection.close()

    print_final_summary(results, args.dry_run)
    return 1 if any(result.failed for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
