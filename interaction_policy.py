"""Deterministic, response-time arbitration. No model, network or persistence writes."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
import re
import time

from awareness_features import SafetyContext, classify_safety, advice_kind
from character_bits import eligible_for_joke

CURRENT = ContextVar("interaction", default=None)
_SERIOUS_GUILDS = {}


def pause_gags(guild_id, *, now=None):
    if guild_id:
        now = time.monotonic() if now is None else now
        _SERIOUS_GUILDS[guild_id] = now + 300


def gags_paused(guild_id, *, now=None):
    now = time.monotonic() if now is None else now
    expiry = _SERIOUS_GUILDS.get(guild_id, 0)
    if expiry <= now:
        _SERIOUS_GUILDS.pop(guild_id, None)
        return False
    return True


class Outcome(str, Enum):
    CONTINUE = "continue"
    CONSUMED = "consumed"
    SUPPRESSED = "suppressed"


OPTIONAL_COMMANDS = frozenset("court wager challenge pranks phantomping muzzle parodyas kidnap slowtrap serverwipe fakewipe popquiz trivia roast insult spar duel arena dare hostage judge stalk blackmail interrogate possess impersonate verdict nightmare argue duet both compare jointinterview vcgame vcparty sound".split())
CONTROL_ACTIONS = frozenset("off stop cancel leave reject restore restore-all disable forget status help unmuzzle".split())


@dataclass
class InteractionContext:
    user_id: int = 0
    channel_id: int = 0
    guild_id: int | None = None
    safety: SafetyContext = field(default_factory=SafetyContext)
    mode: str = "NORMAL"
    command: bool = False
    direct: bool = False
    boundary: bool = False
    opted_out: bool = False
    quiet_hours: bool = False
    muted: bool = False
    joke_eligible: bool = True
    media: bool = False
    prior_last_active: float = 0.0
    session_owner: str = ""
    session: dict | None = None
    user: dict = field(default_factory=dict)
    duo: dict | None = None
    trivia: dict | None = None
    selected_feature: str = ""
    response_path: str = ""
    outcome: Outcome = Outcome.CONTINUE
    suppressed_features: set = field(default_factory=set)

    @property
    def serious(self):
        return self.mode != "NORMAL"

    def allows(self, feature, *, preemptive=False):
        allowed = not (self.serious or self.boundary or self.opted_out or self.quiet_hours
                       or self.muted or self.command or self.session_owner or not self.joke_eligible)
        if preemptive:
            allowed = allowed and not (self.direct or self.media or self.outcome != Outcome.CONTINUE
                                       or (self.selected_feature and self.selected_feature != feature))
        if not allowed:
            self.suppressed_features.add(feature)
        return allowed

    def consume(self, path, *, suppressed=False):
        if self.outcome != Outcome.CONTINUE:
            return False
        self.response_path = path
        self.outcome = Outcome.SUPPRESSED if suppressed else Outcome.CONSUMED
        return True

    def select(self, feature):
        if not self.allows(feature, preemptive=True):
            return False
        self.selected_feature = feature
        return self.consume(feature)

    def debug(self):
        return dict(interaction_mode=self.mode, serious=self.serious, command=self.command,
                    session_owner=self.session_owner, selected_feature=self.selected_feature,
                    suppressed_features_count=len(self.suppressed_features),
                    response_path=self.response_path, interaction_outcome=self.outcome.value)


def classify(text, *, user_id=0, channel_id=0, guild_id=None, user=None, command=False,
             direct=False, quiet_hours=False, muted=False, media=False):
    user = user or {}
    content = (text or "").strip()
    safety = classify_safety(text)
    utility = advice_kind(text, safety=safety) == "factual"
    if safety.crisis or safety.distressed:
        mode = "SERIOUS"
    elif safety.high_stakes:
        mode = "HIGH_STAKES_UTILITY"
    elif not media and (utility or (content and not eligible_for_joke(text) and not command)):
        mode = "UTILITY"
    else:
        mode = "NORMAL"
    boundary = bool(re.search(r"\b(stop (teasing|mocking|joking|insulting|talking)|leave me alone|don't (tease|mock|joke|insult)|do not (tease|mock|joke|insult))\b", text, re.I))
    return InteractionContext(user_id=user_id, channel_id=channel_id, guild_id=guild_id,
                              safety=safety, mode=mode, command=command, direct=direct,
                              boundary=boundary, opted_out=not user.get("proactive", True),
                              quiet_hours=quiet_hours, muted=muted, media=media,
                              joke_eligible=eligible_for_joke(text), user=user)


def current_or_classify(text, user=None, *, user_id=0, channel_id=0, is_dm=False):
    current = CURRENT.get()
    if current and current.user_id == user_id and current.channel_id == channel_id:
        return current
    return classify(text, user=user, user_id=user_id, channel_id=channel_id, direct=is_dm)


def credential_disclosure(text):
    # Narrow disclosure guard, not a general-purpose DLP scanner. Never persist the match.
    return bool(re.search(r"\b(?:password|api[_ -]?key|access[_ -]?token|secret[_ -]?key)\s*(?:=|:|is)\s*[\"']?[^\s\"']{4,}|\bgsk_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----", text, re.I))


def optional_command_blocked(context, name, argument):
    if argument.strip().lower().split(" ", 1)[0] in CONTROL_ACTIONS:
        return False
    return context.serious and (name in OPTIONAL_COMMANDS or name == "chaos" and argument.split(" ", 1)[0] in {"sovereign", "event"})


@dataclass(frozen=True)
class ResolvedCharacterState:
    stance: str
    conflict: str
    relationship: str
    boundary: bool
    grudge: bool
    willingness: str

    def prompt(self):
        return ("RESOLVED_CHARACTER_STATE: " + self.stance + f"; conflict={self.conflict}; "
                f"user-scoped relationship={self.relationship}; boundary={'binding' if self.boundary else 'none'}; "
                f"grudge={'background only' if self.grudge else 'none'}; willingness={self.willingness}. "
                "This interpretation overrides competing mood, self-state, callbacks and comedy. "
                "Permissions and consent always outrank willingness; current need outranks stale callbacks.")


def resolve_character(context, user=None, dimensions=None):
    user, dimensions = user or {}, dimensions or {}
    conflict = "unresolved" if user.get("conflict_open") else "repairing" if user.get("repair_progress", 0) else "settled"
    trust, affection = user.get("trust", 0), user.get("affection", 0)
    relationship = "earned warmth" if trust >= 70 and affection >= 65 else "guarded familiarity" if trust >= 30 else "guarded distance"
    boundary = context.boundary or context.opted_out or context.muted
    if boundary:
        stance, willingness = "respect the user's boundary; no pursuit or intimacy pressure", "boundary-limited"
    elif context.safety.protective:
        stance, willingness = "guarded concern; accuracy and practical help outrank irritation and petty escalation", "useful within permissions"
    elif context.serious:
        stance, willingness = "direct competence; factual accuracy outranks theatrical distraction", "useful within permissions"
    elif context.session_owner:
        stance, willingness = "honor the structured session role; no competing gag", "session-scoped"
    elif conflict == "unresolved":
        stance, willingness = "unresolved conflict shapes guarded cadence, not consent or facts", "guarded"
    elif dimensions.get("concern", 0) >= 6:
        stance, willingness = "reluctant concern with restrained irritation", "attentive"
    else:
        stance, willingness = "Scaramouche's proud, theatrical, layered natural register", "tone only"
    return ResolvedCharacterState(stance, conflict, relationship, boundary,
                                  bool(user.get("grudge_nick") or user.get("grudge_active")), willingness)


def authoritative_prompt(context, user=None, dimensions=None, *, resolved=None):
    """Render the final directive, reusing a response's pre-resolved state."""
    resolved = (resolved or resolve_character(context, user, dimensions)).prompt()
    if context.serious:
        resolved += "\nACCURACY_FIRST: answer the current need directly. No jokes, quizzes, selective hearing, hostile callbacks, dramatic refusals or arbitrary brevity. Do not claim professional expertise."
    return resolved


def optional_allowed(feature, *, preemptive=False):
    context = CURRENT.get()
    return context is None or context.allows(feature, preemptive=preemptive)
