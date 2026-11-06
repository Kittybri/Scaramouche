"""Pure-stdlib text helpers shared by the bot.

Nothing in here imports Discord, the network or the database, so the offline test-suite can
exercise the exact code the bot runs.

Contents
  * keyword matching with real word boundaries (``keyword_regex`` / ``has_keyword``)
  * ``strip_narration`` - removes roleplay narration without damaging normal text
  * ``speech_text`` - text cleaned up for text-to-speech
  * mention handling (``resolve_name_mentions`` / ``sanitize_mentions``)
  * ``chunk_text`` for Discord's 2000 character limit
  * small parsers: verdicts, nicknames, search queries
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache

__all__ = [
    "normalize_text",
    "keyword_regex",
    "has_keyword",
    "strip_narration",
    "speech_text",
    "resolve_name_mentions",
    "sanitize_mentions",
    "chunk_text",
    "parse_verdict",
    "clean_nickname",
    "needs_search",
    "extract_search_query",
    "prompt_safe",
    "clean_display_name",
    "split_sources",
]


# ── Keyword matching ─────────────────────────────────────────────────────────
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "‛": "'", "ʼ": "'", "`": "'"})

# Short words get strict boundaries, 4-letter words accept common inflections, longer words may
# continue ("annoy" matches "annoying"). This is what stops "ei" matching "ceiling" or
# "date me" matching "update me".
_SUFFIX = r"(?:s|es|d|ed|ing|er|ers|ly|y)?"


def normalize_text(text: str | None) -> str:
    """Lower-case and unify apostrophes so ``I’ve`` and ``i've`` compare equal."""
    return (text or "").translate(_APOSTROPHES).lower()


def _token_regex(token: str) -> str:
    token = normalize_text(token).strip()
    words = token.split()
    if not words:
        return ""
    body = r"\s+".join(re.escape(word) for word in words)
    last = re.sub(r"[^a-z0-9]", "", words[-1])
    left = r"(?<![a-z0-9])" if token[0].isalnum() else ""
    if not token[-1].isalnum():
        tail = ""
    elif len(last) <= 3:
        tail = r"(?![a-z0-9])"
    elif len(last) == 4:
        tail = _SUFFIX + r"(?![a-z0-9])"
    else:
        tail = ""
    return f"{left}{body}{tail}"


@lru_cache(maxsize=1024)
def _compiled(tokens: tuple[str, ...]) -> re.Pattern[str]:
    parts = [part for part in (_token_regex(token) for token in tokens) if part]
    if not parts:
        return re.compile(r"(?!x)x")
    parts.sort(key=len, reverse=True)
    return re.compile("|".join(parts), re.IGNORECASE)


def keyword_regex(words: Iterable[str]) -> re.Pattern[str]:
    """Compile ``words`` into one boundary-aware pattern. Search it against ``normalize_text`` output."""
    return _compiled(tuple(words))


def has_keyword(text: str | None, words: Iterable[str]) -> bool:
    return bool(_compiled(tuple(words)).search(normalize_text(text)))


# ── Narration stripping ──────────────────────────────────────────────────────
def _verb_forms(base: str, *, e_drop: bool = False, double: bool = False, es: bool = False) -> str:
    stem = base[:-1] if e_drop else base
    if double:
        stem_ing = base + base[-1]
        forms = [base, base + "s", stem_ing + "ed", stem_ing + "ing"]
    else:
        forms = [base, base + ("es" if es else "s"), stem + "ed", stem + "ing"]
    return "|".join(re.escape(form) for form in forms)


_STAGE_VERBS = "|".join(
    [
        _verb_forms("smirk"),
        _verb_forms("sigh"),
        _verb_forms("scoff"),
        _verb_forms("glare", e_drop=True),
        _verb_forms("snort"),
        _verb_forms("chuckle", e_drop=True),
        _verb_forms("laugh"),
        _verb_forms("frown"),
        _verb_forms("mutter"),
        _verb_forms("whisper"),
        _verb_forms("sneer"),
        _verb_forms("scowl"),
        _verb_forms("grin", double=True),
        _verb_forms("smile", e_drop=True),
        _verb_forms("stare", e_drop=True),
        _verb_forms("glance", e_drop=True),
        _verb_forms("flick"),
        _verb_forms("clench", es=True),
        _verb_forms("huff"),
        _verb_forms("exhale", e_drop=True),
        _verb_forms("inhale", e_drop=True),
        _verb_forms("wince", e_drop=True),
        _verb_forms("flinch", es=True),
        _verb_forms("bristle", e_drop=True),
        _verb_forms("shrug", double=True),
        _verb_forms("nod", double=True),
        _verb_forms("blink"),
        _verb_forms("squint"),
        _verb_forms("pause", e_drop=True),
        _verb_forms("snicker"),
        _verb_forms("growl"),
        _verb_forms("hiss", es=True),
        r"hmph",
        r"tch",
    ]
)
_POSS = r"(?:his\s+|her\s+|their\s+)?"
_STAGE_PHRASES = "|".join(
    [
        rf"rolls?\s+{_POSS}eyes",
        rf"rolled\s+{_POSS}eyes",
        rf"shakes?\s+{_POSS}head",
        rf"crosses?\s+{_POSS}arms",
        rf"folds?\s+{_POSS}arms",
        rf"tilts?\s+{_POSS}head",
        rf"narrows?\s+{_POSS}eyes",
        r"raises?\s+(?:an?\s+)?eyebrow",
        r"turns?\s+(?:away|around|back)",
        r"looks?\s+(?:away|down|up|aside)",
        r"steps?\s+(?:closer|back|forward|aside)",
        r"leans?\s+(?:in|closer|back|forward)",
        r"adjusts?\s+(?:his\s+|her\s+)?(?:hat|sleeve|gloves?)",
    ]
)
_SUBJECT = r"(?:(?:he|she|they|scaramouche|the\s+balladeer)\s+)?"
_STAGE_BODY = rf"{_SUBJECT}(?:(?:{_STAGE_VERBS})|(?:{_STAGE_PHRASES}))\b[^()\[\]\n]{{0,80}}"
_ADVERBS = r"(?:sarcastically|dryly|coldly|quietly|softly|bitterly|mockingly|flatly|icily|sharply|wryly)"

_META_TAG_RE = re.compile(
    r"\[\s*(?:sent\s+(?:a\s+|an\s+)?)?(?:voice\s+message|voice\s+note|voice|image|video|audio|attachment)\s*\]",
    re.IGNORECASE,
)
# single-asterisk spans on one line: *smirks*. Bold (**x**), bullets ("* x") and maths ("2 * 3") are never matched.
# A match is only *deleted* when it reads like a stage direction (see _is_stage_direction); emphasis such as
# "I do *not* care" is italic markdown and has to survive, because dropping the word flips the meaning.
_ASTERISK_ACTION_RE = re.compile(r"(?<![*\w])\*(?![\s*])([^*\n]{1,200}?)(?<!\s)\*(?![*\w])")
_STAGE_START_RE = re.compile(rf"(?:{_STAGE_BODY}|{_ADVERBS})", re.IGNORECASE)
_SUBJECT_LED_RE = re.compile(r"(?:he|she|they|scaramouche|the\s+balladeer)\s+\w", re.IGNORECASE)
_INTERJECTIONS = frozenset({"hmph", "tch", "tsk", "ugh"})
_SENTENCE_END_CHARS = ".!?…:;"
_CLOSING_CHARS = "\"”’)]"
_PAREN_ACTION_RE = re.compile(rf"\(\s*(?:{_STAGE_BODY}|{_ADVERBS})\s*\)", re.IGNORECASE)
_BRACKET_ACTION_RE = re.compile(rf"\[\s*(?:{_STAGE_BODY}|{_ADVERBS})\s*\]", re.IGNORECASE)
_SPEECH_TAG_RE = re.compile(
    r"(?:(?<=[.!?\"”])\s+|^)"
    r"(?:(?:he|she|scaramouche|the\s+balladeer)\s+(?:muttered|sneered|scoffed|whispered|snapped|drawled|smirked|growled|hissed)"
    r"|(?:scaramouche|the\s+balladeer)\s+(?:said|replied|answered|retorted))\b[,.:]?\s*",
    re.IGNORECASE | re.MULTILINE,
)
_USER_MENTION_RE = re.compile(r"<@!?\d+>")
_ANY_MENTION_RE = re.compile(r"<@!?\d+>|<#\d+>|<@&\d+>")


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # (not right after "<@": removing the blank in "<@ !7>" would build a user mention out of inert text)
    text = re.sub(r"(?<!<@)[ \t]+([,.;:!?])", r"\1", text)
    text = text.strip()
    text = re.sub(r"^[\s,;]+", "", text)
    if re.match(r"\.(?!\.)", text):
        text = text[1:].lstrip()
    return text


def _starts_sentence(text: str, index: int) -> bool:
    """Is ``text[index]`` at the start of a line or sentence (only blanks, or sentence punctuation, before it)?"""
    i = index - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i < 0:
        return True
    last = text[i]
    return last == "\n" or last in _SENTENCE_END_CHARS or last in _CLOSING_CHARS


def _ends_sentence(text: str, index: int) -> bool:
    """Does a new sentence (or the end of the line / text) follow ``text[index:]``?"""
    j = index
    while j < len(text) and text[j] in " \t":
        j += 1
    if j >= len(text) or text[j] == "\n" or text[j] in ".!?…":
        return True
    if j == index:  # glued to the next word or to a comma / dash: the sentence goes on
        return False
    following = text[j]
    return following.isupper() or following.isdigit() or following in "\"“‘([<*"


def _is_stage_direction(match: re.Match[str]) -> bool:
    """Decide whether a ``*...*`` span is roleplay narration (True) or ordinary emphasis (False).

    Stage directions are standalone ("*smirks* Pathetic.", "Fine. *takes a step back* Go on.") or start with an
    action verb from the stage vocabulary. Emphasis sits inside a sentence ("I do *not* care", "This is *very
    important*") and has to stay, so anything that is not clearly an action is left alone.
    """
    text, inner = match.string, match.group(1)
    words = inner.split()
    boundary = _starts_sentence(text, match.start()) and _ends_sentence(text, match.end())
    if _STAGE_START_RE.match(inner):
        if boundary or len(words) >= 2 or _SUBJECT_LED_RE.match(inner):
            return True
        first = re.match(r"[A-Za-z']+", inner)
        word = first.group(0).lower() if first else ""
        return word.endswith("s") or word in _INTERJECTIONS  # "I suppose *sighs* you may stay"
    return boundary and len(words) >= 2 and inner[:1].islower()


def _drop_stage_directions(match: re.Match[str]) -> str:
    return "" if _is_stage_direction(match) else match.group(0)


def strip_narration(text: str | None, *, strip_mentions: bool = False, keep_if_empty: bool = True) -> str:
    """Remove roleplay narration (``*smirks*``, ``(sighs)``, ``[he scoffs]``, ``Scaramouche muttered,``).

    Ordinary content survives: ``**bold**``, ``f(x) = (x+1)^2``, citations like ``[1]``, URLs with
    parentheses, bullet lists, newlines and ``*emphasis*`` inside a sentence ("I do *not* care" keeps its
    "not"). User mentions are kept unless ``strip_mentions`` is set; ``sanitize_mentions`` validates them at
    send time.

    If the text was *nothing but* narration, ``keep_if_empty`` decides: True keeps its words without the markers
    (``*sigh*`` -> ``sigh``, fine for a chat message), False returns an empty string (nothing to speak aloud).
    """
    original = text or ""
    if not original.strip():
        return ""
    cleaned = _META_TAG_RE.sub("", original)
    cleaned = _ASTERISK_ACTION_RE.sub(_drop_stage_directions, cleaned)
    cleaned = _PAREN_ACTION_RE.sub("", cleaned)
    cleaned = _BRACKET_ACTION_RE.sub("", cleaned)
    cleaned = _SPEECH_TAG_RE.sub(lambda m: " " if m.start() else "", cleaned)
    if strip_mentions:
        cleaned = _ANY_MENTION_RE.sub("", cleaned)
    cleaned = _tidy(cleaned)
    if len(cleaned) < 3 and keep_if_empty:
        # Stripping removed (nearly) everything: keep the words, drop only the markers.
        fallback = _META_TAG_RE.sub("", original)
        for marker in "*[]()":
            fallback = fallback.replace(marker, "")
        if strip_mentions:
            fallback = _ANY_MENTION_RE.sub("", fallback)
        fallback = _tidy(fallback)
        if len(fallback) >= len(cleaned):
            cleaned = fallback
    return cleaned


# ── Text to speech ───────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://\S+|<https?://[^>]+>", re.IGNORECASE)
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
_SOURCES_MARK = "Sources:\n"


def split_sources(text: str | None) -> tuple[str, str]:
    """Split a reply into (body, sources_block). The block is empty when there is none."""
    text = text or ""
    # plain string search (a regex for this backtracks badly on long runs of whitespace)
    idx = text.rfind(_SOURCES_MARK)
    while idx > 0 and text[idx - 1] != "\n":  # the marker must start a line
        idx = text.rfind(_SOURCES_MARK, 0, idx)
    if idx == -1:
        return text, ""
    return text[:idx].rstrip(), text[idx:].strip()


def speech_text(text: str | None) -> str:
    """Make ``text`` suitable for TTS: no source list, URLs, markdown marks, citations or emoji."""
    body, _ = split_sources(text)
    body = _URL_RE.sub("", body)
    body = re.sub(r"\[\d{1,2}\]", "", body)
    body = re.sub(r"(\*\*|__|~~|\|\||`+)", "", body)
    body = body.replace("*", "")
    body = _EMOJI_RE.sub("", body)
    return _tidy(body)


# ── Mentions ─────────────────────────────────────────────────────────────────
_AT_RE = re.compile(r"(?<![\w<@])@(?=[^\s@<>])")
_TRAILING_PUNCT = ".,!?;:)'\"”’"


def resolve_name_mentions(text: str, name_to_id: Mapping[str, int]) -> str:
    """Turn ``@Display Name`` into ``<@id>``. Longest name wins, so ``@Alex`` never becomes ``@Al`` + ``ex``.

    ``name_to_id`` maps lower-case display names / usernames to user ids.
    """
    if not text or "@" not in text or not name_to_id:
        return text
    replacements: list[tuple[int, int, str]] = []
    last_end = 0
    for match in _AT_RE.finditer(text):
        if match.start() < last_end:
            continue
        tail = text[match.end(): match.end() + 100]
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", tail)][:4]
        chosen: tuple[int, int] | None = None
        for count in range(len(spans), 0, -1):
            segment = tail[spans[0][0]: spans[count - 1][1]]
            if "@" in segment:
                continue
            segment = segment.rstrip(_TRAILING_PUNCT)
            uid = name_to_id.get(normalize_text(segment))
            if uid and segment:
                chosen = (uid, len(segment))
                break
        if chosen:
            uid, length = chosen
            end = match.end() + length
            replacements.append((match.start(), end, f"<@{uid}>"))
            last_end = end
    if not replacements:
        return text
    out: list[str] = []
    cursor = 0
    for start, end, value in replacements:
        out.append(text[cursor:start])
        out.append(value)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
# First '@' of a run, possessive: linear on "@@@@..." and no word boundary, because Discord matches "@herewhat" too.
_MASS_MENTION_RE = re.compile(r"(?<!@)@++(?=everyone|here)", re.IGNORECASE)


def neutralize_mass_mentions(text: str | None) -> str:
    """Defuse ``@everyone`` / ``@here`` and role mentions, the pings nobody should trigger through the bot.

    A removed role mention becomes a space, never nothing: deleting it could glue its neighbours into a new
    mention (``<@&<@&1>1>`` would turn back into ``<@&1>``).
    """
    if not text:
        return ""
    text = _ROLE_MENTION_RE.sub(" ", text)
    return _MASS_MENTION_RE.sub("", text)


def sanitize_mentions(text: str, valid_user_ids: Iterable[int] | None, *, max_mentions: int = 2) -> str:
    """Make a model-written reply safe to send.

    * ``@everyone`` / ``@here`` are neutralised, role and unknown-user mentions are removed;
    * only real members (``valid_user_ids``) stay mentioned, at most ``max_mentions`` of them.

    Every pass is a single linear scan and replaces a removed mention by a space, so no input can rebuild a
    mention out of the pieces that are left (``<@<@7>7>``).
    """
    if not text:
        return ""
    valid = {int(uid) for uid in (valid_user_ids or [])}
    text = neutralize_mass_mentions(text)
    kept = 0

    def _user(match: re.Match[str]) -> str:
        nonlocal kept
        uid = int(re.search(r"\d+", match.group(0)).group())  # type: ignore[union-attr]
        if uid in valid and kept < max_mentions:
            kept += 1
            return f"<@{uid}>"
        return " "

    text = _USER_MENTION_RE.sub(_user, text)
    return _tidy(text)


# ── Chunking ─────────────────────────────────────────────────────────────────
def chunk_text(text: str | None, limit: int = 1900) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters, preferring paragraph / sentence breaks."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        floor = limit // 2
        cut = -1
        for candidate in (
            window.rfind("\n\n"),
            window.rfind("\n"),
            max(window.rfind(". "), window.rfind("! "), window.rfind("? ")) + 1,
            window.rfind(" "),
        ):
            if candidate >= floor:
                cut = candidate
                break
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return [chunk for chunk in chunks if chunk]


# ── Small parsers ────────────────────────────────────────────────────────────
_VERDICT_RE = re.compile(
    r"^\W*(correct|right|wrong|incorrect|accept(?:ed)?|refuse[ds]?|reject(?:ed)?|deny|denied)\b[\s:.,!\-–—]*",
    re.IGNORECASE,
)
_POSITIVE = {"correct", "right", "accept", "accepted"}


def parse_verdict(text: str | None) -> tuple[bool | None, str]:
    """Read a leading CORRECT/WRONG or ACCEPT/REFUSE token. Returns (verdict, remaining text).

    The verdict is ``None`` when the reply does not start with a recognised token.
    """
    text = (text or "").strip()
    match = _VERDICT_RE.match(text)
    if not match:
        return None, text
    return match.group(1).lower() in _POSITIVE, text[match.end():].strip()


_NICK_BLOCK_RE = re.compile(
    r"n[i1]gg|f[a4]gg?[o0]t|k[i1]ke\b|tr[a4]nn(?:y|ie)|ret[a4]rd|ch[i1]nk\b|sp[i1]c\b|wetback|towelhead",
    re.IGNORECASE,
)
_NICK_GENERIC = {
    "hmph", "tch", "go on", "what now", "pathetic", "unimpressive", "predictable", "well", "spare me",
    "continue", "speak plainly", "nickname", "none", "n/a", "yes", "no",
}


def clean_nickname(raw: str | None, *, max_words: int = 3, max_len: int = 24) -> str | None:
    """Validate a model-written nickname. Returns ``None`` for anything that is not a short, plain name."""
    if not raw:
        return None
    value = raw.strip().splitlines()[0] if raw.strip() else ""
    value = value.strip(" \t\"'“”‘’`*_~")
    value = re.sub(r"[<>@#*_`~|\[\]\\]", "", value).strip(" .,!?:;-")
    if not (2 <= len(value) <= max_len) or len(value.split()) > max_words:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z' \-]*", value):
        return None
    if value.lower() in _NICK_GENERIC or _NICK_BLOCK_RE.search(value):
        return None
    return value


_PERSONAL_RE = re.compile(
    r"\b(?:you|your|yours|yourself|u|ur|me|my|mine|myself|i|i'm|im|i've|ive|i'll|i'd|we|us|our|ours)\b", re.IGNORECASE
)
_QUESTION_START_RE = re.compile(
    r"^(?:what|who|whom|whose|when|where|which|why|how|is|are|was|were|does|do|did|can|could|will|would|should"
    r"|has|have|tell me|explain|define)\b"
)
_LOOKUP_PHRASES = (
    "news about", "look up", "lookup", "search for", "search the web", "find out", "tell me about", "google",
    "latest", "current", "price of", "score", "release date", "how much", "how many", "what is", "what are",
    "who is", "who are", "when did", "when was", "when is", "where is", "where are", "define", "calculate",
)
_URLISH_RE = re.compile(r"https?://\S+|<[@#&!]*\d+>")


def needs_search(text: str | None) -> bool:
    """Decide whether a plain chat message looks like a factual lookup worth sending to a search engine.

    Personal / conversational questions ("do you love me?") are *never* sent to a third party.
    """
    cleaned = _URLISH_RE.sub(" ", normalize_text(text)).strip()
    if len(cleaned) < 8 or len(cleaned) > 300:
        return False
    if _PERSONAL_RE.search(cleaned):
        return False
    lookup = any(phrase in cleaned for phrase in _LOOKUP_PHRASES)
    starts_like_question = bool(_QUESTION_START_RE.match(cleaned))
    return lookup or (cleaned.endswith("?") and starts_like_question)


_QUERY_PREFIX_RE = re.compile(
    r"^\s*(?:search the web for|search for|solve or respond to this accurately|look up|google|find out|tell me about)\s*[:\-]?\s*",
    re.IGNORECASE,
)


def extract_search_query(text: str | None, max_len: int = 160) -> str:
    """Reduce a message to a compact search query: no mentions/URLs/instruction prefixes, bounded length."""
    cleaned = _URLISH_RE.sub(" ", text or "")
    cleaned = _QUERY_PREFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:;")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit(" ", 1)[0]
    return cleaned.strip()


# ── Prompt hygiene ───────────────────────────────────────────────────────────
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def prompt_safe(text: str | None, limit: int = 140) -> str:
    """Flatten user-supplied text before it is embedded in the ``[A|B|C]`` context line of a prompt."""
    cleaned = _CONTROL_RE.sub(" ", text or "")
    cleaned = cleaned.replace("|", "/").replace("[", "(").replace("]", ")")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def clean_display_name(name: str | None, fallback: str = "you") -> str:
    cleaned = re.sub(r"[`<>{}\[\]|\\]", "", _CONTROL_RE.sub(" ", name or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:32]
    return cleaned or fallback
