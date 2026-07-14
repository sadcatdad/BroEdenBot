"""Safe, readable presentation of Discord display names."""

from __future__ import annotations

import unicodedata


_BIDI_CONTROL_CLASSES = {
    "LRE",
    "RLE",
    "LRO",
    "RLO",
    "PDF",
    "LRI",
    "RLI",
    "FSI",
    "PDI",
}


def normalize_display_name(value: object, fallback: str = "Unknown") -> str:
    """Normalize decorative Unicode names without changing stored identity data."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    safe_characters = []
    for character in text:
        if unicodedata.bidirectional(character) in _BIDI_CONTROL_CLASSES:
            continue
        if unicodedata.category(character) == "Cc":
            if character.isspace():
                safe_characters.append(" ")
            continue
        safe_characters.append(character)
    text = "".join(safe_characters)
    text = " ".join(text.split())
    return text or fallback


def normalize_for_font(value: object, font, fallback: str = "Unknown") -> str:
    """Normalize a name and drop characters rendered as the font's .notdef box."""
    text = normalize_display_name(value, fallback="")
    missing_glyph = bytes(font.getmask("\u0378"))
    supported = "".join(
        character
        for character in text
        if character.isspace() or bytes(font.getmask(character)) != missing_glyph
    )
    supported = " ".join(supported.split())
    return supported or fallback
