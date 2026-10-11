"""Deterministic perception and internal-state updates."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class PerceivedEvent:
    event_type: str
    importance: int
    summary: str
    mood_deltas: dict[str, float]
    should_reflect: bool = False


_RUDE = re.compile(r"\b(hate|shut up|useless|stupid|idiot|annoying|worthless)\b", re.I)
_CARE = re.compile(r"\b(are you okay|take care|missed you|miss you|love you|proud of you|thank you)\b", re.I)
_RETURN = re.compile(r"\b(i'?m back|returned|been a while|long time no see)\b", re.I)
_TECH = re.compile(r"\b(ai|bot|python|code|database|sqlite|server|api|model|groq)\b", re.I)
_CONFLICT = re.compile(r"\b(you hurt|that hurt|angry at you|upset with you|betrayed|lied to me)\b", re.I)
_REPAIR = re.compile(r"\b(i'?m sorry|forgive me|make it right|can we fix|truce)\b", re.I)


def perceive_message(text: str, *, relationship_changed: bool = False,
                     returned_after_absence: bool = False, from_partner_bot: bool = False) -> PerceivedEvent:
    content = (text or "").strip()
    if relationship_changed:
        return PerceivedEvent("relationship_change", 9, "A relationship stage changed.",
                              {"attachment": 1.0, "defensiveness": .5}, True)
    if from_partner_bot:
        return PerceivedEvent("bot_interaction", 7, "Wanderer directly interacted with him.",
                              {"irritation": .8, "curiosity": .4, "defensiveness": .6}, True)
    if returned_after_absence or _RETURN.search(content):
        return PerceivedEvent("user_return", 8, "A familiar user returned after an absence.",
                              {"attachment": .8, "curiosity": .6, "defensiveness": .5}, True)
    if _CONFLICT.search(content):
        return PerceivedEvent("conflict", 9, "A user explicitly named hurt or conflict.",
                              {"irritation": 1.2, "defensiveness": 1.4, "concern": .5}, True)
    if _REPAIR.search(content):
        return PerceivedEvent("reconciliation", 8, "A user attempted to repair a conflict.",
                              {"irritation": -.8, "defensiveness": -.6, "concern": .8}, True)
    if _CARE.search(content):
        return PerceivedEvent("attachment_signal", 7, "A user showed direct care or attachment.",
                              {"attachment": .7, "concern": .4, "defensiveness": .3}, True)
    if _RUDE.search(content):
        return PerceivedEvent("hostility", 6, "A user was openly hostile.",
                              {"irritation": 1.0, "defensiveness": .7, "amusement": .2}, False)
    if _TECH.search(content):
        return PerceivedEvent("implementation_question", 4, "The current software implementation became relevant.",
                              {"curiosity": .4}, False)
    if "?" in content:
        return PerceivedEvent("question", 2, "A user asked a question.", {"curiosity": .15}, False)
    return PerceivedEvent("conversation", 1, "An ordinary conversation turn occurred.",
                          {"boredom": -.1, "curiosity": .05}, False)


def willingness_context(text: str, *, repeated_count: int = 0, permission_allowed: bool = True,
                        conflict_open: bool = False, trust: int = 0, irritation: float = 0) -> dict[str, str | bool]:
    """Model willingness without changing deterministic authorization."""
    if not permission_allowed:
        return {"permission_allowed": False, "level": "forbidden", "reason": "application policy denies this action"}
    if repeated_count >= 4:
        return {"permission_allowed": True, "level": "resistant", "reason": "the same request has been repeated several times"}
    if conflict_open and trust < 40:
        return {"permission_allowed": True, "level": "reluctant", "reason": "unresolved conflict makes cooperation costly"}
    if irritation >= 7:
        return {"permission_allowed": True, "level": "resistant", "reason": "current irritation is high"}
    if re.search(r"\b(please|help|need)\b", text or "", re.I) and trust >= 60:
        return {"permission_allowed": True, "level": "willing", "reason": "earned trust outweighs reluctance"}
    return {"permission_allowed": True, "level": "neutral", "reason": "no strong reason to resist"}


def willingness_prompt(value: dict[str, str | bool]) -> str:
    return (
        f"WILLINGNESS:{value['level']}|reason={value['reason']}|"
        f"permission={'allowed' if value['permission_allowed'] else 'denied'}. "
        "Willingness changes tone only; it never changes permissions or factual capability."
    )
