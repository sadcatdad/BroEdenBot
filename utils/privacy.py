"""Shared redaction helpers for private stored or generated context."""

from __future__ import annotations

import re


SENSITIVE_TEXT_PATTERNS = (
    (
        re.compile(
            r'''(?im)(?<!\w)(["']?(?:[A-Z][A-Z0-9_-]*[_-])?'''
            r'''(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|PRIVATE[_-]?KEY)'''
            r'''(?:[_-][A-Z0-9]+)*["']?)\s*[:=]\s*'''
            r'''("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)'''
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(authorization\s*:\s*bearer)\s+\S+"),
        r"\1 [REDACTED]",
    ),
    (
        re.compile(
            r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}"
            r"\.[A-Za-z0-9_-]{20,}\b"
        ),
        "[REDACTED TOKEN]",
    ),
    (
        re.compile(
            r"(?i)\b(?:sk|ghp|github_pat|xox[baprs])[-_]"
            r"[A-Za-z0-9_-]{12,}\b"
        ),
        "[REDACTED TOKEN]",
    ),
)


def redact_sensitive_text(value: object) -> str:
    """Redact obvious credentials without logging or retaining their values."""
    text = str(value or "")
    for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text
