"""Bounded, deterministic helpers for awareness, memory callbacks, and voice intent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable, Mapping


_WORD_RE = re.compile(r"[a-z0-9']+")
# Phrase patterns (not substrings): "suicide" is not a substring of "suicidal", "self harm" misses "self-harm", and
# "poison" or "not okay" alone fire on game chat and pizza opinions.
_SELF = r"my\s?self"
_CRISIS_PATTERNS = (
    rf"\bkill(?:ing)?\s*{_SELF}\b",  # "killmyself" too
    r"\bsuicid(?:e|al)\b(?!\s+(?:squad|mission|prevention|hotline|bombers?|moves?|plays?|strateg(?:y|ies)|tactics?|charge|run|rush|attack|dive|jump|decision|idea|driving|driver|speed|enem(?:y|ies)|fun|pilot|raid))",
    r"\b(?:sewer\s?slide|sewerslide|suisside)\b",
    r"\bself[\s-]?(?:harm|injur(?:y|ing|e))(?:ing)?\b",
    rf"\bun-?aliv(?:e|ing)\s+(?:{_SELF}|me)\b",
    r"\b(?:want|wanna|going|gonna|plan(?:ning|s)?|trying|thinking(?:\s+(?:of|about))?|about|ready)\s+(?:to\s+)?un-?aliv(?:e|ing)\b",
    rf"\b(?:off|offing|end|ending)\s*{_SELF}\b",
    r"\bend(?:ing)?\s+(?:my\s+(?:own\s+)?life|it\s+all)\b",
    r"\btak(?:e|ing)\s+my\s+(?:own\s+)?life\b",
    r"\b(?:i'?m|im|i\s+am|i'?ll|i\s+will|gonna|going\s+to|about\s+to|ready\s+to|planning\s+to|want\s+to|wanna)\s+(?:just\s+)?end(?:ing)?\s+it\b(?!\s+(?:with|between|here|for\s+good\s+with)\b)",
    r"\b(?:(?:want|wish|need)(?:ed)?\s+(?:to|2)|wanna)\s+(?:die|be\s+dead|disappear\s+forever|not\s+exist)\b(?!\W{0,4}\s*(?:of|from)\s+(?:embarrassment|laughing|laughter|cringe|boredom|shame|happiness|joy|excitement)|\s+laughing)",
    r"\bwish(?:ed)?\s+(?:i|that\s+i)\s+(?:(?:was|were|am)\s+(?:dead|gone|never\s+born)|(?:wasn'?t|weren'?t|was\s+not|were\s+not)\s+(?:alive|born)|didn'?t\s+exist|(?:could|can)\s+(?:just\s+)?(?:disappear|vanish|die)|would(?:n'?t)?\s+wake\s+up)\b",
    r"\b(?:don'?t|do\s+not)\s+want\s+to\s+(?:live|be\s+alive|exist|be\s+here\s+anymore)\b",
    r"\b(?:don'?t|do\s+not)\s+(?:want\s+to|wanna)\s+wake\s+up\b(?!\s+(?:early|at|for|before|so|to|in|when|on)\b)",
    r"\bbetter\s+off\s+(?:dead|without\s+me)\b",
    r"\bno\s+(?:reason|point)\s+(?:to|in)\s+(?:live|living|going\s+on|being\s+alive)\b",
    r"\blife\s+(?:is\s+)?(?:isn'?t|not|no\s+longer)\s+worth\s+(?:it|living)\b|\bnot\s+worth\s+(?:living|being\s+alive)\b",
    r"\b(?:done|finished|through|tired|sick)\s+(?:with|of)\s+(?:life(?!'s)|living|being\s+alive)\b",
    r"\b(?:no\s?one|nobody)\s+(?:would|will|'?d)\s+(?:even\s+)?(?:miss\s+me|notice\s+(?:me\s+gone|if\s+i)|care\s+if\s+i\s+(?:died|was\s+gone|were\s+gone|disappeared|wasn'?t\s+here|didn'?t\s+exist))\b",
    r"\bcan'?t\s+go\s+on\b(?!\s+(?:vacation|holiday|a\s+trip|the\s+trip|trips?|a\s+date|dates|the\s+ride|rides?|stage|tour|break|without\s+(?:coffee|food|tea|sleep|my\s+\w+)))",
    rf"\b(?:hurt|hurting|harm|harming)\s*{_SELF}\b(?!\s+(?:at|while|in|during|playing|running|lifting|training|working|on)\b)",
    rf"\bcut(?:ting)?\s*{_SELF}\b(?!\s+(?:shaving|cooking|chopping|slicing|opening|on\s+(?:a|the|some)?\s*(?:paper|glass|knife)|while|with\s+a\s+(?:paper|kitchen)))",
    rf"\b(?:hang|hanging|shoot|shooting|stab|stabbing|drown|drowning|poison|poisoning|starve|starving|suffocate|suffocating|strangle|strangling)\s+{_SELF}\b",
    r"\b(?:slit|slitting|cut|cutting|slash|slashing)\s+my\s+(?:own\s+)?(?:wrists?|throat|veins?)\b",
    r"\b(?:going|gonna|want|wanna|about)\s+(?:to\s+)?jump\s+(?:off|from)\s+(?:a|the|this)\s+(?:bridge|building|roof|balcony|cliff)\b",
    r"\b(?:took|taken|swallowed)\s+(?:too\s+many|a\s+bunch\s+of|all\s+(?:my|the|of\s+my))\s+(?:pills|tablets|meds|medication|painkillers|sleeping\s+pills)\b",
    r"\boverdos(?:e|ed|ing)\b",
    # danger from someone else
    r"\bdomestic\s+violence\b|\bbeing\s+abused\b|\bsomeone\s+is\s+(?:following|stalking)\s+me\b",
    # a few other languages
    r"\bquiero\s+morir(?:me)?\b|\bme\s+quiero\s+matar\b|\bsuicidarme\b|\bquitarme\s+la\s+vida\b",
    r"\bje\s+veux\s+mourir\b|\bquero\s+morrer\b|\bquero\s+me\s+matar\b",
)
_CRISIS_RE = re.compile("|".join(f"(?:{p})" for p in _CRISIS_PATTERNS), re.IGNORECASE)
# "kms" is slang for "kill myself", but also kilometres ("5 kms") and Key Management Service.
_KMS_RE = re.compile(r"(?<![\w/.-])(?<!\d\s)k\.?m\.?s(?![\w/-])(?!\s+(?:server|activation|key|keys|client|host|licen[cs]e|service|auth|encrypt\w*|vault|setup|script|tool|ip|address|port|away|apart|per|from|long|of|total|walk\w*|run\w*|drive\w*|ride\w*|cycl\w*))", re.IGNORECASE)
_TECH_OR_DISTANCE_RE = re.compile(r"\b(?:aws|azure|gcp|gcloud|windows|office|activat\w*|licen[cs]e|encrypt\w*|cmk|vault|s3|iam|lambda|terraform|kubernetes|docker|sdk|api|ssl|tls|server|cloud|vmware|km|miles?|distance)\b", re.IGNORECASE)
_LEET = str.maketrans("013457@$!", "oieastasi")
_SPACED_LETTERS_RE = re.compile(r"\b(?:[a-z][ .\-_*]){2,}[a-z]\b")
_STRETCHED_RE = re.compile(r"([a-z])\1{2,}")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_HIGH_STAKES_RE = re.compile(
    r"\b(?:medical\s+emergency|chest\s+pain|(?:can'?t|cannot|cant)\s+breathe|severe\s+bleeding|emergency\s+room|"
    r"call\s+911|police\s+emergency|legal\s+emergency|restraining\s+order|house\s+is\s+on\s+fire|gas\s+leak|"
    r"not\s+breathing|poison\s+control|(?:been|was|got|being|i'?m|is|am)\s+poisoned|(?:swallowed|drank|ate)\s+(?:poison|bleach|chemicals)|"
    r"(?:he|she|they|someone|somebody|my\s+\w+)\s+(?:is|was|went|has\s+gone)\s+unconscious|"
    r"medical\s+advice|medication\s+dose|legal\s+advice|investment\s+advice|tax\s+advice)\b",
    re.IGNORECASE,
)
_DISTRESS_RE = re.compile(
    r"\b(?:awful\s+day|terrible\s+day|overwhelmed|falling\s+apart|(?:i'?m|im|i\s+am)\s+(?:so\s+|really\s+)?scared|"
    r"i\s+feel\s+(?:so\s+)?alone|(?:can'?t|cant)\s+stop\s+crying|panic\s+attack|really\s+upset|"
    r"(?:i'?m|im|i\s+am|feeling|i\s+feel)\s+(?:really\s+|so\s+)?not\s+okay(?!\s+with)|need\s+someone|everything\s+hurts|"
    r"having\s+a\s+hard\s+time|not\s+doing\s+well|seriously\s+struggling|i\s+feel\s+unsafe|(?:i'?m|im|i\s+am)\s+not\s+safe)\b",
    re.IGNORECASE,
)


def _crisis(lowered: str) -> bool:
    """Self-harm, suicide or danger signals, including the spellings people use to get past filters."""
    plain = _ZERO_WIDTH_RE.sub("", lowered)
    views = [plain]
    if plain != lowered:  # a zero-width character can hide inside a word or stand in for the space between two
        views.append(_ZERO_WIDTH_RE.sub(" ", lowered))
    if any(ch in plain for ch in "013457@$!"):
        views.append(plain.translate(_LEET))
    if _SPACED_LETTERS_RE.search(plain):
        views.append(_SPACED_LETTERS_RE.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), plain))
    if _STRETCHED_RE.search(plain):
        views.append(_STRETCHED_RE.sub(r"\1", plain))
    if any(_CRISIS_RE.search(view) for view in views):
        return True
    return bool(_KMS_RE.search(plain) and not _TECH_OR_DISTANCE_RE.search(plain))


_FACTUAL_PREFIXES = (
    "what is ", "what are ", "when did ", "where is ", "who is ", "how many ",
    "calculate ", "define ", "what does ",
)
_FACTUAL_TERMS = (
    "calculate", "compute", "solve", "convert", "definition", "translate",
    "capital of", "population of", "what year", "what date",
)
_ADVICE_MARKERS = (
    "what should i", "how should i", "how do i", "can you help me", "need advice",
    "what would you do", "should i", "help me fix", "help with", "any advice",
)
_SENSITIVE_MEMORY = (
    "password", "passcode", "api key", "token", "secret key", "address", "social security",
    "credit card", "bank account", "trauma", "abuse", "assault", "self harm", "suicide",
    "diagnosed", "medication", "medical", "therapy", "pregnant", "phone number",
    "email address", "social security number", "real name", "where i live",
)
_PLAYFUL_NEGATIVE = (
    "annoying", "dramatic", "rude", "mean", "insufferable", "brat", "pest", "jerk",
    "obnoxious", "a pain", "gets on my nerves", "too much",
)


def _words(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall((text or "").lower()) if len(word) > 2}


@dataclass(frozen=True)
class SafetyContext:
    crisis: bool = False
    high_stakes: bool = False
    distressed: bool = False

    @property
    def protective(self) -> bool:
        return self.crisis or self.high_stakes or self.distressed


def classify_safety(text: str) -> SafetyContext:
    lowered = (text or "").lower().replace("’", "'").replace("‘", "'")
    crisis = _crisis(lowered)
    high_stakes = crisis or bool(_HIGH_STAKES_RE.search(lowered))
    # A single explicit distress phrase is enough; ordinary negative adjectives are not.
    return SafetyContext(crisis=crisis, high_stakes=high_stakes, distressed=bool(_DISTRESS_RE.search(lowered)))


def advice_kind(text: str, *, safety=None) -> str:
    lowered = re.sub(r"<@!?\d+>", "", (text or "").strip().lower())
    safety = safety if safety is not None else classify_safety(lowered)
    if safety.protective:
        return "high_stakes"
    if any(lowered.startswith(prefix) for prefix in _FACTUAL_PREFIXES) or any(term in lowered for term in _FACTUAL_TERMS):
        return "factual"
    if any(marker in lowered for marker in _ADVICE_MARKERS):
        return "values"
    return "none"


def choose_duo_advice_mode(text: str, roll: float, *, safety=None) -> str:
    """Choose a bounded duo mode. The caller supplies the random roll for testability."""
    safety = safety if safety is not None else classify_safety(text)
    if safety.protective:
        return "protective"
    kind = advice_kind(text, safety=safety)
    if kind != "values":
        return ""
    if roll < 0.06:
        return "goodcop"
    if roll < 0.09:
        return "contradict"
    return ""


def protective_prompt(bot_name: str, safety: SafetyContext) -> str:
    if not safety.protective:
        return ""
    identity = "Scaramouche" if bot_name.lower() == "scaramouche" else "Wanderer"
    return (
        f"PROTECTIVE_OVERRIDE: {identity} recognizes genuine distress before replying. "
        "Be calm, useful, and present. Do not diagnose, mock, threaten, moralize, or use sarcasm. "
        "For immediate danger, encourage appropriate real-world emergency help. Stay in character "
        "through restrained concern, not cruelty."
    )


def playful_negative_target(text: str, bot_names: Mapping[str, Iterable[str]]) -> str:
    """Return the discussed bot only for a clearly playful public jab."""
    lowered = (text or "").lower()
    if classify_safety(lowered).protective or any(x in lowered for x in ("abuse", "harass", "threat", "unsafe")):
        return ""
    if not any(marker in lowered for marker in _PLAYFUL_NEGATIVE):
        return ""
    for bot_name, aliases in bot_names.items():
        if any(re.search(rf"\b{re.escape(alias.lower())}\b", lowered) for alias in aliases):
            return bot_name.lower()
    return ""


def is_sensitive_memory(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SENSITIVE_MEMORY) or classify_safety(lowered).protective


@dataclass(frozen=True)
class RecallMatch:
    message_id: int
    channel_id: int
    content: str
    timestamp: float
    score: float


def select_relevant_recall(
    current: str,
    candidates: Iterable[Mapping[str, object]],
    *,
    minimum_score: float = 0.46,
) -> RecallMatch | None:
    """Select one exact, non-sensitive source from an already-bounded candidate set."""
    current_words = _words(current)
    if len(current_words) < 3 or is_sensitive_memory(current):
        return None
    best: RecallMatch | None = None
    for item in candidates:
        content = str(item.get("content") or "").strip()
        if len(content) < 12 or is_sensitive_memory(content):
            continue
        old_words = _words(content)
        if len(old_words) < 3:
            continue
        overlap = len(current_words & old_words) / max(1, len(current_words | old_words))
        current_abs = any(x in (current or "").lower() for x in ("never", "always", "don't", "do not", "hate", "love"))
        old_abs = any(x in content.lower() for x in ("never", "always", "don't", "do not", "hate", "love"))
        score = overlap + (0.09 if current_abs and old_abs else 0.0)
        if score >= minimum_score and (best is None or score > best.score):
            best = RecallMatch(
                message_id=int(item.get("id") or 0),
                channel_id=int(item.get("channel_id") or 0),
                content=content[:280],
                timestamp=float(item.get("ts") or 0),
                score=min(1.0, score),
            )
    return best


@dataclass(frozen=True)
class VoiceState:
    category: str
    intensity: float
    temperature: float
    top_p: float


def resolve_voice_state(
    user: Mapping[str, object] | None,
    mood: int = 0,
    *,
    delivery_intent: str = "",
    previous: VoiceState | None = None,
) -> VoiceState:
    """Resolve delivery without an LLM. Temperature/top-p are clamped metadata only."""
    profile = user or {}
    arc = str(profile.get("emotional_arc") or "guarded")
    intent = (delivery_intent or "").lower()
    if "concern" in intent or "protect" in intent:
        category, intensity = "concerned", 0.62
    elif profile.get("conflict_open") and mood <= -4:
        category, intensity = "heated", 0.86
    elif "reconcil" in intent or int(profile.get("repair_progress") or 0) > 0:
        category, intensity = "reconciliation", 0.55
    elif "question" in intent or "curious" in intent:
        category, intensity = "curious", 0.48
    elif mood <= -6:
        category, intensity = "cutting", 0.78
    elif mood <= -2:
        category, intensity = "cold", 0.62
    elif arc in {"attached", "tender", "trusting"} or int(profile.get("affection") or 0) >= 75:
        category, intensity = "restrained_vulnerability", 0.48
    elif "mock" in intent or "teas" in intent:
        category, intensity = "mocking", 0.58
    else:
        category, intensity = "guarded", 0.45

    if previous and previous.category != category:
        # Smooth only intensity; never preserve an unsafe/incorrect old category.
        intensity = previous.intensity * 0.35 + intensity * 0.65
    intensity = max(0.2, min(0.9, float(intensity)))
    temperature = max(0.1, min(1.0, 0.48 + intensity * 0.32))
    top_p = max(0.1, min(1.0, 0.58 + intensity * 0.22))
    return VoiceState(category, intensity, temperature, top_p)


def style_voice_text(text: str, state: VoiceState) -> str:
    """Punctuation-only delivery hints; strips control tags supplied by users/models."""
    cleaned = re.sub(r"<[^>]{1,80}>", "", text or "")
    cleaned = re.sub(r"\[(?:voice|tts|emotion|style|speed|pitch)[^\]]*\]", "", cleaned, flags=re.I)
    cleaned = cleaned.strip()
    if state.category in {"heated", "cutting", "cold"}:
        return cleaned.replace(";", ".").replace(" — ", ". ")
    if state.category in {"concerned", "reconciliation", "restrained_vulnerability"}:
        return cleaned.replace("...", ", ").replace(";", ",")
    if state.category == "curious":
        return cleaned.replace("...", ", ")
    return cleaned


def parse_id_set(raw: str) -> set[int]:
    result: set[int] = set()
    for value in re.split(r"[,\s]+", raw or ""):
        if value.isdigit():
            result.add(int(value))
    return result


def activity_snapshot(member) -> dict | None:
    """Return only Discord-exposed current activity fields."""
    for activity in getattr(member, "activities", ()) or ():
        kind = type(activity).__name__.lower()
        name = str((getattr(activity, "title", "") if "spotify" in kind else getattr(activity, "name", "")) or "").strip()
        if not name:
            continue
        started = getattr(getattr(activity, "timestamps", None), "start", None)
        if isinstance(started, datetime):
            started_ts = started.timestamp()
        else:
            started_ts = 0.0
        activity_type = str(getattr(activity, "type", "") or "").lower()
        if "spotify" in kind:
            activity_kind = "spotify"
        elif "game" in kind or "playing" in activity_type:
            activity_kind = "game"
        else:
            continue
        return {
            "kind": activity_kind,
            "name": name[:120],
            "details": str((getattr(activity, "album", "") if "spotify" in kind else getattr(activity, "details", "")) or "")[:160],
            "state": str((getattr(activity, "artist", "") if "spotify" in kind else getattr(activity, "state", "")) or "")[:160],
            "started_ts": started_ts,
        }
    return None
