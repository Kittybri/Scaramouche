"""Crisis detection and the out-of-character safety reply.

When someone says they may hurt themselves the roleplay stops: the bot answers once with a plain,
caring message and softens the persona for a while afterwards.
"""

from __future__ import annotations

import re

from text_utils import normalize_text

_CRISIS_PATTERNS = [
    r"\bkill(?:ing)?\s+my\s?self\b",
    r"\bsuicid(?:e|al)\b(?!\s+(?:squad|mission|prevention|hotline|bomber|bombers))",
    r"\bend(?:ing)?\s+(?:my\s+(?:own\s+)?life|it\s+all)\b",
    r"\b(?:want|wanna|wish|wished|need)(?:ed)?\s+(?:to|2)\s+(?:die|be\s+dead|disappear\s+forever)\b",
    r"\b(?:don'?t|do\s+not)\s+want\s+to\s+(?:live|be\s+alive|exist|be\s+here\s+anymore)\b",
    r"\bself[\s-]?harm(?:ing)?\b",
    r"\b(?:hurt|hurting|cut|cutting|harm|harming)\s+my\s?self\b",
    r"\bbetter\s+off\s+(?:dead|without\s+me)\b",
    r"\bno\s+(?:reason|point)\s+(?:to|in)\s+(?:live|living|going\s+on|being\s+alive)\b",
]
_CRISIS_RE = re.compile("|".join(f"(?:{pattern})" for pattern in _CRISIS_PATTERNS), re.IGNORECASE)


def detect_crisis(text: str | None) -> bool:
    """True when the message contains a self-harm / suicide signal."""
    return bool(_CRISIS_RE.search(normalize_text(text)))


CRISIS_REPLY = (
    "I'm stepping out of character for a moment, because what you just said matters more than the act.\n\n"
    "I'm a bot, so I can't be there for you the way a person can, but you deserve real support. "
    "If you might act on these thoughts or you're in immediate danger, please contact your local emergency number now.\n"
    "- US: call or text **988** (Suicide & Crisis Lifeline)\n"
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
