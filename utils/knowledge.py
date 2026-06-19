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


def compact_knowledge_context(max_chars: int = 9_000) -> str:
    """Return compact local context suitable for a moderation prompt."""
    sections = [COMMUNITY_CONTEXT]
    for label, content in load_knowledge().items():
        if content:
            sections.append(f"## {label}\n{content}")
    context = "\n\n".join(sections)
    if len(context) <= max_chars:
        return context
    return context[: max_chars - 1].rstrip() + "…"


def search_knowledge(
    query: str,
    max_results: int = 5,
    max_excerpt_chars: int = 700,
) -> List[Tuple[str, str, str]]:
    """Rank Markdown sections with simple keyword matching."""
    terms = _query_terms(query)
    if not terms:
        return []

    matches = []
    for source, content in load_knowledge().items():
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
