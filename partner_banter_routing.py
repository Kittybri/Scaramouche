"""Audience attribution for optional two-bot banter (no Discord side effects)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


def safe_reference_name(value: str) -> str:
    """Display-only name: not a mention, instruction, or verified relationship."""
    return re.sub(r"[^\w .'-]", "", str(value or ""))[:48].strip()


def jealousy_context(partner_name: str, bystander_name: str) -> str:
    """Keep the jealous joke aimed at the *partner*, not the third person."""
    name = safe_reference_name(bystander_name)
    if not name:
        return ""
    return (
        f"\nJEALOUSY_REFERENCE: {name!r} is a romance-mode person in the channel, "
        f"not the author of this message. PARTNER_SPEAKER: {partner_name}. "
        "Keep your jealous, competitive teasing as sharp as usual; you may "
        "reference or tag the romance-mode person as part of a clearly framed "
        "joke. Do not present that person as the one who made the partner's "
        "statement, and never invent an opinion they did not give."
    )


def coherent_partner_reply(
    text: str, partner_name: str, bystander_name: str = "",
    bystander_mention: str = "",
    *, turn=None,
) -> str:
    """Reject explicit false speech attribution, not ordinary jealousy.

    A romance-mode person may be teased, directly addressed, or mentioned.
    What cannot happen is crediting that person with the partner's opinion.
    This is a conservative guard, not a substitute for conversational evals.
    """
    if turn is not None and (
        turn.speaker_kind != turn.addressee_kind
        or turn.speaker_id != turn.addressee_id
    ):
        return ""
    text = (text or "").strip()
    if not text:
        return ""
    refs = []
    name = safe_reference_name(bystander_name)
    if name:
        refs.append(re.escape(name))
    if re.fullmatch(r"<@!?\d+>", str(bystander_mention or "")):
        refs.append(re.escape(bystander_mention))
    if not refs:
        return text
    person = "(?:" + "|".join(refs) + ")"
    # Only target assertions of opinion or speech, not normal direct teasing.
    false_speech = (
        rf"(?:^|[.!?;]\s*|,\s*)"
        rf"(?:as\s+usual,\s*)?(?:hey\s+)?{person}\s*[,!:]?\s*"
        rf"(?:your\s+(?:worst\s+)?opinion\b|"
        rf"you\s+(?:just\s+)?(?:said|claimed|argued|asked)\b)"
    )
    if re.search(false_speech, text, flags=re.IGNORECASE):
        return ""
    return text


def contextual_romance_tag(text: str, name: str, mention: str) -> str:
    """Use the existing romance ping inside the jealousy joke, not as its addressee.

    Called only after the original 45% romance mention selection.
    """
    text = (text or "").strip()
    if not text or not mention:
        return text
    if mention in text:
        return text
    name = safe_reference_name(name)
    if name:
        hit = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, flags=re.IGNORECASE)
        if hit:
            return text[:hit.start()] + mention + text[hit.end():]
    return text + f" And don't expect {mention} to rescue that argument."


@dataclass(frozen=True)
class TurnEnvelope:
    """Immutable attribution for a bot-to-bot turn; no Discord side effects."""
    source_message_id: int
    channel_id: int
    speaker_id: int
    speaker_kind: str
    addressee_id: int
    addressee_kind: str
    explicit_human_target_ids: frozenset[int] = field(default_factory=frozenset)
    romance_target_id: int | None = None
    mode: str = "rivalry"


def authorized_ping_ids(turn: TurnEnvelope, *, romance_ping_selected: bool = False) -> frozenset[int]:
    """Allow verified interaction targets without suppressing in-character mention text."""
    ids = set(turn.explicit_human_target_ids)
    if turn.speaker_id > 0:
        ids.add(turn.speaker_id)
    if romance_ping_selected and turn.romance_target_id is not None:
        ids.add(turn.romance_target_id)
    return frozenset(uid for uid in ids if isinstance(uid, int) and uid > 0)

async def resolve_duo_reply_anchor(channel, source_message_id: int | None, fallback=None, *, partner_bot_id: int = 0):
    """Use a persisted ID, never the newest unrelated partner message.

    An unavailable or incorrect persisted source means no reply, not a
    misleading attachment to a different conversation.
    """
    if not source_message_id:
        return fallback
    import discord

    try:
        original = await channel.fetch_message(int(source_message_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    if partner_bot_id and int(getattr(getattr(original, "author", None), "id", 0) or 0) != int(partner_bot_id):
        return None
    return original


async def resolve_autoplay_anchor(channel, session, bot_name, partner_name, partner_bot_id, target_message, memory):
    if session.get("mode") in {"interview", "welcome_interview"}:
        return target_message
    source_id = await memory.get_duo_reply_anchor(channel.id, bot_name)
    if source_id:
        return await resolve_duo_reply_anchor(
            channel, source_id, partner_bot_id=partner_bot_id,
        )
    if (session.get("last_speaker") or "").lower() == partner_name.lower():
        return None
    return target_message


def romance_ping_chosen(draw: float) -> bool:
    """The original conditional 45% chance; caller draws only with a target."""
    return float(draw) < 0.45

def authoritative_turn_context(turn: TurnEnvelope) -> str:
    """Source-verified attribution facts, separate from character/persona prose."""
    if turn.speaker_kind != turn.addressee_kind or turn.speaker_id != turn.addressee_id:
        raise ValueError("invalid primary speaker/addressee attribution")
    return (
        "AUTHORITATIVE_DISCORD_TURN: "
        f"message_id={turn.source_message_id}; "
        f"speaker={turn.speaker_kind}; primary_addressee={turn.addressee_kind}; "
        f"romance_reference_is_secondary={turn.romance_target_id is not None}. "
        "Do not attribute the speaker's words to the romance-mode human."
    )
