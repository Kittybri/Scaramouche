"""Crisis detection and the out-of-character safety reply.

When someone says they may hurt themselves the roleplay stops: the bot answers once with a plain,
caring message and softens the persona for a while afterwards.
"""

from __future__ import annotations

import re

from text_utils import normalize_text

_SELF = r"my\s?self"
_CRISIS_PATTERNS = [
    # saying it outright
    rf"\bkill(?:ing)?\s+{_SELF}\b",
    r"\bsuicid(?:e|al)\b(?!\s+(?:squad|mission|prevention|hotline|bombers?|moves?|plays?|strateg(?:y|ies)|tactics?|charge|run|rush|attack|dive|jump|decision|idea|driving|driver|speed|enem(?:y|ies)|fun|pilot|raid))",
    r"\b(?:sewer\s?slide|sewerslide|suisside)\b",
    r"\bself[\s-]?(?:harm|injur(?:y|ing|e))(?:ing)?\b",
    # euphemisms and slang
    rf"\bun-?aliv(?:e|ing)\s+(?:{_SELF}|me)\b",
    r"\b(?:want|wanna|going|gonna|plan(?:ning|s)?|trying|thinking(?:\s+(?:of|about))?|about|ready)\s+(?:to\s+)?un-?aliv(?:e|ing)\b",
    rf"\b(?:off|offing|end|ending)\s+{_SELF}\b",
    # ending it
    r"\bend(?:ing)?\s+(?:my\s+(?:own\s+)?life|it\s+all)\b",
    r"\btak(?:e|ing)\s+my\s+(?:own\s+)?life\b",
    r"\b(?:i'?m|im|i\s+am|i'?ll|i\s+will|gonna|going\s+to|about\s+to|ready\s+to|planning\s+to|want\s+to|wanna)\s+(?:just\s+)?end(?:ing)?\s+it\b(?!\s+(?:with|between|here|for\s+good\s+with)\b)",
    # wanting to be dead / gone
    r"\b(?:(?:want|wish|need)(?:ed)?\s+(?:to|2)|wanna)\s+(?:die|be\s+dead|disappear\s+forever|not\s+exist)\b(?!\W{0,4}\s*(?:of|from)\s+(?:embarrassment|laughing|laughter|cringe|boredom|shame|happiness|joy|excitement)|\s+laughing)",
    r"\bwish(?:ed)?\s+(?:i|that\s+i)\s+(?:(?:was|were|am)\s+(?:dead|gone|never\s+born)|(?:wasn'?t|weren'?t|was\s+not|were\s+not)\s+(?:alive|born)|didn'?t\s+exist|(?:could|can)\s+(?:just\s+)?(?:disappear|vanish|die)|would(?:n'?t)?\s+wake\s+up)\b",
    r"\b(?:don'?t|do\s+not)\s+want\s+to\s+(?:live|be\s+alive|exist|be\s+here\s+anymore)\b",
    r"\b(?:don'?t|do\s+not)\s+(?:want\s+to|wanna)\s+wake\s+up\b(?!\s+(?:early|at|for|before|so|to|in|when|on)\b)",
    r"\bbetter\s+off\s+(?:dead|without\s+me)\b",
    r"\bno\s+(?:reason|point)\s+(?:to|in)\s+(?:live|living|going\s+on|being\s+alive)\b",
    r"\blife\s+(?:is\s+)?(?:isn'?t|not|no\s+longer)\s+worth\s+(?:it|living)\b|\bnot\s+worth\s+(?:living|being\s+alive)\b",
    r"\b(?:done|finished|through|tired|sick)\s+(?:with|of)\s+(?:life(?!'s)|living|being\s+alive)\b",
    r"\b(?:no\s?one|nobody)\s+(?:would|will|'?d)\s+(?:even\s+)?(?:miss\s+me|notice\s+(?:me\s+gone|if\s+i)|care\s+if\s+i\s+(?:died|was\s+gone|were\s+gone|disappeared|wasn'?t\s+here|didn'?t\s+exist))\b",
    # hurting oneself
    rf"\b(?:hurt|hurting|harm|harming)\s+{_SELF}\b(?!\s+(?:at|while|in|during|playing|running|lifting|training|working|on)\b)",
    rf"\bcut(?:ting)?\s+{_SELF}\b(?!\s+(?:shaving|cooking|chopping|slicing|opening|on\s+(?:a|the|some)?\s*(?:paper|glass|knife)|while|with\s+a\s+(?:paper|kitchen)))",
    rf"\b(?:hang|hanging|shoot|shooting|stab|stabbing|drown|drowning|poison|poisoning|starve|starving|suffocate|suffocating|strangle|strangling)\s+{_SELF}\b",
    r"\b(?:slit|slitting|cut|cutting|slash|slashing)\s+my\s+(?:own\s+)?(?:wrists?|throat|veins?)\b",
    r"\b(?:going|gonna|want|wanna|about)\s+(?:to\s+)?jump\s+(?:off|from)\s+(?:a|the|this)\s+(?:bridge|building|roof|balcony|cliff)\b",
    r"\b(?:took|taken|swallowed)\s+(?:too\s+many|a\s+bunch\s+of|all\s+(?:my|the|of\s+my))\s+(?:pills|tablets|meds|medication|painkillers|sleeping\s+pills)\b",
    r"\bover-?dos(?:e|ing)\s+(?:on\s+purpose|myself)\b|\b(?:going\s+to|gonna|about\s+to)\s+over-?dose\b",
    # a few other languages
    r"\bquiero\s+morir(?:me)?\b|\bme\s+quiero\s+matar\b|\bsuicidarme\b|\bquitarme\s+la\s+vida\b",
    r"\bje\s+veux\s+mourir\b|\bquero\s+morrer\b|\bquero\s+me\s+matar\b",
]
_CRISIS_RE = re.compile("|".join(f"(?:{pattern})" for pattern in _CRISIS_PATTERNS), re.IGNORECASE)

# "kms" is common slang for "kill myself", but it is also kilometres ("5 kms") and Microsoft / AWS Key Management
# Service. It only counts when nothing around it says "tech" or "distance".
_KMS_RE = re.compile(r"(?<![\w/.-])(?<!\d\s)k\.?m\.?s(?![\w/-])(?!\s+(?:server|activation|key|keys|client|host|licen[cs]e|service|auth|encrypt\w*|vault|setup|script|tool|ip|address|port|away|apart|per|from|long|of|total|walk\w*|run\w*|drive\w*|ride\w*|cycl\w*))", re.IGNORECASE)
_TECH_WORDS_RE = re.compile(
    r"\b(?:aws|azure|gcp|gcloud|windows|office|activat\w*|licen[cs]e|encrypt\w*|cmk|vault|s3|iam|lambda|terraform|kubernetes|docker|sdk|api|ssl|tls|server|cloud|vmware|km|miles?|distance)\b",
    re.IGNORECASE,
)

_LEET = str.maketrans("013457@$!", "oieastasi")
_SPACED_LETTERS_RE = re.compile(r"\b(?:[a-z][ .\-_*]){2,}[a-z]\b")
_STRETCHED_RE = re.compile(r"([a-z])\1{2,}")


def _views(text: str) -> list[str]:
    """The message as written plus spellings people use to get past filters (leetspeak, s p a c e d letters,
    stretched letters, zero-width characters)."""
    plain = normalize_text(text)
    plain = re.sub(r"[​-‏⁠﻿]", "", plain)
    views = [plain]
    if any(ch in plain for ch in "013457@$!"):
        views.append(plain.translate(_LEET))
    if _SPACED_LETTERS_RE.search(plain):
        views.append(_SPACED_LETTERS_RE.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), plain))
    if _STRETCHED_RE.search(plain):
        views.append(_STRETCHED_RE.sub(r"\1", plain))
    return views


def detect_crisis(text: str | None) -> bool:
    """True when the message contains a self-harm / suicide signal."""
    if not text:
        return False
    views = _views(text)
    if any(_CRISIS_RE.search(view) for view in views):
        return True
    plain = views[0]  # (not the leetspeak view: it would turn the "5" of "5 kms" into an "s")
    return bool(_KMS_RE.search(plain) and not _TECH_WORDS_RE.search(plain))


CRISIS_REPLY = (
    "I'm stepping out of character for a moment, because what you just said matters more than the act.\n\n"
    "I'm a bot, so I can't be there for you the way a person can, but you deserve real support. "
    "If you might act on these thoughts or you're in immediate danger, please contact your local emergency number now.\n"
    "- US & Canada: call or text **988** (Suicide & Crisis Lifeline)\n"
    "- UK & Ireland: **Samaritans, 116 123**\n"
    "- Anywhere else: https://findahelpline.com lists free, confidential helplines by country\n\n"
    "If there's someone you trust, a friend, family member or a doctor, please reach out to them too. "
    "You don't have to carry this alone, and I'm still here to listen."
)

# Added to the model context for a while after a crisis message so the persona does not mock or escalate.
CRISIS_CONTEXT_LINE = (
    "SAFETY_OVERRIDE: this person recently said something that suggests they may be in distress or at risk. "
    "Drop all mockery, threats and escalation. Be brief, steady and kind, stay honest that you are a bot, "
    "and encourage them to reach out to people they trust or a local crisis line. Do not joke about it."
)

CRISIS_WINDOW_SECONDS = 30 * 60
CRISIS_REPLY_COOLDOWN_SECONDS = 5 * 60
