"""Reply clean-up: remove roleplay narration without destroying ordinary content.

The old stripper deleted every ``*...*``, ``(...)`` and ``[...]`` span. That erased bold text ("**42**" became
"**"), flipped meanings ("I do *not* care" became "I do care"), broke maths ("f(x) = (x+1)^2" became "f = ^2"),
cut source links ("Boiling_(disambiguation)") and deleted ``[ANSWER: ...]`` before ``!trivia`` could read it. Here a
span is removed only when it reads like a stage direction. Pure functions, no Discord or provider imports.
"""

from __future__ import annotations

import re


def _verb_forms(base: str, *, e_drop: bool = False, double: bool = False, es: bool = False) -> str:
    stem = base[:-1] if e_drop else base
    if double:
        doubled = base + base[-1]
        forms = [base, base + "s", doubled + "ed", doubled + "ing"]
    else:
        forms = [base, base + ("es" if es else "s"), stem + "ed", stem + "ing"]
    return "|".join(re.escape(form) for form in forms)


_STAGE_VERBS = "|".join([
    _verb_forms("smirk"), _verb_forms("sigh"), _verb_forms("scoff"), _verb_forms("glare", e_drop=True),
    _verb_forms("snort"), _verb_forms("chuckle", e_drop=True), _verb_forms("laugh"), _verb_forms("frown"),
    _verb_forms("mutter"), _verb_forms("whisper"), _verb_forms("sneer"), _verb_forms("scowl"),
    _verb_forms("grin", double=True), _verb_forms("smile", e_drop=True), _verb_forms("stare", e_drop=True),
    _verb_forms("glance", e_drop=True), _verb_forms("flick"), _verb_forms("clench", es=True), _verb_forms("huff"),
    _verb_forms("exhale", e_drop=True), _verb_forms("inhale", e_drop=True), _verb_forms("wince", e_drop=True),
    _verb_forms("flinch", es=True), _verb_forms("bristle", e_drop=True), _verb_forms("shrug", double=True),
    _verb_forms("nod", double=True), _verb_forms("blink"), _verb_forms("squint"), _verb_forms("pause", e_drop=True),
    _verb_forms("snicker"), _verb_forms("growl"), _verb_forms("hiss", es=True), r"hmph", r"tch",
])
_POSS = r"(?:his\s+|her\s+|their\s+)?"
_STAGE_PHRASES = "|".join([
    rf"rolls?\s+{_POSS}eyes", rf"rolled\s+{_POSS}eyes", rf"shakes?\s+{_POSS}head", rf"crosses?\s+{_POSS}arms",
    rf"folds?\s+{_POSS}arms", rf"tilts?\s+{_POSS}head", rf"narrows?\s+{_POSS}eyes", r"raises?\s+(?:an?\s+)?eyebrow",
    r"turns?\s+(?:away|around|back)", r"looks?\s+(?:away|down|up|aside)", r"steps?\s+(?:closer|back|forward|aside)",
    r"leans?\s+(?:in|closer|back|forward)", r"adjusts?\s+(?:his\s+|her\s+)?(?:hat|sleeve|gloves?)",
])
_SUBJECT = r"(?:(?:he|she|they|scaramouche|the\s+balladeer|wanderer)\s+)?"
_STAGE_BODY = rf"{_SUBJECT}(?:(?:{_STAGE_VERBS})|(?:{_STAGE_PHRASES}))\b[^()\[\]\n]{{0,80}}"
_ADVERBS = r"(?:sarcastically|dryly|coldly|quietly|softly|bitterly|mockingly|flatly|icily|sharply|wryly)"

# Meta tags the model sometimes copies from history ("[voice message] ..."): never part of a reply.
_META_TAG_RE = re.compile(
    r"\[\s*(?:sent\s+(?:a\s+|an\s+)?)?(?:voice\s+message|voice\s+note|voice|image|video|audio|attachment)\s*\]",
    re.IGNORECASE,
)
# Single-asterisk spans on one line. Bold (**x**), bullets ("* x") and maths ("2 * 3") never match.
_ASTERISK_RE = re.compile(r"(?<![*\w])\*(?![\s*])([^*\n]{1,200}?)(?<!\s)\*(?![*\w])")
_STAGE_START_RE = re.compile(rf"(?:{_STAGE_BODY}|{_ADVERBS})", re.IGNORECASE)
_SUBJECT_LED_RE = re.compile(r"(?:he|she|they|scaramouche|the\s+balladeer|wanderer)\s+\w", re.IGNORECASE)
_INTERJECTIONS = frozenset({"hmph", "tch", "tsk", "ugh"})
_PAREN_ACTION_RE = re.compile(rf"\(\s*(?:{_STAGE_BODY}|{_ADVERBS})\s*\)", re.IGNORECASE)
_BRACKET_ACTION_RE = re.compile(rf"\[\s*(?:{_STAGE_BODY}|{_ADVERBS})\s*\]", re.IGNORECASE)
# "Scaramouche muttered," only as a tag at the start of a sentence; "They said the library closes" is content.
_SPEECH_TAG_RE = re.compile(
    r"(?:(?<=[.!?\"”])\s+|^)"
    r"(?:(?:he|she|scaramouche|the\s+balladeer|wanderer)\s+(?:muttered|sneered|scoffed|whispered|snapped|drawled|smirked|growled|hissed)"
    r"|(?:scaramouche|the\s+balladeer|wanderer)\s+(?:said|replied|answered|retorted))\b[,.:]?\s*",
    re.IGNORECASE | re.MULTILINE,
)
_ANY_MENTION_RE = re.compile(r"<@!?\d+>|<#\d+>|<@&\d+>")
_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
# First '@' of a run, possessive: linear on "@@@@..." and no word boundary (Discord pings "@herewhat" too).
_MASS_MENTION_RE = re.compile(r"(?<!@)@++(?=everyone|here)", re.IGNORECASE)
_SENTENCE_END = ".!?…:;"
_CLOSERS = "\"”’)]"


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # (never right after "<@": closing the gap in "<@ !7>" would build a mention out of inert text)
    text = re.sub(r"(?<!<@)[ \t]+([,.;:!?])", r"\1", text)
    text = text.strip()
    text = re.sub(r"^[\s,;]+", "", text)
    if re.match(r"\.(?!\.)", text):
        text = text[1:].lstrip()
    return text


def _starts_sentence(text: str, index: int) -> bool:
    i = index - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i < 0 or text[i] == "\n" or text[i] in _SENTENCE_END or text[i] in _CLOSERS


def _ends_sentence(text: str, index: int) -> bool:
    j = index
    while j < len(text) and text[j] in " \t":
        j += 1
    if j >= len(text) or text[j] == "\n" or text[j] in ".!?…":
        return True
    if j == index:
        return False
    nxt = text[j]
    return nxt.isupper() or nxt.isdigit() or nxt in "\"“‘([<*"


def _is_stage_direction(match: re.Match[str]) -> bool:
    """``*smirks* Pathetic.`` is narration; ``I do *not* care`` is emphasis and must keep its word."""
    text, inner = match.string, match.group(1)
    words = inner.split()
    boundary = _starts_sentence(text, match.start()) and _ends_sentence(text, match.end())
    if _STAGE_START_RE.match(inner):
        if boundary or len(words) >= 2 or _SUBJECT_LED_RE.match(inner):
            return True
        first = re.match(r"[A-Za-z']+", inner)
        word = first.group(0).lower() if first else ""
        return word.endswith("s") or word in _INTERJECTIONS
    return boundary and len(words) >= 2 and inner[:1].islower()


def neutralize_mass_mentions(text: str | None) -> str:
    """Defuse ``@everyone`` / ``@here`` and role mentions. A removed role mention becomes a space, so the pieces
    left behind can never form a new mention (``<@&<@&1>1>``)."""
    if not text:
        return ""
    return _MASS_MENTION_RE.sub("", _ROLE_MENTION_RE.sub(" ", text))


def strip_narration(text: str | None, *, strip_mentions: bool = True, keep_if_empty: bool = True) -> str:
    """Remove roleplay narration (``*smirks*``, ``(sighs)``, ``[he scoffs]``, ``Scaramouche muttered,``).

    Ordinary content survives: bold, ``*emphasis*`` inside a sentence, ``f(x) = (x+1)^2``, ``(see page 4)``,
    citations ``[1]``, tags such as ``[ANSWER: ...]``, URLs with parentheses and "They said ...". Raw mentions the
    model typed are removed (``strip_mentions``, the historical behaviour; real ones are added afterwards from
    ``@Name``), and ``@everyone`` / ``@here`` never survive.
    """
    original = text or ""
    if not original.strip():
        return ""
    cleaned = _META_TAG_RE.sub("", original)
    cleaned = _ASTERISK_RE.sub(lambda m: "" if _is_stage_direction(m) else m.group(0), cleaned)
    cleaned = _PAREN_ACTION_RE.sub("", cleaned)
    cleaned = _BRACKET_ACTION_RE.sub("", cleaned)
    cleaned = _SPEECH_TAG_RE.sub(lambda m: " " if m.start() else "", cleaned)
    if strip_mentions:
        cleaned = _ANY_MENTION_RE.sub(" ", cleaned)
    cleaned = _tidy(neutralize_mass_mentions(cleaned))
    if len(cleaned) < 3 and keep_if_empty:
        # Stripping removed (nearly) everything: keep the words, drop only the markers.
        fallback = _META_TAG_RE.sub("", original)
        for marker in "*[]()":
            fallback = fallback.replace(marker, "")
        if strip_mentions:
            fallback = _ANY_MENTION_RE.sub(" ", fallback)
        fallback = _tidy(neutralize_mass_mentions(fallback))
        if len(fallback) >= len(cleaned):
            cleaned = fallback
    return cleaned


def chunk_text(text: str | None, limit: int = 1900) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters, preferring paragraph / sentence breaks."""
    text = (text or "").strip()
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
