"""Evidence-checked, deterministic scoring for the public community rubric."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

from utils.message_context import parse_timestamp


BASELINE = 70
CATEGORIES = (
    ("connection", "Connection and conversation", 10),
    ("inclusion", "Welcoming and inclusion", 7),
    ("support", "Support and helpfulness", 6),
    ("initiative", "Shared experiences and initiative", 4),
    ("conduct", "Constructive conduct", 3),
)
RUBRIC_INSTRUCTION = """
Evaluate the contributionRubric, not an overall numerical score. The bot
calculates 70 + category bonuses - supported conduct deductions itself.
Categories (maximum bonuses): connection 10 (reciprocal conversation, shared
interests, rapport and enjoyable humor); inclusion 7 (welcomes, orientation,
making room for others); support 6 (helpful answers, encouragement, thoughtful
check-ins); initiative 4 (invitations, shared activities, creative contributions,
helping events happen, without requiring a staff role); conduct 3 (respectful
differences, boundaries, patience, de-escalation and sustained accountability).
For EACH category return rating 0-4, a concise public explanation, and exact
evidenceMessageIds belonging to the TARGET member: 0 = not observed (neutral),
1 = one clear example, 2 = multiple independent exchanges, 3 = a recurring
pattern on different dates, 4 = a sustained strength across several weeks.
Use at most 8 representative evidence IDs per category, spread across dates.
Many messages in one exchange are one example, not many contributions.
No bonus for message volume, XP, reactions, popularity, money or staff rank.
Do not deduct for inactivity, concise messages, GIFs, personal sharing, NSFW
participation, missing VC/attachment content, asking for support, declining an
invitation, setting boundaries, or civil disagreement with staff.
Mutual teasing, mature language and consensual flirting are not harmful by
themselves. Assess targeting, reciprocity, persistence, expressed boundaries
and responses to objections. Do not infer consent from silence. Never infer
motives from command frequency or describe rank checks as NSFW access farming.
conductIncidents must contain only directly supported, unaddressed conduct:
isolated_disrespect = a clear isolated disrespectful exchange (-3);
repeated_antagonism = a recurring pattern across separate exchanges (-10,
score ceiling 69); persistent_harm = repeated harassment or ignored explicit
boundaries (-25, ceiling 59, but withheld pending staff review); serious_review
= a serious incident that requires staff investigation (withhold score).
Combine evidence for the same pattern into one incident; never count both the
pattern and its constituent messages. Corrected minor misunderstandings have
no deduction. An apology alone does not establish changed behavior, and does
not earn repeated repair bonuses. Use public, behavioral explanations without
other members' names, staff notes, moderation history or private information.
Supply exact TARGET evidenceMessageIds for every incident. Nearby OTHER
messages are context only, never contributions, quotations or evidence IDs.
Return empty incidents when no supported concern exists. Never manufacture
growth advice to fill a field. Missing evidence limits confidence, not conduct.
""".strip()

_EVIDENCE_SCHEMA = {"type": "array", "items": {"type": "string"}}
RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "object",
            "properties": {
                key: {
                    "type": "object",
                    "properties": {
                        "rating": {"type": "integer", "minimum": 0, "maximum": 4},
                        "explanation": {"type": "string"},
                        "evidenceMessageIds": _EVIDENCE_SCHEMA,
                    },
                    "required": ["rating", "explanation", "evidenceMessageIds"],
                }
                for key, _, _ in CATEGORIES
            },
            "required": [key for key, _, _ in CATEGORIES],
        },
        "conductIncidents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["isolated_disrespect", "repeated_antagonism",
                                 "persistent_harm", "serious_review"],
                    },
                    "explanation": {"type": "string"},
                    "evidenceMessageIds": _EVIDENCE_SCHEMA,
                },
                "required": ["type", "explanation", "evidenceMessageIds"],
            },
        },
    },
    "required": ["categories", "conductIncidents"],
}


@dataclass(frozen=True)
class CategoryResult:
    key: str
    name: str
    maximum: int
    rating: int
    bonus: float
    explanation: str


@dataclass(frozen=True)
class Scorecard:
    score: Optional[int]
    status: str
    categories: tuple[CategoryResult, ...]
    deductions: tuple[tuple[int, str], ...]
    bonus: float
    penalty: int
    ceiling: Optional[int]
    confidence: str
    explanation: str
    exchanges: int


def scorecard_label(scorecard: Scorecard) -> str:
    if scorecard.score is None:
        return "Staff review required" if scorecard.status == "review_required" else "Insufficient evidence"
    score = scorecard.score
    return ("Exceptional sustained contribution" if score >= 90 else
            "Clearly positive contributor" if score >= 80 else
            "Healthy community participation" if score >= 70 else
            "Repeated detracting behavior" if score >= 60 else
            "Substantial harmful behavior")


def _sessions(rows: list[dict]) -> dict[str, tuple]:
    """Conservatively group a member's messages into channel sessions (30m gap)."""
    sessions = {}
    previous = {}
    for row in sorted(rows, key=lambda row: str(row.get("timestamp", ""))):
        stamp = parse_timestamp(row.get("timestamp"))
        message_id = str(row.get("message_id") or "")
        if not stamp or not message_id:
            continue
        channel = str(row.get("channel_id") or "")
        last, session = previous.get(channel, (None, None))
        if last is None or (stamp - last).total_seconds() > 1800:
            session = (channel, message_id)
        previous[channel] = (stamp, session)
        sessions[message_id] = session
    return sessions


def _evidence(ids: object, by_id: dict) -> list[dict]:
    if not isinstance(ids, list):
        return []
    return [by_id[mid] for mid in dict.fromkeys(str(v) for v in ids) if mid in by_id]


def _supported_rating(rows: list[dict], sessions: dict) -> int:
    stamps = sorted(parse_timestamp(row["timestamp"]) for row in rows)
    if not stamps:
        return 0
    dates = {stamp.date() for stamp in stamps}
    if len(dates) >= 4 and (stamps[-1] - stamps[0]).days >= 21:
        return 4
    if len(dates) >= 3:
        return 3
    if len({sessions.get(str(row["message_id"])) for row in rows}) >= 2:
        return 2
    return 1


def calculate_scorecard(data: dict, rows: list, *, total_matching: int,
                        failed_chunks: int = 0,
                        context_available_ids: Optional[set[str]] = None) -> Scorecard:
    """Never accept an AI-authored total or award credit to unverified evidence."""
    rows = [dict(row) for row in rows]
    by_id = {
        str(row["message_id"]): row for row in rows
        if row.get("message_id") and parse_timestamp(row.get("timestamp"))
        and not row.get("is_bot") and not row.get("is_webhook")
        and not row.get("is_deleted")
        and re.sub(r"https?://\S+", "", str(row.get("content") or "")).strip()
    }
    sessions = _sessions(list(by_id.values()))
    exchanges = len(set(sessions.values()))
    rubric = data.get("contributionRubric")
    ratings = rubric.get("categories") if isinstance(rubric, dict) else None
    empty = lambda reason: Scorecard(None, "insufficient", (), (), 0, 0, None,
                                     "Low", reason, exchanges)
    if not isinstance(ratings, dict) or not isinstance(rubric.get("conductIncidents"), list):
        return empty("The model did not return a complete, usable rubric.")
    categories = []
    positive_evidence = {}
    for key, name, maximum in CATEGORIES:
        category = ratings.get(key)
        if not isinstance(category, dict) or type(category.get("rating")) is not int:
            return empty("The model did not return all five valid category ratings.")
        if not 0 <= category["rating"] <= 4 or not isinstance(category.get("explanation"), str):
            return empty("The model returned an invalid category rating.")
        evidence = _evidence(category.get("evidenceMessageIds"), by_id)
        rating = min(category["rating"], _supported_rating(evidence, sessions))
        if rating:
            positive_evidence.update({str(row["message_id"]): row for row in evidence})
        explanation = (category["explanation"].strip() or "Supported by archived target-message evidence.") if rating else "Not observed; no deduction."
        if rating and rating < category["rating"]:
            explanation = f"Available source evidence supports {rating}/4; a stronger pattern is unverified."
        categories.append(CategoryResult(key, name, maximum, rating,
                                         maximum * rating / 4, explanation))
    bonus = sum(category.bonus for category in categories)
    deductions = []
    used_sessions = set()
    ceiling = None
    review = False
    # Review severe patterns first so overlapping lesser incidents cannot mask them.
    severity = {"serious_review": 3, "persistent_harm": 2,
                "repeated_antagonism": 1, "isolated_disrespect": 0}
    incidents = sorted((v for v in rubric["conductIncidents"] if isinstance(v, dict)),
                       key=lambda v: severity.get(str(v.get("type")), -1), reverse=True)
    for incident in incidents:
        evidence = _evidence(incident.get("evidenceMessageIds"), by_id)
        episodes = {sessions[str(row["message_id"])] for row in evidence}
        kind = incident.get("type")
        if not episodes or not isinstance(kind, str) or kind not in severity:
            continue
        if kind in ("repeated_antagonism", "persistent_harm") and len(episodes) < 2:
            continue
        if kind in ("serious_review", "persistent_harm"):
            review = True
        if context_available_ids is not None and any(
            str(row["message_id"]) not in context_available_ids for row in evidence
        ):
            # Unseen surrounding exchanges must not generate public deductions.
            review = True
            continue
        if episodes & used_sessions:
            continue
        used_sessions.update(episodes)
        penalty = {"isolated_disrespect": 3, "repeated_antagonism": 10,
                   "persistent_harm": 25, "serious_review": 0}[kind]
        if kind == "repeated_antagonism":
            ceiling = min(ceiling or 100, 69)
        elif kind == "persistent_harm":
            ceiling = min(ceiling or 100, 59)
        deductions.append((penalty, str(incident.get("explanation") or "Supported conduct concern.")))
    penalty = min(100, sum(item[0] for item in deductions))
    if review:
        return Scorecard(None, "review_required", tuple(categories), (), bonus,
                         0, None, "Low", "A numerical score is withheld pending staff review.", exchanges)
    if exchanges < 5:
        return Scorecard(None, "insufficient", tuple(categories), tuple(deductions),
                         bonus, penalty, ceiling, "Low",
                         "Fewer than five interpretable conversation sessions were available.", exchanges)
    # A short snapshot cannot establish exceptional *sustained* contribution.
    if _supported_rating(list(positive_evidence.values()), sessions) < 4:
        ceiling = min(ceiling or 100, 89)
    score = min(ceiling or 100, max(0, math.floor(BASELINE + bonus - penalty + 0.5)))
    stamps = sorted(parse_timestamp(row["timestamp"]) for row in by_id.values())
    span = (stamps[-1] - stamps[0]).days if stamps else 0
    confidence = "Low" if failed_chunks or span < 7 else (
        "High" if span >= 21 and exchanges >= 10 and len(rows) >= total_matching else "Medium")
    explanation = ("Ratings are limited to verified target-message evidence. "
                   "Missing activity lowers confidence, not conduct.")
    if ceiling == 89:
        explanation += " Scores of 90+ require positive evidence spanning at least three weeks."
    return Scorecard(score, "scored", tuple(categories), tuple(deductions), bonus,
                     penalty, ceiling, confidence, explanation, exchanges)


def verified_public_quotes(quotes: object, rows: list) -> list[dict]:
    """Rebuild citation metadata from source rows; reject invented/other-user quotes."""
    by_id = {str(dict(row).get("message_id")): dict(row) for row in rows}
    result = []
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, dict):
            continue
        row = by_id.get(str(quote.get("messageId")))
        text = quote.get("quote")
        if not row or not isinstance(text, str) or not text or len(text) > 280:
            continue
        if text not in str(row.get("content") or ""):
            continue
        result.append({"quote": text, "timestamp": row["timestamp"],
                       "channelName": row.get("channel_name") or row["channel_id"],
                       "jumpUrl": row.get("jump_url")})
        if len(result) == 5:
            break
    return result
