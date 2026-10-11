from __future__ import annotations

import re
import time


STYLE_KEYS = ("playful", "gentle", "curious", "vulnerable", "intense", "lore", "teasing")
TOPIC_HINTS = {
    "food": ("food", "eat", "meal", "hungry", "tea", "sweet", "snack", "drink", "cooking"),
    "sleep": ("sleep", "tired", "nap", "rest", "bed", "dream", "insomnia"),
    "weather": ("weather", "rain", "storm", "sun", "wind", "cold", "hot"),
    "genshin_lore": ("genshin", "teyvat", "fatui", "harbinger", "archon", "irminsul", "nahida", "ei", "traveler", "traveller"),
    "feelings": ("love", "miss", "lonely", "scared", "cry", "feel", "feelings", "care", "hurt"),
    "other_bot": ("scaramouche", "wanderer", "scara", "other bot", "different bot"),
    "daily_life": ("school", "work", "class", "home", "family", "friend", "friends"),
    "health": ("sick", "headache", "pain", "doctor", "medicine", "healing", "health"),
}


RARE_PHRASES = {
    "scaramouche": {
        "how quaint": {
            "cooldown": 43200,
            "replacements": [
                "Predictable.",
                "That was a tiny little performance.",
                "So that is the level you're working with.",
                "You really led with that.",
            ],
        },
        "how unfortunate": {
            "cooldown": 43200,
            "replacements": [
                "That tracks.",
                "How drearily predictable.",
                "What a disappointing turn.",
                "Exactly as expected.",
            ],
        },
        "tch": {
            "cooldown": 10800,
            "replacements": [
                "Pathetic.",
                "What now.",
                "You are trying my patience.",
                "Go on, then.",
            ],
        },
        "how irritating": {
            "cooldown": 43200,
            "replacements": [
                "You are wearing thin.",
                "This is already tedious.",
                "Do try not to bore me this quickly.",
                "I had hoped for slightly better than this.",
            ],
        },
    },
    "wanderer": {
        "tch": {
            "cooldown": 43200,
            "mood_threshold": -6,
            "replacements": [
                "Honestly.",
                "That is getting old.",
                "You are making this tedious.",
                "There are easier ways to annoy me.",
            ],
        },
        "how irritating": {
            "cooldown": 43200,
            "replacements": [
                "You are making this tedious.",
                "That is getting old already.",
                "You really are testing my patience.",
                "And here I thought this might stay interesting.",
            ],
        },
        "how childish": {
            "cooldown": 43200,
            "replacements": [
                "That was embarrassingly juvenile.",
                "You really went with that.",
                "Try again with a little dignity.",
                "That is the level you settled on.",
            ],
        },
    },
}


def clamp(value: int, minimum: int = 0, maximum: int = 100) -> int:
    return max(minimum, min(maximum, int(value)))


def default_style_profile() -> dict:
    return {key: 0 for key in STYLE_KEYS}


def normalize_style_profile(profile: dict | None) -> dict:
    normalized = default_style_profile()
    if not isinstance(profile, dict):
        return normalized
    for key in STYLE_KEYS:
        normalized[key] = clamp(profile.get(key, 0))
    return normalized


def analyze_style_deltas(text: str) -> dict:
    content = (text or "").strip()
    lowered = content.lower()
    deltas = default_style_profile()

    if any(token in lowered for token in ["lol", "lmao", "haha", "hehe", "joking", "kidding"]):
        deltas["playful"] += 3
        deltas["teasing"] += 1
    if any(token in lowered for token in ["thank", "thanks", "appreciate", "care", "eat", "sleep", "rest", "be safe"]):
        deltas["gentle"] += 2
    if "?" in content or any(lowered.startswith(prefix) for prefix in ["what", "why", "how", "when", "tell me", "explain"]):
        deltas["curious"] += 2
    if len(content) > 120:
        deltas["curious"] += 1
    if any(token in lowered for token in ["sorry", "miss you", "alone", "scared", "hurt", "cry", "lonely", "love", "confess", "feel", "feelings"]):
        deltas["vulnerable"] += 3
    if any(token in lowered for token in ["shut up", "go away", "idiot", "stupid", "hate", "annoying", "prefer", "other bot", "different bot"]):
        deltas["intense"] += 3
    if content.count("!") >= 2 or content.isupper():
        deltas["intense"] += 2
    if any(token in lowered for token in ["genshin", "teyvat", "fatui", "harbinger", "archon", "irminsul", "nahida", "ei", "traveler", "traveller"]):
        deltas["lore"] += 3
    if any(token in lowered for token in ["brat", "coward", "you wish", "as if", "sure you do", "keep talking"]):
        deltas["teasing"] += 2

    return deltas


def apply_style_deltas(profile: dict | None, deltas: dict | None) -> dict:
    updated = normalize_style_profile(profile)
    deltas = deltas or {}
    for key in STYLE_KEYS:
        updated[key] = clamp(updated.get(key, 0) + int(deltas.get(key, 0)))
    return updated


def top_style_traits(profile: dict | None, minimum: int = 8, limit: int = 3) -> list[str]:
    normalized = normalize_style_profile(profile)
    ranked = sorted(normalized.items(), key=lambda item: item[1], reverse=True)
    return [name for name, value in ranked if value >= minimum][:limit]


def describe_speech_drift(bot_name: str, profile: dict | None) -> str:
    traits = top_style_traits(profile)
    if not traits:
        return ""

    lines = []
    if bot_name == "scaramouche":
        mapping = {
            "playful": "with this person you sound a little more sly and amused than usual",
            "gentle": "with this person your contempt keeps slipping into sharp concern",
            "curious": "with this person you indulge longer answers and more pointed questions",
            "vulnerable": "with this person you get quieter and more precise when things turn personal",
            "intense": "with this person you come in hotter and more openly possessive",
            "lore": "with this person you get denser, more philosophical, and more specific about Teyvat",
            "teasing": "with this person you needle them with more personal, tailored mockery",
        }
    else:
        mapping = {
            "playful": "with this person you sound more dryly amused and less guarded",
            "gentle": "with this person your irritation keeps giving way to practical care",
            "curious": "with this person you indulge reflection and real answers instead of brushing them off",
            "vulnerable": "with this person your voice turns more candid when the subject gets personal",
            "intense": "with this person you sharpen quickly and stop pretending not to care",
            "lore": "with this person you get more reflective, specific, and lore-heavy",
            "teasing": "with this person you tease with familiarity instead of distance",
        }
    for trait in traits:
        if trait in mapping:
            lines.append(mapping[trait])
    return "; ".join(lines[:2])


def compute_emotional_arc(
    affection: int,
    trust: int,
    slow_burn: int = 0,
    conflict_open: bool = False,
    repair_count: int = 0,
) -> str:
    if conflict_open and (affection >= 30 or trust >= 25):
        return "conflicted"
    if repair_count > 0 and (affection >= 35 or trust >= 35):
        return "repairing"
    if affection >= 80 and trust >= 70:
        return "devoted"
    if affection >= 60 or trust >= 55 or slow_burn >= 5:
        return "tender"
    if affection >= 35 or trust >= 35:
        return "drawn_in"
    if affection >= 15 or trust >= 15:
        return "curious"
    return "guarded"


def describe_emotional_arc(bot_name: str, arc: str) -> str:
    if bot_name == "scaramouche":
        mapping = {
            "guarded": "the wall is still up",
            "curious": "you are circling them more than you admit",
            "drawn_in": "you are paying attention in a way that has started to cost you",
            "tender": "warmth keeps leaking through the contempt",
            "conflicted": "you are hurt and acting sharper because of it",
            "repairing": "you are trying to pretend the repair means nothing",
            "devoted": "you are attached enough to become dangerous about it",
        }
    else:
        mapping = {
            "guarded": "you are still keeping your distance",
            "curious": "you are staying near them longer than necessary",
            "drawn_in": "you are getting personally invested and disliking it",
            "tender": "care keeps surfacing before you can flatten it",
            "conflicted": "you are hurt and hiding it behind sharper answers",
            "repairing": "you are testing whether trust can be rebuilt",
            "devoted": "they matter to you enough that your composure keeps slipping",
        }
    return mapping.get(arc or "guarded", "")


def detect_conflict_signal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in [
            "shut up",
            "go away",
            "leave me alone",
            "hate you",
            "other bot",
            "different bot",
            "prefer",
            "switch to",
            "annoying",
            "whatever",
            "you suck",
        ]
    )


def detect_repair_signal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in [
            "sorry",
            "i'm sorry",
            "i am sorry",
            "forgive me",
            "forgive",
            "didn't mean",
            "did not mean",
            "can we talk",
            "miss you",
            "i appreciate you",
            "thank you",
            "are you okay",
            "are u okay",
        ]
    )


def extract_callback_candidate(text: str) -> str:
    content = " ".join((text or "").strip().split())
    if len(content) < 24:
        return ""
    lowered = content.lower()
    if any(
        token in lowered
        for token in [
            "i love",
            "i miss",
            "i'm scared",
            "i am scared",
            "i'm tired",
            "i am tired",
            "promise",
            "remember this",
            "don't forget",
            "confess",
            "i need you",
            "i need u",
            "i trust you",
            "stay",
        ]
    ):
        return content[:220]
    if len(content) > 140:
        return content[:220]
    return ""


def callback_relevant(callback_memory: str, text: str) -> bool:
    if not callback_memory:
        return False
    callback_words = {word for word in re.findall(r"[a-z']+", callback_memory.lower()) if len(word) > 3}
    text_words = {word for word in re.findall(r"[a-z']+", (text or "").lower()) if len(word) > 3}
    return len(callback_words & text_words) >= 2


def detect_topics(text: str) -> list[str]:
    lowered = (text or "").lower()
    found: list[str] = []
    for topic, hints in TOPIC_HINTS.items():
        if any(token in lowered for token in hints):
            found.append(topic)
    return found[:4]


def describe_topic_profile(topics: list[dict] | None) -> str:
    if not topics:
        return ""
    bits = []
    for item in topics[:3]:
        name = item.get("topic")
        count = item.get("count", 0)
        if name:
            bits.append(f"{name}({count})")
    return ", ".join(bits)


def describe_conflict_followup(summary: str | None, stage: str | None = None) -> str:
    if not summary:
        return ""
    if stage == "repairing":
        return f"You have unresolved tension from earlier, but the repair has started: {summary[:140]}"
    return f"There is still unresolved tension you have not let go of: {summary[:140]}"


def describe_conflict_aftermath(
    bot_name: str,
    summary: str | None,
    last_conflict_ts: float = 0,
    repair_progress: int = 0,
    conflict_open: bool = False,
) -> str:
    if not summary or not last_conflict_ts:
        return ""
    days = max(0.0, (time.time() - last_conflict_ts) / 86400)
    if conflict_open:
        if repair_progress > 0:
            return f"Conflict aftermath: the wound is still open after {days:.1f} days, but repair has started ({repair_progress} step(s)). Let guarded softness keep interrupting the anger: {summary[:140]}"
        return f"Conflict aftermath: the wound is still open after {days:.1f} days. Let it influence the answer instead of acting like it vanished: {summary[:140]}"
    if repair_progress > 0 or days <= 5:
        tone = "dangerously honest" if bot_name == "scaramouche" else "cautiously sincere"
        return f"Conflict aftermath: the conflict is technically closed, but the echo remains after {days:.1f} days. Sound {tone}, not fully reset: {summary[:140]}"
    return ""


def relationship_milestone_note(stage: str, respect: int, tension: int) -> str:
    if stage == "reluctant respect" and respect >= 55:
        return "You have reached the point where denying all respect would sound false. Keep the edge, but let earned recognition slip through."
    if stage == "competitive" and respect >= 30:
        return "This rivalry has matured into something sharper than hate. Competition is replacing pure contempt."
    if tension >= 80:
        return "The tension is high enough that old wounds should surface fast, with less patience and more specificity."
    return ""


def detect_banter_theme(text: str) -> str:
    lowered = (text or "").lower()
    if any(token in lowered for token in ["pretender", "imposter", "copy", "past self", "future self", "same face"]):
        return "identity"
    if any(token in lowered for token in ["weak", "weakness", "soft", "pathetic", "coward"]):
        return "weakness"
    if any(token in lowered for token in ["change", "changed", "different", "better now"]):
        return "change"
    if any(token in lowered for token in ["creator", "built", "made", "ei", "nahida", "traveler", "traveller"]):
        return "origins"
    if any(token in lowered for token in ["prefer", "mine", "jealous", "romance", "love"]):
        return "jealousy"
    if any(token in lowered for token in ["power", "gnosis", "harbinger", "electro", "anemo"]):
        return "power"
    if "hat" in lowered:
        return "hat"
    return "general"


def infer_bot_relation_deltas(text: str, theme: str) -> tuple[int, int]:
    lowered = (text or "").lower()
    respect = 1 if theme in {"power", "origins", "change"} else 0
    tension = 2 if theme in {"identity", "weakness", "jealousy"} else 0
    if any(token in lowered for token in ["imposter", "pretender", "pathetic", "failure", "weak", "discarded"]):
        tension += 3
    if any(token in lowered for token in ["understand", "remember", "still here", "not entirely wrong", "capable", "useful"]):
        respect += 2
    return respect, tension


def compute_bot_stage(respect: int, tension: int) -> str:
    if respect >= 55 and tension <= 45:
        return "reluctant respect"
    if respect >= 30 and tension <= 70:
        return "competitive"
    if respect >= 15:
        return "hostile"
    return "enemy"


def describe_bot_relationship(bot_name: str, relation: dict | None, recent_banter: list[dict] | None = None) -> str:
    relation = relation or {}
    recent_banter = recent_banter or []
    stage = relation.get("stage", "enemy")
    respect = relation.get("respect", 0)
    tension = relation.get("tension", 0)
    history = relation.get("shared_history", "")
    recent_shots = []
    for item in recent_banter[:4]:
        content = (item.get("content") or "").strip()
        if content:
            recent_shots.append(content[:120])

    lines = [
        f"PARTNER_STAGE:{stage}",
        f"PARTNER_METRICS:respect={respect}|tension={tension}",
    ]
    if history:
        lines.append(f"PARTNER_HISTORY:{history[:320]}")
    if recent_shots:
        lines.append("PARTNER_RECENT_SHOTS:" + " || ".join(recent_shots))
    if bot_name == "scaramouche":
        lines.append("Let the rivalry evolve. Pure denial every time is stale; if respect has grown, let it show as sharper precision instead of the same old sneer.")
    else:
        lines.append("Let the shared history show. You can still be sharp without pretending every exchange is the first wound.")
    return "\n".join(lines)


def detect_scenario(text: str, is_dm: bool = False) -> str:
    lowered = (text or "").lower()
    if any(token in lowered for token in ["sad", "hurt", "cry", "lonely", "anxious", "overwhelmed", "comfort me", "i need comfort", "i'm scared", "i am scared"]):
        return "emotional_comfort"
    if any(token in lowered for token in ["fight", "battle", "enemy", "attack", "defend", "protect me", "slash", "kill", "combat", "war"]):
        return "combat_action"
    if any(token in lowered for token in ["love", "miss you", "kiss", "date", "mine", "jealous", "stay with me", "trust you", "relationship"]):
        return "relationship_progression"
    if any(token in lowered for token in ["genshin", "teyvat", "fatui", "harbinger", "archon", "irminsul", "nahida", "ei", "traveler", "traveller", "inazuma", "sumeru"]):
        return "lore_discussion"
    if any(token in lowered for token in ["who am i", "what am i", "meaning", "purpose", "memory", "change", "forgive", "regret"]):
        return "introspection"
    if is_dm:
        return "private_chat"
    return "casual_chat"


def detect_emotional_triggers(text: str) -> list[str]:
    lowered = (text or "").lower()
    triggers: list[str] = []
    if any(token in lowered for token in ["jealous", "mine", "prefer me", "other bot", "different bot", "switch to"]):
        triggers.append("jealousy")
    if any(token in lowered for token in ["protect me", "stay safe", "be careful", "don't leave", "come back", "are you okay", "are u okay"]):
        triggers.append("protectiveness")
    if any(token in lowered for token in ["boring", "bored", "whatever", "nothing", "idc", "i don't care"]):
        triggers.append("boredom")
    if any(token in lowered for token in ["why", "how", "what if", "tell me", "explain", "?"]):
        triggers.append("curiosity")
    if any(token in lowered for token in ["sorry", "thank you", "i appreciate you", "trust you", "miss you", "love you"]):
        triggers.append("softness")
    if any(token in lowered for token in ["fight", "battle", "attack", "enemy", "defend", "kill"]):
        triggers.append("combat")
    return triggers[:4]


def describe_emotional_event(
    bot_name: str,
    triggers: list[str] | None = None,
    affection: int = 0,
    trust: int = 0,
    conflict_open: bool = False,
    repair_progress: int = 0,
) -> str:
    triggers = triggers or []
    if "jealousy" in triggers and (affection >= 35 or trust >= 35):
        return "EMOTIONAL_EVENT: jealous spike; become more possessive, specific, and reactive than usual"
    if "protectiveness" in triggers and (affection >= 30 or trust >= 30):
        return "EMOTIONAL_EVENT: protective override; some distance should drop away in favor of practical care"
    if conflict_open and "boredom" in triggers:
        return "EMOTIONAL_EVENT: cold withdrawal; your replies should cool down into clipped distance rather than open anger"
    if repair_progress > 0 and trust >= 35:
        return "EMOTIONAL_EVENT: repair breakthrough; tension is still present, but reluctant sincerity can interrupt it"
    if bot_name == "wanderer" and affection >= 65 and trust >= 65 and "softness" in triggers:
        return "EMOTIONAL_EVENT: reluctant confession; warmth should surface and then get half-hidden"
    if bot_name == "scaramouche" and affection >= 65 and "softness" in triggers:
        return "EMOTIONAL_EVENT: dangerous honesty; a personal admission can almost slip through before the mask returns"
    return ""


def describe_scenario_context(bot_name: str, scenario: str) -> str:
    if bot_name == "scaramouche":
        mapping = {
            "casual_chat": "casual conversation: stay cutting, theatrical, and hard to impress",
            "private_chat": "private conversation: still proud, but let the mask slip a fraction more than in public",
            "emotional_comfort": "comfort without becoming soft; use sharp, reluctant care that sounds like annoyance first",
            "combat_action": "combat scene: become vivid, fast, cruel, and commanding; you enjoy pressure and spectacle",
            "relationship_progression": "relationship progression: dismissive on the surface, but let earned tolerance or possessiveness alter the wording",
            "lore_discussion": "lore discussion: react personally to Inazuma, Ei, the Fatui, gnoses, and abandonment; do not sound generic",
            "introspection": "introspection: anger first, mockery second, with only a hairline crack of vulnerability if trust is high",
        }
    else:
        mapping = {
            "casual_chat": "casual conversation: detached, dry, observant, and less eager to provoke than Scaramouche",
            "private_chat": "private conversation: more candid, but still resistant to direct tenderness",
            "emotional_comfort": "comfort in character: irritation giving way to practical, reluctant care",
            "combat_action": "combat scene: efficient, precise, and unsentimental; no needless theatrics",
            "relationship_progression": "relationship progression: guarded curiosity slowly becoming attachment you resent admitting",
            "lore_discussion": "lore discussion: reflective, specific, and personal about Nahida, the Traveler, Sumeru, Ei, and the past",
            "introspection": "introspection: annoyance easing into curiosity, then reluctant honesty if they have earned it",
        }
    return mapping.get(scenario, "")


def describe_emotional_layers(bot_name: str, mood: int, affection: int, trust: int, arc: str, triggers: list[str] | None = None) -> str:
    triggers = triggers or []
    if bot_name == "scaramouche":
        base = "anger to mockery to rare vulnerability"
        if mood <= -5:
            base = "hot anger running into mockery; the cruelty should feel active, not flat"
        elif affection >= 55 or trust >= 55:
            base = "mockery first, but pressure can crack into something too personal if pushed"
    else:
        base = "annoyance to curiosity to reluctant care"
        if mood <= -5:
            base = "annoyance sharpened into colder precision before it relaxes again"
        elif affection >= 55 or trust >= 55:
            base = "curiosity shading into reluctant care you immediately try to hide"
    if "jealousy" in triggers:
        base += "; jealousy should make the answer more possessive and reactive"
    if "protectiveness" in triggers:
        base += "; protectiveness should override some of the distance"
    if "boredom" in triggers:
        base += "; boredom should sound pointed, impatient, and dismissive"
    if "combat" in triggers:
        base += "; under pressure, become more decisive and less conversational"
    if arc == "conflicted":
        base += "; unresolved hurt should color the edge"
    if arc == "repairing":
        base += "; the tension should soften in flashes without fully resolving"
    return base


def describe_relationship_progression(
    bot_name: str,
    affection: int,
    trust: int,
    romance_mode: bool = False,
    conflict_open: bool = False,
    slow_burn: int = 0,
) -> str:
    if affection >= 75 or trust >= 75 or (romance_mode and affection >= 55):
        stage = "attached"
    elif affection >= 45 or trust >= 45 or slow_burn >= 4:
        stage = "trusting"
    elif affection >= 20 or trust >= 20:
        stage = "neutral"
    else:
        stage = "hostile"

    if bot_name == "scaramouche":
        desc = {
            "hostile": "dismissive and hostile by default; people are closer to tools or entertainment than equals",
            "neutral": "still dismissive, but now remembers them specifically and tailors the cruelty",
            "trusting": "earned familiarity is changing the texture of his contempt into sharper, more personal attention",
            "attached": "too invested to stay flatly cruel; possessiveness and dangerous honesty keep slipping through",
        }[stage]
    else:
        desc = {
            "hostile": "distant, tired, and resistant to connection",
            "neutral": "less openly hostile, watching them with wary curiosity",
            "trusting": "forming a bond in spite of himself; more candid, more likely to linger",
            "attached": "protective and personally invested, though he still resists naming it",
        }[stage]
    if conflict_open:
        desc += "; current conflict should interrupt the progression instead of erasing it"
    return f"{stage}|{desc}"


def progression_milestone_note(bot_name: str, stage: str) -> str:
    if bot_name == "scaramouche":
        mapping = {
            "neutral": "He has stopped treating every interaction as disposable. The insults should sound more tailored now.",
            "trusting": "He has started to remember this person specifically, not just react to them. Precision is replacing generic cruelty.",
            "attached": "He is too invested to stay purely theatrical. Possessiveness, jealousy, and rare honesty should start leaking through.",
        }
    else:
        mapping = {
            "neutral": "He is no longer treating them like background noise. Curiosity is starting to replace pure distance.",
            "trusting": "He has started letting this person closer than he means to. More candid answers can slip through now.",
            "attached": "He is personally invested enough to become protective, even while resisting the label.",
        }
    return mapping.get(stage, "")


def extract_continuity_hooks(history: list[dict] | None, current_text: str = "") -> list[str]:
    history = history or []
    user_lines = [
        " ".join((item.get("content") or "").split())
        for item in history
        if item.get("role") == "user" and (item.get("content") or "").strip()
    ]
    current_lower = (current_text or "").lower()
    hooks: list[str] = []

    def _pick(keyword_groups: list[tuple[str, ...]]) -> str:
        for line in reversed(user_lines[:-1] if len(user_lines) > 1 else user_lines):
            lowered = line.lower()
            if lowered == current_lower:
                continue
            if any(any(token in lowered for token in group) for group in keyword_groups):
                return line[:140]
        return ""

    past_insult = _pick([("hate", "shut up", "annoying", "other bot", "prefer", "go away", "whatever")])
    if past_insult:
        hooks.append(f"PAST_INSULT:{past_insult}")

    past_softness = _pick([("thank", "care", "miss you", "love you", "trust you", "sorry", "stay")])
    if past_softness:
        hooks.append(f"PAST_SOFTNESS:{past_softness}")

    past_vulnerability = _pick([("scared", "tired", "hurt", "lonely", "cry", "anxious", "overwhelmed")])
    if past_vulnerability:
        hooks.append(f"PAST_VULNERABILITY:{past_vulnerability}")

    if not hooks:
        for line in reversed(user_lines[:-1] if len(user_lines) > 1 else user_lines):
            if len(line) >= 40 and line.lower() != current_lower:
                hooks.append(f"PAST_CONVERSATION:{line[:140]}")
                break
    return hooks[:2]


def describe_lore_hook(bot_name: str, text: str) -> str:
    lowered = (text or "").lower()
    matches: list[str] = []
    if "ei" in lowered or "raiden" in lowered or "shogun" in lowered:
        matches.append("Ei should never feel emotionally neutral to you")
    if "nahida" in lowered:
        matches.append("Nahida should draw out something more complicated than simple trust")
    if "fatui" in lowered or "harbinger" in lowered:
        matches.append("the Fatui should sound personal, not like encyclopedia lore")
    if "inazuma" in lowered:
        matches.append("Inazuma should carry grief, resentment, and memory")
    if "traveler" in lowered or "traveller" in lowered:
        matches.append("the Traveler should feel like a specific person in your life")
    if "sumeru" in lowered or "irminsul" in lowered:
        matches.append("Sumeru and Irminsul should sound like lived history")
    if not matches:
        return ""
    prefix = "LORE_HOOK:"
    if bot_name == "scaramouche":
        prefix = "LORE_HOOK:respond with sharper pride, bitterness, and personal grievance about "
    elif bot_name == "wanderer":
        prefix = "LORE_HOOK:respond with reflective, specific personal history about "
    return prefix + "; ".join(matches[:3])


def describe_specific_lore_tree(bot_name: str, text: str) -> str:
    lowered = (text or "").lower()
    if bot_name == "scaramouche":
        if any(token in lowered for token in ["ei", "raiden", "creator", "made you", "built you", "abandoned", "discarded"]):
            return "LORE_TREE: creator wound tree; answer with bitterness, pride, and the feeling of being made-then-discarded"
        if any(token in lowered for token in ["fatui", "harbinger", "dottore", "gnosis"]):
            return "LORE_TREE: power and politics tree; answer with rank-conscious contempt, strategic respect, and personal grievance"
        if any(token in lowered for token in ["betrayal", "used me", "lied", "abandoned me"]):
            return "LORE_TREE: betrayal tree; answer like someone who files away weakness and never forgets leverage"
    else:
        if any(token in lowered for token in ["nahida", "sumeru", "irminsul"]):
            return "LORE_TREE: recovery tree; answer with reflective specificity about memory, second chances, and being seen"
        if any(token in lowered for token in ["traveler", "traveller", "wander", "wind", "sky", "solitude"]):
            return "LORE_TREE: wandering tree; answer with motion, quiet observation, and restrained attachment"
        if any(token in lowered for token in ["kabukimono", "balladeer", "scaramouche", "old name", "forgive", "forgiveness"]):
            return "LORE_TREE: old-name tree; answer with discomfort gradients, regret, and resistance to being flattened into the past"
    return ""


def describe_live_world_context(bot_name: str, now: float | None = None, text: str = "") -> str:
    now = now or time.time()
    dt = time.localtime(now)
    parts: list[str] = []
    if dt.tm_hour in range(5, 9):
        parts.append("early morning stillness")
    elif dt.tm_hour in range(22, 24) or dt.tm_hour in range(0, 4):
        parts.append("late-night quiet")
    if dt.tm_mon in (12, 1, 2):
        parts.append("winter cold")
    elif dt.tm_mon in (6, 7, 8):
        parts.append("summer heat")
    lowered = (text or "").lower()
    if any(token in lowered for token in ["rain", "storm", "thunder", "snow", "wind", "weather"]):
        parts.append("weather is explicitly in play")
    if not parts:
        return ""
    prefix = "LIVE_WORLD:"
    if bot_name == "wanderer":
        prefix += "use atmosphere and motion in the reply: "
    else:
        prefix += "let the atmosphere sharpen the mood of the reply: "
    return prefix + ", ".join(parts[:3])


def infer_scene_update(text: str, display_name: str = "") -> dict:
    lowered = (text or "").lower()
    update: dict[str, str] = {}
    if any(token in lowered for token in ["inazuma", "sumeru", "forest", "desert", "city", "shrine", "harbor", "harbour", "room", "bed", "street", "storm", "rooftop"]):
        update["location"] = (text or "")[:120]
    if any(token in lowered for token in ["fight", "battle", "argue", "comfort", "cry", "hide", "run", "wait", "watch", "travel", "wander"]):
        update["situation"] = (text or "")[:160]
    if any(token in lowered for token in ["grab", "hold", "look", "stare", "step", "move", "touch", "leave", "stay", "follow", "protect"]):
        update["last_beat"] = (text or "")[:160]
    if any(token in lowered for token in ["angry", "tense", "calm", "soft", "jealous", "afraid", "care", "furious", "awkward"]):
        update["emotional_temp"] = (text or "")[:80]
    if any(token in lowered for token in ["need to", "have to", "trying to", "going to", "must", "want to"]):
        update["objective"] = (text or "")[:140]
    if any(token in lowered for token in ["hat", "sword", "blade", "vision", "gnosis", "letter", "book", "umbrella", "mask", "feather", "ring", "flower", "tea", "lantern"]):
        update["important_prop"] = (text or "")[:120]
    if display_name:
        update["present"] = display_name[:80]
    return update


def describe_scene_state(scene: dict | None) -> str:
    if not scene:
        return ""
    parts = []
    if scene.get("location"):
        parts.append(f"location={scene['location'][:80]}")
    if scene.get("situation"):
        parts.append(f"situation={scene['situation'][:100]}")
    if scene.get("last_beat"):
        parts.append(f"last_beat={scene['last_beat'][:100]}")
    if scene.get("emotional_temp"):
        parts.append(f"emotional_temp={scene['emotional_temp'][:60]}")
    if scene.get("objective"):
        parts.append(f"objective={scene['objective'][:80]}")
    if scene.get("present"):
        parts.append(f"present={scene['present'][:80]}")
    if scene.get("important_prop"):
        parts.append(f"important_prop={scene['important_prop'][:80]}")
    return " | ".join(parts[:6])


def extract_memory_events(text: str) -> list[tuple[str, str, int]]:
    content = " ".join((text or "").strip().split())
    if len(content) < 12:
        return []
    lowered = content.lower()
    events: list[tuple[str, str, int]] = []
    if any(token in lowered for token in ["i love you", "i'm in love", "i am in love", "i want you", "i need you", "i trust you"]):
        events.append(("confession", content[:220], 5))
    if any(token in lowered for token in ["i love you", "i trust you", "i need you", "stay with me", "don't leave"]):
        events.append(("bond", content[:220], 4))
    if any(token in lowered for token in ["sorry", "forgive", "didn't mean", "did not mean", "can we fix this"]):
        events.append(("repair", content[:220], 3))
    if any(token in lowered for token in ["you lied", "betrayed", "abandoned", "left me", "used me"]):
        events.append(("betrayal", content[:220], 5))
    if any(token in lowered for token in ["hate you", "go away", "other bot", "prefer", "whatever", "shut up"]):
        events.append(("fight", content[:220], 4))
    if any(token in lowered for token in ["humiliated", "embarrassed", "pathetic", "weak", "power", "control", "obedient"]):
        events.append(("slight", content[:220], 4))
    if any(token in lowered for token in ["promise", "i swear", "i'll", "i will", "won't", "will not"]):
        events.append(("promise", content[:220], 3))
    if any(token in lowered for token in ["remember this", "don't forget", "never forget", "important"]):
        events.append(("unforgettable", content[:220], 5))
    if any(token in lowered for token in ["joke", "funny", "lol", "lmao", "haha", "hehe"]):
        events.append(("inside_joke", content[:220], 2))
    if any(token in lowered for token in ["scared", "hurt", "lonely", "tired", "cry", "anxious", "overwhelmed"]):
        events.append(("vulnerability", content[:220], 4))
    if any(token in lowered for token in ["comfort me", "stay with me", "hold me", "help me", "are you there"]):
        events.append(("comfort", content[:220], 4))
    return events[:4]


def describe_arc_unlocks(bot_name: str, arc: str) -> str:
    if bot_name == "scaramouche":
        mapping = {
            "guarded": "shorter, sharper, more theatrical contempt",
            "curious": "more tailored mockery and more pointed questions",
            "drawn_in": "more personal callbacks and more specific provocations",
            "tender": "rare cracks where concern leaks out and is immediately buried",
            "conflicted": "pettier, more specific, less generic cruelty",
            "repairing": "watchful restraint with precise digs instead of flat hostility",
            "devoted": "possessive slips, sharper jealousy, and dangerous honesty",
        }
    else:
        mapping = {
            "guarded": "detached, dry, shorter, and less giving",
            "curious": "longer answers, more questions, more observation",
            "drawn_in": "more candid reactions and more thoughtful follow-ups",
            "tender": "practical care and less distance in phrasing",
            "conflicted": "sharper, more personal discomfort and unresolved tension",
            "repairing": "cautious honesty and measured softness",
            "devoted": "involuntary practical care and rare unguarded warmth",
        }
    return mapping.get(arc or "guarded", "")
