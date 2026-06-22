import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "knowledge"
KNOWLEDGE_FILES = {
    "Bro Eden Rules": KNOWLEDGE_DIR / "rules.md",
    "Bro Eden Survival Guide": KNOWLEDGE_DIR / "survival_guide.md",
}
STAFF_KNOWLEDGE_DIR = PROJECT_ROOT / "data" / "staff_knowledge"
STAFF_KNOWLEDGE_FILES = {
    "Ranger's Handbook (Staff Only)": (
        STAFF_KNOWLEDGE_DIR / "rangers_handbook.md"
    ),
}

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


def compact_knowledge_context(max_chars: int = 18_000) -> str:
    """Return public and staff-only context for private moderation prompts."""
    sections = [COMMUNITY_CONTEXT]
    for label, content in load_staff_knowledge().items():
        if content:
            sections.append(f"## {label}\n{content}")
    context = "\n\n".join(sections)
    if len(context) <= max_chars:
        return context
    return context[: max_chars - 1].rstrip() + "…"


def build_public_ask_context(query: str, max_chars: int = 12_000) -> str:
    """Build member-safe context from only the public rules and survival guide."""
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
    return _search_sources(
        load_staff_knowledge(),
        query,
        max_results,
        max_excerpt_chars,
    )


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
    return _search_sources(
        load_server_knowledge(),
        query,
        max_results,
        max_excerpt_chars,
    )


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
