from __future__ import annotations

import random
import re
from collections import Counter, deque
from difflib import SequenceMatcher


_RUNTIME_RECENT: dict[str, deque[str]] = {
    "scaramouche": deque(maxlen=80),
    "wanderer": deque(maxlen=80),
}


def response_shape(text: str) -> dict[str, int | bool | str]:
    """Describe cheap structural traits without semantic/model calls."""
    cleaned = (text or "").strip()
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    words = re.findall(r"\b[\w']+\b", cleaned)
    first = sentences[0] if sentences else cleaned
    opening = "interjection" if re.match(r"^(tch|hmph|heh|well|honestly)\b", cleaned, re.I) else (
        "question" if first.endswith("?") else "statement"
    )
    return {
        "sentence_count": len(sentences) or (1 if cleaned else 0),
        "opening_type": opening,
        "first_sentence_words": len(re.findall(r"\b[\w']+\b", first)),
        "question_count": cleaned.count("?"),
        "length_band": "short" if len(words) <= 8 else "medium" if len(words) <= 30 else "long",
        "ends_question": cleaned.endswith("?"),
        "starts_insult": bool(re.match(r"^(idiot|fool|pathetic|predictable|weak|boring)\b", cleaned, re.I)),
        "has_admission": bool(re.search(r"\b(i care|i miss|i was wrong|i need|i wanted)\b", cleaned, re.I)),
    }


def shape_signature(text: str) -> str:
    shape = response_shape(text)
    return "|".join(str(shape[key]) for key in (
        "sentence_count", "opening_type", "first_sentence_words", "question_count",
        "length_band", "ends_question", "starts_insult", "has_admission",
    ))


def repeated_shape(text: str, recent_messages: list[str], threshold: int = 3) -> bool:
    if not text or len(recent_messages) < threshold:
        return False
    signature = shape_signature(text)
    return sum(shape_signature(old) == signature for old in recent_messages[:12] if old) >= threshold


_OPENING_VARIANTS: dict[str, list[tuple[str, re.Pattern[str], list[str]]]] = {
    "scaramouche": [
        (
            "how quaint",
            re.compile(r"^\s*how(?:\W+\w+){0,2}\W+quaint\b[,.! ]*", re.IGNORECASE),
            [
                "Predictable.",
                "What a tiny performance.",
                "So that was your big idea.",
                "You really thought that was clever.",
                "And here I was expecting effort.",
                "That is painfully on brand for you.",
            ],
        ),
        (
            "tch",
            re.compile(r"^\s*tch\b[,.! ]*", re.IGNORECASE),
            [
                "Pathetic.",
                "Unimpressive.",
                "You are trying my patience.",
                "That again.",
                "Go on, then.",
                "What now.",
            ],
        ),
        (
            "hmph",
            re.compile(r"^\s*hmph\b[,.! ]*", re.IGNORECASE),
            [
                "Spare me.",
                "How tiresome.",
                "Say what you actually mean.",
                "Continue.",
                "What a dreary opening.",
                "Well?",
            ],
        ),
        (
            "how unfortunate",
            re.compile(r"^\s*how(?:\W+\w+){0,2}\W+unfortunate\b[,.! ]*", re.IGNORECASE),
            [
                "That tracks.",
                "How drearily predictable.",
                "What an uninspired turn.",
                "Exactly as disappointing as expected.",
                "That could not be less surprising.",
            ],
        ),
        (
            "how irritating",
            re.compile(r"^\s*how(?:\W+\w+){0,2}\W+irritating\b[,.! ]*", re.IGNORECASE),
            [
                "You are wearing thin.",
                "This is already tedious.",
                "Do try not to bore me this quickly.",
                "You make annoyance look effortless.",
                "I had hoped for slightly better than this.",
            ],
        ),
    ],
    "wanderer": [
        (
            "how irritating",
            re.compile(r"^\s*how(?:\W+\w+){0,2}\W+irritating\b[,.! ]*", re.IGNORECASE),
            [
                "You are making this tedious.",
                "That is getting old already.",
                "You really are testing my patience.",
                "And here I thought this might stay interesting.",
                "There are easier ways to waste my time.",
                "You are dragging this downhill.",
            ],
        ),
        (
            "how childish",
            re.compile(r"^\s*how(?:\W+\w+){0,2}\W+childish\b[,.! ]*", re.IGNORECASE),
            [
                "That was embarrassingly juvenile.",
                "You really went with that.",
                "Try again with a little dignity.",
                "That is the level you settled on.",
                "You can do better than that. Probably.",
            ],
        ),
        (
            "tch",
            re.compile(r"^\s*tch\b[,.! ]*", re.IGNORECASE),
            [
                "Honestly.",
                "That is getting old.",
                "You are making this tedious.",
                "There are easier ways to annoy me.",
                "Enough already.",
            ],
        ),
        (
            "hmph",
            re.compile(r"^\s*hmph\b[,.! ]*", re.IGNORECASE),
            [
                "Honestly.",
                "Right.",
                "Fine.",
                "Well then.",
                "Go on.",
                "You have my attention. Briefly.",
            ],
        ),
        (
            "heh",
            re.compile(r"^\s*heh\b[,.! ]*", re.IGNORECASE),
            [
                "Well.",
                "I see.",
                "Interesting.",
                "So that is where we are.",
                "Mm. Alright.",
            ],
        ),
        (
            "i've got nothing",
            re.compile(r"^\s*i've got nothing\b[,.! ]*", re.IGNORECASE),
            [
                "Say something worth answering.",
                "Start talking. I will decide if it is worth it.",
                "Ask already.",
                "If you have a point, get to it.",
                "I am listening. Do not waste that.",
            ],
        ),
    ],
}


_FALLBACK_LINES: dict[str, list[str]] = {
    "scaramouche": [
        "Go on.",
        "Speak plainly.",
        "You have my attention. For now.",
        "What exactly are you trying to prove.",
        "Start over and do it better.",
        "Say something worth the interruption.",
    ],
    "wanderer": [
        "Go ahead.",
        "Say what you mean.",
        "I am listening. Briefly.",
        "Get to the point.",
        "Try that again without the recycled opener.",
        "Start where you actually mean to start.",
    ],
}


def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"<@!?\d+>", "", text)
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = re.sub(r"\([^)]+\)", " ", text)
    text = re.sub(r"[^a-z0-9'\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _opening_key(text: str, words: int = 4) -> str:
    normalized = _normalize(text)
    if not normalized:
        return ""
    return " ".join(normalized.split()[:words])


def _phrase_counts(bot_name: str, recent_messages: list[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for text in recent_messages:
        normalized = _normalize(text)
        for seed, pattern, _ in _OPENING_VARIANTS.get(bot_name, []):
            if seed and normalized.startswith(seed):
                counts[seed] += 1
    return counts


def merge_recent_messages(*message_lists: list[str], limit: int = 40) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for messages in message_lists:
        for text in messages:
            cleaned = (text or "").strip()
            if not cleaned:
                continue
            key = _normalize(cleaned)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(cleaned)
            if len(merged) >= limit:
                return merged
    return merged


def remember_output(bot_name: str, text: str):
    bot_key = bot_name.lower()
    cleaned = (text or "").strip()
    if bot_key in _RUNTIME_RECENT and cleaned:
        _RUNTIME_RECENT[bot_key].appendleft(cleaned)


def get_runtime_recent(bot_name: str, limit: int = 30) -> list[str]:
    return list(_RUNTIME_RECENT.get(bot_name.lower(), deque()))[:limit]


def pick_fresh_option(bot_name: str, options: list[str], recent_messages: list[str] | None = None) -> str:
    recent_messages = recent_messages or []
    stale_openings = {_opening_key(text) for text in recent_messages if text}
    fresh = [option for option in options if _opening_key(option) not in stale_openings]
    return random.choice(fresh or options)


def build_prompt_guard(bot_name: str, recent_messages: list[str]) -> str:
    if not recent_messages:
        return ""

    opening_counts = Counter(_opening_key(text) for text in recent_messages if _opening_key(text))
    stale_openings = [opening for opening, count in opening_counts.items() if count >= 2][:6]
    stale_phrases = [phrase for phrase, count in _phrase_counts(bot_name, recent_messages).items() if count >= 2][:6]

    shape_counts = Counter(shape_signature(text) for text in recent_messages[:20] if text)
    stale_shapes = [signature for signature, count in shape_counts.items() if count >= 3][:3]

    if not stale_openings and not stale_phrases and not stale_shapes:
        return ""

    lines = [
        "ANTI_REPEAT:",
        "You have been falling into phrase habits. Keep the tone, but change the wording and sentence shape.",
    ]
    if stale_openings:
        # Do not echo arbitrary openings: the global anti-repeat sample can
        # include bot replies from another user's private conversation.
        lines.append("Several recent first clauses repeated. Use a genuinely new opening.")
    if stale_phrases:
        lines.append("Do not use these stale signature phrases right now: " + "; ".join(stale_phrases))
    if stale_shapes:
        lines.append(
            "Recent replies reused the same structural shape. Change sentence count, opening type, "
            "length band, question placement, and whether the answer or mockery comes first."
        )
    lines.append("Do not recycle the same first clause, favorite interjection, or same mockery template.")
    return "\n".join(lines)


def diversify_reply(bot_name: str, text: str, recent_messages: list[str]) -> str:
    if not text:
        return text

    updated = text.strip()
    recent_openings = {_opening_key(item) for item in recent_messages if item}
    phrase_counts = _phrase_counts(bot_name, recent_messages)

    for _, pattern, options in _OPENING_VARIANTS.get(bot_name, []):
        match = pattern.match(updated)
        if not match:
            continue

        matched_text = _normalize(match.group(0))
        is_stale = phrase_counts.get(matched_text, 0) >= 2 or _opening_key(updated) in recent_openings
        if not is_stale:
            return updated

        replacement = pick_fresh_option(bot_name, options, recent_messages)
        rest = updated[match.end():].lstrip(" ,.!?-")
        return f"{replacement} {rest}".strip() if rest else replacement

    return updated


def detect_opening_phrase(bot_name: str, text: str) -> str:
    sample = (text or "").strip()
    for seed, pattern, _ in _OPENING_VARIANTS.get(bot_name, []):
        if pattern.match(sample):
            return seed
    return ""


def replace_opening_phrase(bot_name: str, text: str, recent_messages: list[str] | None = None) -> str:
    recent_messages = recent_messages or []
    updated = (text or "").strip()
    for _, pattern, options in _OPENING_VARIANTS.get(bot_name, []):
        match = pattern.match(updated)
        if not match:
            continue
        replacement = pick_fresh_option(bot_name, options, recent_messages)
        rest = updated[match.end():].lstrip(" ,.!?-")
        return f"{replacement} {rest}".strip() if rest else replacement
    return updated


def looks_repetitive(text: str, recent_messages: list[str], *, include_shape: bool = True) -> bool:
    normalized = _normalize(text)
    if not normalized or len(normalized) < 8:
        return False

    opening = _opening_key(text)
    for recent in recent_messages[:18]:
        recent_normalized = _normalize(recent)
        if not recent_normalized:
            continue
        if normalized == recent_normalized:
            return True
        if opening and opening == _opening_key(recent):
            return True
        if SequenceMatcher(None, normalized, recent_normalized).ratio() >= 0.91:
            return True
    return repeated_shape(text, recent_messages) if include_shape else False


def fallback_reply(bot_name: str, recent_messages: list[str]) -> str:
    return pick_fresh_option(bot_name, _FALLBACK_LINES[bot_name.lower()], recent_messages)
