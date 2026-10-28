from __future__ import annotations

import random
import re
from collections import Counter, deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from runtime_cache import BoundedTTLCache


_RUNTIME_RECENT: dict[str, deque[str]] = {
    "scaramouche": deque(maxlen=80),
    "wanderer": deque(maxlen=80),
}

_VOICE_DELIVERY_MARKER = re.compile(r"^\[voice spoken\]\s*", re.IGNORECASE)


def _reply_text(text: str) -> str:
    """Ignore the delivery label when comparing spoken and typed replies."""
    return _VOICE_DELIVERY_MARKER.sub("", (text or "").strip())


_PRAISE_WORDS = {
    "amazing", "astonishing", "brilliant", "clever", "excellent", "impressive",
    "incredible", "magnificent", "remarkable", "wonderful",
}
_FAILURE_WORDS = {
    "broke", "broken", "fail", "failed", "failure", "mess", "ruined", "wrong",
    "disaster", "pathetic", "embarrassing", "useless",
}
_INSULT_WORDS = {
    "idiot", "fool", "pathetic", "predictable", "weak", "boring", "tedious",
    "hopeless", "childish", "incompetent", "useless",
}


def rhetorical_pattern(text: str) -> str:
    """Classify a bounded rhetorical move without semantic/model inference."""
    cleaned = (text or "").strip()
    lowered = cleaned.lower()
    words = re.findall(r"\b[a-z']+\b", lowered)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", lowered) if part.strip()]
    first_words = set(re.findall(r"\b[a-z']+\b", sentences[0] if sentences else lowered))
    all_words = set(words)
    if len(words) <= 3 and all_words & {"pathetic", "predictable", "boring", "tedious", "no", "enough"}:
        return "one_word_dismissal"
    if re.search(r"\b(congratulations|well done|bravo)\b", lowered) and all_words & _FAILURE_WORDS:
        return "sarcastic_congratulation"
    if first_words & _PRAISE_WORDS and all_words & _FAILURE_WORDS:
        return "fake_praise"
    if re.match(r"^(tch|hmph|spare me|how tedious|how boring|pathetic|predictable|honestly)\b", lowered):
        return "dismissive_opening"
    if re.search(r"\b(i care|i noticed|i remembered|stay|be careful|rest|eat something)\b", lowered) and re.search(
        r"\b(not that|don't misunderstand|it means nothing|forget i said)\b", lowered
    ):
        return "denial_after_softness"
    if re.match(r"^(fine|if you must|since you clearly|apparently i have to)\b", lowered) and re.search(
        r"\b(help|explain|answer|show|tell)\b", lowered
    ):
        return "reluctant_help"
    if re.search(r"\b(rest|eat|sleep|be careful|take care|stay)\b", lowered) and re.search(
        r"\b(fine|just|don't make|for once|idiot|fool)\b", lowered
    ):
        return "reluctant_care"
    if re.search(r"\b(rank|harbinger|beneath me|sixth|authority|power)\b", lowered):
        return "rank_flex"
    if re.search(r"\b(creator|created me|made me|abandoned|discarded|gnosis)\b", lowered):
        return "creator_wound_callback"
    if re.search(r"\b(careful|you'll regret|you will regret|dare|destroy|ruin you|last warning)\b", lowered):
        return "dramatic_threat"
    if "?" in cleaned and re.search(r"\b(you really|did you honestly|are you seriously|you expect me)\b", lowered):
        return "rhetorical_disbelief"
    if "?" in cleaned and re.match(r"^(what|why|how|do you|did you|are you)\b", lowered):
        return "mock_question"
    if len(sentences) >= 2:
        first_insult = bool(first_words & _INSULT_WORDS)
        later_words = set(re.findall(r"\b[a-z']+\b", " ".join(sentences[1:])))
        later_insult = bool(later_words & _INSULT_WORDS)
        if first_insult and not later_insult:
            return "insult_then_answer"
        if not first_insult and later_insult:
            return "answer_then_insult"
    return "neutral"


@dataclass(frozen=True)
class ResponseSignature:
    opening_family: str
    rhetorical_pattern: str
    sentence_count: int
    answer_position: str
    question_placement: str
    mockery_position: str
    admission_softness: bool
    length_band: str


def response_signature(text: str) -> ResponseSignature:
    shape = response_shape(text)
    pattern = rhetorical_pattern(text)
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if part.strip()]
    first = sentences[0].lower() if sentences else ""
    last = sentences[-1].lower() if sentences else ""
    first_mockery = bool(set(re.findall(r"\b[a-z']+\b", first)) & _INSULT_WORDS)
    last_mockery = bool(set(re.findall(r"\b[a-z']+\b", last)) & _INSULT_WORDS)
    if first_mockery:
        mockery_position = "first"
    elif last_mockery:
        mockery_position = "last"
    else:
        mockery_position = "none"
    answer_position = "after_mockery" if (
        pattern == "insult_then_answer" or first_mockery and len(sentences) >= 2
    ) else (
        "before_mockery" if pattern == "answer_then_insult" or last_mockery and len(sentences) >= 2
        else "unclear"
    )
    question_placement = "none"
    if "?" in (text or ""):
        question_placement = "first" if first.endswith("?") else "last" if last.endswith("?") else "middle"
    return ResponseSignature(
        opening_family=str(shape["opening_type"]), rhetorical_pattern=pattern,
        sentence_count=int(shape["sentence_count"]), answer_position=answer_position,
        question_placement=question_placement, mockery_position=mockery_position,
        admission_softness=bool(shape["has_admission"]),
        length_band=str(shape["length_band"]),
    )


@dataclass(frozen=True)
class PatternScopeSamples:
    user: tuple[str, ...] = ()
    channel: tuple[str, ...] = ()
    global_character: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepetitionAnalysis:
    repetitive: bool
    detected_pattern: str = "neutral"
    pattern_frequency: int = 0
    rejected_for_text_similarity: bool = False
    rejected_for_pattern_repeat: bool = False
    rejected_for_shape: bool = False


class PatternHistory:
    """Bounded process-only signatures; global scope never stores reply text."""

    def __init__(self):
        self.users = BoundedTTLCache(ttl_seconds=7 * 86400, max_entries=2048)
        self.channels = BoundedTTLCache(ttl_seconds=2 * 86400, max_entries=1024)
        self.global_character: dict[str, deque[str]] = {
            "scaramouche": deque(maxlen=80), "wanderer": deque(maxlen=80),
        }

    @staticmethod
    def _append(cache, key, pattern, maximum):
        values = cache.get(key)
        if values is None:
            values = deque(maxlen=maximum)
        values.appendleft(pattern)
        cache[key] = values

    def remember(self, bot_name: str, text: str, *, user_id=None, channel_id=None):
        pattern = rhetorical_pattern(text)
        if pattern == "neutral":
            return
        bot_key = bot_name.lower()
        if user_id is not None:
            self._append(self.users, (bot_key, int(user_id)), pattern, 24)
        if channel_id is not None:
            self._append(self.channels, (bot_key, int(channel_id)), pattern, 32)
        self.global_character.setdefault(bot_key, deque(maxlen=80)).appendleft(pattern)

    def samples(self, bot_name: str, *, user_id=None, channel_id=None) -> PatternScopeSamples:
        bot_key = bot_name.lower()
        return PatternScopeSamples(
            tuple(self.users.get((bot_key, int(user_id)), ())) if user_id is not None else (),
            tuple(self.channels.get((bot_key, int(channel_id)), ())) if channel_id is not None else (),
            tuple(self.global_character.get(bot_key, ())),
        )

    def forget_user(self, bot_name: str, user_id: int) -> None:
        self.users.pop((bot_name.lower(), int(user_id)), None)


_PATTERN_HISTORY = PatternHistory()


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


def _structural_shape_signature(text: str) -> str:
    shape = response_shape(text)
    return "|".join(str(shape[key]) for key in (
        "sentence_count", "opening_type", "first_sentence_words", "question_count",
        "length_band", "ends_question", "starts_insult", "has_admission",
    ))


def shape_signature(text: str) -> str:
    structural = _structural_shape_signature(text)
    signature = response_signature(text)
    return "|".join((
        structural, signature.rhetorical_pattern, signature.answer_position,
        signature.question_placement, signature.mockery_position,
    ))


def repeated_shape(text: str, recent_messages: list[str], threshold: int = 3) -> bool:
    if not text or len(recent_messages) < threshold:
        return False
    signature = _structural_shape_signature(text)
    return sum(
        _structural_shape_signature(old) == signature
        for old in recent_messages[:12] if old
    ) >= threshold


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
            cleaned = _reply_text(text)
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


def suppress_repeated_name_opener(
    text: str, display_name: str, recent_messages: list[str],
) -> str:
    """Keep a rare direct address, but not the same greeting every turn or sentence."""
    name = (display_name or "").strip()
    if not text or not name or len(name) > 80:
        return text
    opener = re.compile(
        r"(?:^|(?<=[.!?]\s))\s*Ah\s*,?\s*"
        + re.escape(name)
        + r"(?=\W|$)\s*(?:[,:;.!?—-]+\s*)?(?P<next>[A-Za-z])?",
        re.IGNORECASE | re.MULTILINE,
    )
    seen = any(opener.match(_reply_text(old)) for old in recent_messages[:20])

    def replace(match: re.Match[str]) -> str:
        nonlocal seen
        if not seen:
            seen = True
            return match.group(0)
        return (match.group("next") or "").upper()

    return opener.sub(replace, text).strip()


def _patterns(messages: list[str]) -> tuple[str, ...]:
    return tuple(
        pattern for pattern in (rhetorical_pattern(text) for text in messages)
        if pattern != "neutral"
    )


def build_pattern_scopes(
    bot_name: str,
    *,
    user_messages: list[str] | None = None,
    channel_messages: list[str] | None = None,
    global_messages: list[str] | None = None,
    user_id: int | None = None,
    channel_id: int | None = None,
) -> PatternScopeSamples:
    """Derive signatures from bounded persisted replies, falling back to runtime."""
    runtime = _PATTERN_HISTORY.samples(
        bot_name, user_id=user_id, channel_id=channel_id,
    )
    user = _patterns(user_messages or [])
    channel = _patterns(channel_messages or [])
    global_patterns = _patterns(global_messages or [])
    return PatternScopeSamples(
        user or runtime.user,
        channel or runtime.channel,
        global_patterns or runtime.global_character,
    )


def remember_output(
    bot_name: str, text: str, *, user_id: int | None = None,
    channel_id: int | None = None,
):
    bot_key = bot_name.lower()
    cleaned = (text or "").strip()
    if bot_key in _RUNTIME_RECENT and cleaned:
        # User-scoped replies persist in SQLite and runtime signature scopes;
        # never retain their raw private text in the global runtime deque.
        if user_id is None and channel_id is None:
            _RUNTIME_RECENT[bot_key].appendleft(cleaned)
        _PATTERN_HISTORY.remember(
            bot_key, cleaned, user_id=user_id, channel_id=channel_id,
        )


def forget_user_patterns(bot_name: str, user_id: int) -> None:
    _PATTERN_HISTORY.forget_user(bot_name, user_id)


def get_runtime_recent(bot_name: str, limit: int = 30) -> list[str]:
    return list(_RUNTIME_RECENT.get(bot_name.lower(), deque()))[:limit]


def pick_fresh_option(bot_name: str, options: list[str], recent_messages: list[str] | None = None) -> str:
    recent_messages = recent_messages or []
    stale_openings = {_opening_key(text) for text in recent_messages if text}
    fresh = [option for option in options if _opening_key(option) not in stale_openings]
    return random.choice(fresh or options)


def _pattern_counts(scopes: PatternScopeSamples) -> dict[str, tuple[int, int, int]]:
    names = set(scopes.user) | set(scopes.channel) | set(scopes.global_character)
    return {
        name: (
            scopes.user.count(name), scopes.channel.count(name),
            scopes.global_character.count(name),
        )
        for name in names if name != "neutral"
    }


def stale_patterns(scopes: PatternScopeSamples) -> list[tuple[str, int]]:
    stale: list[tuple[str, int]] = []
    for name, (user_count, channel_count, global_count) in _pattern_counts(scopes).items():
        if user_count >= 3 or channel_count >= 4 or global_count >= 8:
            stale.append((name, max(user_count, channel_count, global_count)))
    return sorted(stale, key=lambda item: item[1], reverse=True)


def build_prompt_guard(
    bot_name: str,
    recent_messages: list[str],
    *,
    pattern_scopes: PatternScopeSamples | None = None,
    factual_mode: bool = False,
    serious_mode: bool = False,
) -> str:
    if not recent_messages:
        recent_messages = []

    opening_counts = Counter(_opening_key(text) for text in recent_messages if _opening_key(text))
    stale_openings = [opening for opening, count in opening_counts.items() if count >= 2][:6]
    stale_phrases = [phrase for phrase, count in _phrase_counts(bot_name, recent_messages).items() if count >= 2][:6]

    shape_counts = Counter(
        _structural_shape_signature(text) for text in recent_messages[:20] if text
    )
    stale_shapes = [signature for signature, count in shape_counts.items() if count >= 3][:3]
    scopes = pattern_scopes or PatternScopeSamples(user=_patterns(recent_messages[:18]))
    patterns = stale_patterns(scopes)
    if factual_mode or serious_mode:
        stale_openings = []
        stale_shapes = []
        safe_focus = {
            "fake_praise", "dismissive_opening", "one_word_dismissal",
            "sarcastic_congratulation",
        }
        patterns = [item for item in patterns if item[0] in safe_focus]

    if not stale_openings and not stale_phrases and not stale_shapes and not patterns:
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
    for pattern, _ in patterns[:2]:
        readable = pattern.replace("_", " ")
        lines.append(
            f"You have recently overused {readable}. Use a different conversational strategy."
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


def analyze_repetition(
    text: str,
    recent_messages: list[str],
    *,
    include_shape: bool = True,
    pattern_scopes: PatternScopeSamples | None = None,
    factual_mode: bool = False,
    serious_mode: bool = False,
) -> RepetitionAnalysis:
    normalized = _normalize(text)
    if not normalized or len(normalized) < 8:
        return RepetitionAnalysis(False, rhetorical_pattern(text))

    opening = _opening_key(text)
    for recent in recent_messages[:18]:
        recent_normalized = _normalize(recent)
        if not recent_normalized:
            continue
        if normalized == recent_normalized:
            return RepetitionAnalysis(
                True, rhetorical_pattern(text), rejected_for_text_similarity=True,
            )
        if opening and opening == _opening_key(recent):
            return RepetitionAnalysis(
                True, rhetorical_pattern(text), rejected_for_text_similarity=True,
            )
        if SequenceMatcher(None, normalized, recent_normalized).ratio() >= 0.91:
            return RepetitionAnalysis(
                True, rhetorical_pattern(text), rejected_for_text_similarity=True,
            )

    pattern = rhetorical_pattern(text)
    scopes = pattern_scopes or PatternScopeSamples(user=_patterns(recent_messages[:18]))
    counts = _pattern_counts(scopes).get(pattern, (0, 0, 0))
    frequency = max(counts)
    style_only = factual_mode or serious_mode
    allowed_patterns = {
        "fake_praise", "dismissive_opening", "one_word_dismissal",
        "sarcastic_congratulation",
    } if style_only else set(_pattern_counts(scopes)) | {pattern}
    pattern_stale = (
        pattern != "neutral" and pattern in allowed_patterns
        and (counts[0] >= 3 or counts[1] >= 4 or counts[2] >= 8)
    )
    if pattern_stale:
        return RepetitionAnalysis(
            True, pattern, frequency, rejected_for_pattern_repeat=True,
        )
    shape_stale = include_shape and not (factual_mode or serious_mode) and repeated_shape(
        text, recent_messages,
    )
    return RepetitionAnalysis(
        shape_stale, pattern, frequency, rejected_for_shape=shape_stale,
    )


def looks_repetitive(
    text: str, recent_messages: list[str], *, include_shape: bool = True,
    pattern_scopes: PatternScopeSamples | None = None,
    factual_mode: bool = False, serious_mode: bool = False,
) -> bool:
    return analyze_repetition(
        text, recent_messages, include_shape=include_shape,
        pattern_scopes=pattern_scopes, factual_mode=factual_mode,
        serious_mode=serious_mode,
    ).repetitive


def fallback_reply(bot_name: str, recent_messages: list[str]) -> str:
    return pick_fresh_option(bot_name, _FALLBACK_LINES[bot_name.lower()], recent_messages)
