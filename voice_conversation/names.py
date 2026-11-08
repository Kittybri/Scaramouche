"""How a bot recognises its own name, and its partner's, in speech-recognition text. Shared by both bots.

Whisper spells the names many ways ("Scaramouch", "Scara Mouche", "Wonderer"), and people put a name at the start
("Wanderer, ...") or at the end ("..., Wanderer?"). Exact, opening-only matching made one bot ignore lines meant
for it while the other bot answered them, and spoken leave only worked for one exact sentence. Pure functions,
no Discord imports.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

FILLERS = frozenset({"hey", "okay", "ok", "um", "uh", "please", "hi", "hello", "yo", "oh"})

_NAMES = {
    "scaramouche": {
        "canonical": "scaramouche",
        "aliases": frozenset({"scaramouche", "scara", "balladeer"}),
        # common speech-recognition spellings
        "heard": frozenset({
            "scaramouch", "scaramoosh", "scaramooch", "scaramush", "scaramuche", "scaramuccia", "skaramouche",
            "scaramouse", "scaramoushe", "scarramouche", "scaramoose", "skara",
        }),
        "opening_only": frozenset(),
    },
    "wanderer": {
        "canonical": "wanderer",
        "aliases": frozenset({"wanderer", "hatguy"}),
        "heard": frozenset({"wonderer", "wandera", "wondera", "wanderor", "wonderor"}),
        # short forms that are ordinary words elsewhere: only as an opening address
        "opening_only": frozenset({"wander", "wanda"}),
    },
}
# "Wander through the woods" is the verb, not the shortened name.
_WANDER_VERB_NEXT = frozenset({
    "through", "around", "about", "into", "across", "off", "over", "along", "back", "away", "up", "out", "down", "to",
})


def words(text: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("’", "'"))


def partner_of(bot: str) -> str:
    return "wanderer" if bot.lower() == "scaramouche" else "scaramouche"


def _spec(bot: str) -> dict:
    bot = bot.lower()
    return _NAMES.get(bot) or {"canonical": bot, "aliases": frozenset({bot}), "heard": frozenset(),
                               "opening_only": frozenset()}


def _word_is(word: str, bot: str, *, joined: bool = False) -> bool:
    spec = _spec(bot)
    if word in spec["aliases"] or word in spec["heard"]:
        return True
    canonical = spec["canonical"]
    if len(word) < 7 or word[0] != canonical[0]:
        return False
    if joined:
        # a name split into pieces ("scara mouche", "scare a mouche") must still be about the name's length,
        # so that "wander over" (the verb and a preposition) never turns into "Wanderer"
        return abs(len(word) - len(canonical)) <= 1 and SequenceMatcher(None, word, canonical).ratio() >= 0.85
    return SequenceMatcher(None, word, canonical).ratio() >= 0.78


def _name_span(tokens: list[str], bot: str, *, opening: bool) -> int:
    """How many leading tokens spell ``bot``'s name (a split name like "scara mouche" uses two); 0 if none."""
    spec = _spec(bot)
    for width in (1, 2, 3):
        if len(tokens) < width:
            break
        if _word_is("".join(tokens[:width]), bot, joined=width > 1):
            return width
    if opening and tokens and tokens[0] in spec["opening_only"]:
        if tokens[0] == "wander" and len(tokens) > 1 and tokens[1] in _WANDER_VERB_NEXT:
            return 0
        return 1
    return 0


def _skip_fillers(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens) and tokens[i] in FILLERS:
        i += 1
    return tokens[i:]


def addressed_at_opening(text: str | None, bot: str) -> bool:
    """The sentence opens with the name, after fillers: "Hey Scara, ...", "Wander, are you there?"."""
    return bool(_name_span(_skip_fillers(words(text)), bot, opening=True))


def addressed_at_end(text: str | None, bot: str) -> bool:
    """The name closes the sentence after a comma: "Can you hear me, Wanderer?"."""
    match = re.search(r",\s*([^,]{2,40}?)[\s.!?]*$", (text or "").lower())
    if not match:
        return False
    tail = _skip_fillers(words(match.group(1)))
    return bool(tail) and _name_span(tail, bot, opening=False) == len(tail)


def addressed(text: str | None, bot: str) -> bool:
    """Spoken TO ``bot`` (name first, or name last after a comma), as opposed to merely talking about it."""
    return addressed_at_opening(text, bot) or addressed_at_end(text, bot)


def mentions(text: str | None, bot: str) -> bool:
    """The name anywhere; the short forms ("wander", "wanda") only count as an opening address."""
    if addressed_at_opening(text, bot):
        return True
    tokens = words(text)
    return any(_name_span(tokens[i:], bot, opening=False) for i in range(len(tokens)))


_LEAVE = re.compile(
    r"(?:(?:can|could|will|would) you |you (?:can|may) )?(?:please )?(?:just )?"
    r"(?:leave|exit|disconnect(?: from)?|get out(?: of)?|stop listening)"
    r"(?: (?:the|this|our))?(?: (?:voice|vc))?(?: (?:channel|chat|call|room))?(?: here)?(?: (?:now|please|already))*"
)
_TARGET = frozenset({"voice", "vc", "channel", "chat", "call", "room", "here"})


def leave_intent(text: str | None, bot: str) -> bool:
    """A short spoken request for ``bot`` to leave the voice channel.

    "Scaramouche, leave the voice chat", "Scara, can you leave the call?", "get out of the VC, Wanderer",
    and a plain "leave the voice channel" (which every bot in the channel hears and obeys). Not "don't leave",
    questions about leaving, a request aimed only at the other bot, or "leave it/me alone".
    """
    raw = (text or "").lower().strip()
    if not raw or len(raw) > 140:
        return False
    # Only the bot's own name, fillers and the request itself may be there: anything else (a negation, a question
    # word, the other bot's name, "me alone") leaves words over and the request does not match.
    tokens = _skip_fillers(words(raw))
    named = False
    span = _name_span(tokens, bot, opening=True)
    if span:
        tokens, named = _skip_fillers(tokens[span:]), True
    for width in (3, 2, 1):  # "... leave the voice chat, Scaramouche"
        if len(tokens) > width and _name_span(tokens[-width:], bot, opening=False) == width:
            tokens, named = tokens[:-width], True
            break
    phrase = " ".join(tokens)
    if not _LEAVE.fullmatch(phrase):
        return False
    # A bare "leave" must name the bot; with a target ("the voice chat", "the call") everyone it reaches obeys.
    return named or any(token in _TARGET for token in tokens)
