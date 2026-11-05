import random
import re
from collections import Counter, deque
from difflib import SequenceMatcher


_COMPARE_CHARS = 600  # how much of each reply the similarity check looks at

_RUNTIME_RECENT: dict[str, deque[str]] = {
    "scaramouche": deque(maxlen=80),
    "wanderer": deque(maxlen=80),
}


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


_APOSTROPHES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201b": "'", "\u02bc": "'"})


def _variants(bot_name: str):
    return _OPENING_VARIANTS.get((bot_name or "").lower(), [])


def _normalize(text: str) -> str:
    text = (text or "").translate(_APOSTROPHES).lower()
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
    """How often each signature opener ("tch", "how quaint", ...) starts a recent message."""
    counts: Counter[str] = Counter()
    for text in recent_messages:
        sample = (text or "").translate(_APOSTROPHES).strip()
        for seed, pattern, _ in _variants(bot_name):
            if pattern.match(sample):
                counts[seed] += 1
                break
    return counts


def _join_opening(replacement: str, rest: str) -> str:
    """Attach the remainder of a reply to its new opener, capitalising it as a new sentence."""
    if not rest:
        return replacement
    if replacement.endswith((".", "!", "?")):
        rest = rest[:1].upper() + rest[1:]
    return f"{replacement} {rest}".strip()


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

    if not stale_openings and not stale_phrases:
        return ""

    lines = [
        "ANTI_REPEAT:",
        "You have been falling into phrase habits. Keep the tone, but change the wording and sentence shape.",
    ]
    if stale_openings:
        lines.append("Avoid these recent openings: " + "; ".join(stale_openings))
    if stale_phrases:
        lines.append("Do not use these stale signature phrases right now: " + "; ".join(stale_phrases))
    lines.append("Do not recycle the same first clause, favorite interjection, or same mockery template.")
    return "\n".join(lines)


def diversify_reply(bot_name: str, text: str, recent_messages: list[str]) -> str:
    if not text:
        return text

    updated = text.strip()
    recent_openings = {_opening_key(item) for item in recent_messages if item}
    phrase_counts = _phrase_counts(bot_name, recent_messages)

    for seed, pattern, options in _variants(bot_name):
        match = pattern.match(updated.translate(_APOSTROPHES))
        if not match:
            continue

        is_stale = phrase_counts.get(seed, 0) >= 2 or _opening_key(updated) in recent_openings
        if not is_stale:
            return updated

        replacement = pick_fresh_option(bot_name, options, recent_messages)
        rest = updated[match.end():].lstrip(" ,.!?-")
        return _join_opening(replacement, rest)

    return updated


def detect_opening_phrase(bot_name: str, text: str) -> str:
    sample = (text or "").translate(_APOSTROPHES).strip()
    for seed, pattern, _ in _variants(bot_name):
        if pattern.match(sample):
            return seed
    return ""


def replace_opening_phrase(
    bot_name: str,
    text: str,
    recent_messages: list[str] | None = None,
    extra_options: list[str] | None = None,
) -> str:
    """Swap a signature opener for a fresh alternative. ``extra_options`` extends the built-in pool."""
    recent_messages = recent_messages or []
    updated = (text or "").strip()
    for _, pattern, options in _variants(bot_name):
        match = pattern.match(updated.translate(_APOSTROPHES))
        if not match:
            continue
        pool = list(dict.fromkeys([*options, *(extra_options or [])]))
        replacement = pick_fresh_option(bot_name, pool, recent_messages)
        rest = updated[match.end():].lstrip(" ,.!?-")
        return _join_opening(replacement, rest)
    return updated


def looks_repetitive(text: str, recent_messages: list[str]) -> bool:
    normalized = _normalize(text)
    if not normalized or len(normalized) < 8:
        return False

    opening = _opening_key(text)
    normalized = normalized[:_COMPARE_CHARS]  # repetition shows in how a reply starts; also bounds the cost below
    for recent in recent_messages[:18]:
        recent_normalized = _normalize(recent)[:_COMPARE_CHARS]
        if not recent_normalized:
            continue
        if normalized == recent_normalized:
            return True
        ratio = SequenceMatcher(None, normalized, recent_normalized).ratio()
        if ratio >= 0.91:
            return True
        # Same first words *and* mostly the same sentence. A shared opener alone ("I do not know ...")
        # is normal speech, not a rut.
        if opening and opening == _opening_key(recent) and ratio >= 0.65:
            return True
    return False


def fallback_reply(bot_name: str, recent_messages: list[str]) -> str:
    lines = _FALLBACK_LINES.get((bot_name or "").lower()) or _FALLBACK_LINES["scaramouche"]
    return pick_fresh_option(bot_name, lines, recent_messages)
