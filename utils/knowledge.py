import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from utils.live_knowledge import excerpt_for_terms, score_entry
from utils.settings import settings_database_path
from utils.sqlite import AutoClosingSQLiteConnection, configure_sync_connection


PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"
KNOWLEDGE_FILES: Dict[str, Path] = {}
STAFF_KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "staff_knowledge"
STAFF_KNOWLEDGE_FILES: Dict[str, Path] = {}

COMMUNITY_CONTEXT = """
Bro Eden is an 18+ Discord community for gay, bi, and queer men.
Mature or adult discussion is allowed in appropriate spaces.
Adult language or NSFW context alone is not automatically a violation.
Evaluate channel context, consent, member roles and DM boundaries, whether
conduct is targeted or persistent, and its likely impact on the community.
""".strip()


@lru_cache(maxsize=1)
def load_knowledge() -> Dict[str, str]:
    """Load and cache local Bro Eden knowledge without raising on missing files."""
    knowledge = {}
    for label, path in KNOWLEDGE_FILES.items():
        try:
            knowledge[label] = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            knowledge[label] = ""
    return knowledge


def load_server_knowledge() -> Dict[str, str]:
    """Return a copy of the public-safe server knowledge used by member tools."""
    return dict(load_knowledge())


@lru_cache(maxsize=1)
def load_staff_knowledge() -> Dict[str, str]:
    """Load public guidance plus private staff-only operational knowledge."""
    knowledge = dict(load_knowledge())
    for label, path in STAFF_KNOWLEDGE_FILES.items():
        try:
            knowledge[label] = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            knowledge[label] = ""
    return knowledge


def reload_knowledge() -> Dict[str, int]:
    """Clear both knowledge caches and reload their existing source files."""
    load_knowledge.cache_clear()
    load_staff_knowledge.cache_clear()
    public = load_server_knowledge()
    staff = load_staff_knowledge()
    return {
        "public_sources": sum(bool(content) for content in public.values()),
        "staff_sources": sum(bool(content) for content in staff.values()),
    }


def compact_knowledge_context(max_chars: int = 18_000) -> str:
    """Return public and staff-only context for private moderation prompts."""
    sections = [COMMUNITY_CONTEXT]
    for label, content in load_staff_knowledge().items():
        if content:
            sections.append(f"## {label}\n{content}")
    for label, content in _load_live_knowledge_sources(
        visibility="staff",
        max_sources=30,
    ).items():
        if content:
            sections.append(f"## {label}\n{content}")
    context = "\n\n".join(sections)
    if len(context) <= max_chars:
        return context
    return context[: max_chars - 1].rstrip() + "…"


def build_public_ask_context(query: str, max_chars: int = 12_000) -> str:
    """Build member-safe context from public live knowledge sources."""
    sections = []
    matches = search_server_knowledge(query, max_results=6)
    if matches:
        excerpts = [
            f"### {source} — {heading}\n{excerpt}"
            for source, heading, excerpt in matches
        ]
        sections.append("## Most relevant public sections\n" + "\n\n".join(excerpts))

    public_sources = [
        f"## {label}\n{content}"
        for label, content in load_server_knowledge().items()
        if content
    ]
    sections.extend(public_sources)
    context = "\n\n".join(sections)
    if len(context) <= max_chars:
        return context
    return context[: max_chars - 1].rstrip() + "…"


def search_knowledge(
    query: str,
    max_results: int = 5,
    max_excerpt_chars: int = 700,
) -> List[Tuple[str, str, str]]:
    """Search public and staff-only knowledge for private staff tools."""
    results = _search_sources(
        load_staff_knowledge(),
        query,
        max_results,
        max_excerpt_chars,
    )
    results.extend(
        _search_live_knowledge(
            query,
            visibility="staff",
            max_results=max_results,
            max_excerpt_chars=max_excerpt_chars,
        )
    )
    return _rank_results(results, query, max_results)


def build_staff_knowledge_context(
    query: str,
    max_results: int = 6,
    max_chars: int = 5_000,
) -> str:
    """Build relevant private knowledge context for staff-facing AI tools."""
    results = search_knowledge(
        query,
        max_results=max_results,
        max_excerpt_chars=900,
    )
    context = "\n\n".join(
        f"### {source} — {heading}\n{excerpt}"
        for source, heading, excerpt in results
    )
    if len(context) <= max_chars:
        return context
    return context[: max_chars - 1].rstrip() + "…"


def _search_sources(
    sources: Dict[str, str],
    query: str,
    max_results: int,
    max_excerpt_chars: int,
) -> List[Tuple[str, str, str]]:
    """Rank Markdown sections from an explicit source set."""
    terms = _query_terms(query)
    if not terms:
        return []

    matches = []
    for source, content in sources.items():
        for heading, section in _sections(content):
            haystack = f"{heading} {section}".casefold()
            score = sum(haystack.count(term) for term in terms)
            heading_score = sum(
                2 for term in terms if term in heading.casefold()
            )
            score += heading_score
            if score:
                excerpt = re.sub(r"\s+", " ", section).strip()
                if len(excerpt) > max_excerpt_chars:
                    excerpt = excerpt[: max_excerpt_chars - 1].rstrip() + "…"
                matches.append((score, source, heading, excerpt))

    matches.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [
        (source, heading, excerpt)
        for _, source, heading, excerpt in matches[:max_results]
    ]


def search_server_knowledge(
    query: str,
    max_results: int = 5,
    max_excerpt_chars: int = 700,
) -> List[Tuple[str, str, str]]:
    """Search only the public-safe server knowledge."""
    results = _search_sources(
        load_server_knowledge(),
        query,
        max_results,
        max_excerpt_chars,
    )
    results.extend(
        _search_live_knowledge(
            query,
            visibility="public",
            max_results=max_results,
            max_excerpt_chars=max_excerpt_chars,
        )
    )
    return _rank_results(results, query, max_results)


def _connect_live_knowledge() -> sqlite3.Connection:
    connection = sqlite3.connect(
        settings_database_path(),
        timeout=30,
        factory=AutoClosingSQLiteConnection,
    )
    connection.row_factory = sqlite3.Row
    return configure_sync_connection(connection)


def _live_tables_available(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'knowledge_entries'
        """
    ).fetchone()
    return row is not None


def _visibility_filter(visibility: str) -> tuple[str, tuple[str, ...]]:
    if visibility in {"staff", "staff_only"}:
        return "visibility IN (?, ?)", ("public", "staff_only")
    return "visibility = ?", ("public",)


def _search_live_knowledge(
    query: str,
    *,
    visibility: str,
    max_results: int,
    max_excerpt_chars: int,
) -> List[Tuple[str, str, str]]:
    terms = _query_terms(query)
    if not terms:
        return []
    clause, values = _visibility_filter(visibility)
    try:
        with _connect_live_knowledge() as connection:
            if not _live_tables_available(connection):
                return []
            rows = connection.execute(
                f"""
                SELECT
                    source_channel_id, source_message_id, source_type,
                    visibility, title, content, indexed_at
                FROM knowledge_entries
                WHERE {clause}
                ORDER BY indexed_at DESC, id DESC
                LIMIT 300
                """,
                values,
            ).fetchall()
    except sqlite3.Error:
        return []

    matches = []
    for row in rows:
        score = score_entry(query, row["title"] or "", row["content"] or "")
        if score <= 0:
            continue
        source = (
            f"Live Discord #{row['source_channel_id']} "
            f"({row['source_type']})"
        )
        heading = row["title"] or "Discord Knowledge"
        excerpt = excerpt_for_terms(
            row["content"],
            terms,
            limit=max_excerpt_chars,
        )
        matches.append((score, source, heading, excerpt))
    matches.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [
        (source, heading, excerpt)
        for _, source, heading, excerpt in matches[:max_results]
    ]


def _load_live_knowledge_sources(
    *,
    visibility: str,
    max_sources: int,
) -> Dict[str, str]:
    clause, values = _visibility_filter(visibility)
    try:
        with _connect_live_knowledge() as connection:
            if not _live_tables_available(connection):
                return {}
            rows = connection.execute(
                f"""
                SELECT source_channel_id, source_type, title, content
                FROM knowledge_entries
                WHERE {clause}
                ORDER BY indexed_at DESC, id DESC
                LIMIT ?
                """,
                (*values, max_sources),
            ).fetchall()
    except sqlite3.Error:
        return {}
    sources = {}
    for row in rows:
        label = f"Live Discord #{row['source_channel_id']} ({row['source_type']})"
        heading = row["title"] or "Discord Knowledge"
        sources[f"{label} — {heading}"] = row["content"]
    return sources


def _rank_results(
    results: Iterable[Tuple[str, str, str]],
    query: str,
    max_results: int,
) -> List[Tuple[str, str, str]]:
    ranked = []
    for source, heading, excerpt in results:
        ranked.append((score_entry(query, heading, excerpt), source, heading, excerpt))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [
        (source, heading, excerpt)
        for score, source, heading, excerpt in ranked
        if score > 0
    ][:max_results]


def _query_terms(query: str) -> List[str]:
    words = re.findall(r"[a-z0-9']+", query.casefold())
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
    }
    return [word for word in words if len(word) > 2 and word not in stop_words]


def _sections(content: str):
    current_heading = "General"
    current_lines = []
    for line in content.splitlines():
        if line.startswith("#"):
            if current_lines:
                yield current_heading, "\n".join(current_lines).strip()
            current_heading = line.lstrip("#").strip() or "General"
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        yield current_heading, "\n".join(current_lines).strip()
