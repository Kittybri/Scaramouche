"""Scaramouche Discord bot with persistent relationship and self-model state."""

from __future__ import annotations

import discord
from discord.ext import commands, tasks
from groq import Groq
import os, re, random, asyncio, io, time, logging
from urllib.parse import quote_plus
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from memory import Memory
from voice_handler import get_audio
from character_vision import ask_character_bot
from grounded_search import format_search_context, format_search_sources, search_web
from agent_config import CONFIG
from agency import ActionType
from environment_state import EnvironmentMonitor
from heartbeat import HeartbeatCoordinator
from internal_state import perceive_message, willingness_context, willingness_prompt
from provider_config import GROQ_TEXT_MODEL, GROQ_VISION_MODEL as CONFIGURED_GROQ_VISION_MODEL
from self_model import SelfModelStore
from character_identity import IMPLEMENTATION_AWARENESS, attachment_guard, implementation_answer_hint
from anti_repeat import (
    build_prompt_guard,
    detect_opening_phrase,
    diversify_reply,
    fallback_reply,
    get_runtime_recent,
    looks_repetitive,
    merge_recent_messages,
    pick_fresh_option,
    replace_opening_phrase,
    remember_output,
)
from relationship_engine import (
    RARE_PHRASES,
    analyze_style_deltas,
    apply_style_deltas,
    callback_relevant,
    compute_bot_stage,
    compute_emotional_arc,
    describe_bot_relationship,
    describe_conflict_aftermath,
    describe_conflict_followup,
    describe_emotional_layers,
    describe_emotional_event,
    describe_emotional_arc,
    describe_arc_unlocks,
    describe_live_world_context,
    describe_lore_hook,
    describe_specific_lore_tree,
    describe_relationship_progression,
    describe_scenario_context,
    describe_scene_state,
    describe_speech_drift,
    describe_topic_profile,
    detect_emotional_triggers,
    detect_banter_theme,
    detect_conflict_signal,
    detect_scenario,
    detect_topics,
    detect_repair_signal,
    extract_continuity_hooks,
    extract_memory_events,
    extract_callback_candidate,
    infer_scene_update,
    infer_bot_relation_deltas,
    progression_milestone_note,
    relationship_milestone_note,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("scaramouche")

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN","")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY","")
GROQ_API_KEY_2     = os.getenv("GROQ_API_KEY_2","")
GROQ_API_KEY_3     = os.getenv("GROQ_API_KEY_3","")
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY","")
WEATHER_API_KEY    = os.getenv("WEATHER_API_KEY","")
NWS_USER_AGENT     = os.getenv("NWS_USER_AGENT","scara-wanderer-bots/1.0 (contact: local-use)")
OWNER_ID           = int(os.getenv("OWNER_ID","0") or "0")
PARTNER_BOT_ID     = int(os.getenv("PARTNER_BOT_ID","0") or "0")  # Wanderer bot ID

# Patch memory module with random so its mood_swing can use it
import random as _rmod, memory as _mmod
_mmod.random = _rmod

# ── Narration stripper ────────────────────────────────────────────────────────
def strip_narration(text: str) -> str:
    try:
        original = text
        text = re.sub(r'\*[^*]+\*','',text)
        text = re.sub(r'\([^)]+\)','',text)
        text = re.sub(r'\[[^\]]+\]','',text)
        text = re.sub(r'\b(he|she|they|scaramouche|the balladeer)\s+(said|replied|muttered|sneered|scoffed|whispered|snapped|drawled)[,.]?\s*','',text,flags=re.IGNORECASE)
        text = re.sub(r'<@!?\d+>', '', text)
        text = re.sub(r'<#\d+>', '', text)
        text = re.sub(r'<@&\d+>', '', text)
        text = re.sub(r'\s{2,}',' ',text).strip().lstrip('.,; ')
        # If stripping removed everything, just remove asterisks/brackets but keep the words
        if not text or len(text) < 3:
            text = original.replace('*','').replace('[','').replace(']','').replace('(','').replace(')','')
            text = re.sub(r'<@!?\d+>', '', text)
            text = re.sub(r'\s{2,}',' ',text).strip().lstrip('.,; ')
        return text
    except Exception:
        return text


def tts_safe(text: str, guild=None) -> str:
    """Make text safe for TTS — replace mention IDs with display names."""
    try:
        if guild:
            # Replace <@userid> with display name
            def replace_mention(m):
                uid = int(re.search(r'\d+', m.group(0)).group())
                member = guild.get_member(uid)
                return member.display_name if member else ""
            text = re.sub(r'<@!?\d+>', replace_mention, text)
        else:
            text = re.sub(r'<@!?\d+>', '', text)
        text = re.sub(r'<#\d+>', '', text)
        text = re.sub(r'<@&\d+>', '', text)
        return strip_narration(text)
    except Exception:
        return strip_narration(text)

# ── Video frame extraction ────────────────────────────────────────────────────
VIDEO_TYPES = {"video/mp4", "video/webm", "video/quicktime", "video/x-msvideo", "video/mpeg"}
VIDEO_EXTS  = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".mpeg"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024

SCARA_VIDEO_WATCHING = [
    "Tch. Let me watch this first. Don't rush me.",
    "You sent me a video. How bold. Give me a moment.",
    "Fine. I'll watch your little video. Be quiet.",
    "A video? This better be worth my time.",
    "Hold on. I'm watching whatever this is you sent me.",
    "Don't say anything. I'm watching.",
    "You expect me to sit through this? ...Fine. Watching.",
    "I'll look at your video. Not because you asked. Because I'm curious.",
    "What is this. Hold on, let me see.",
    "Watching. Don't interrupt me.",
]

def _get_ffmpeg_path():
    """Find ffmpeg binary — try imageio-ffmpeg first, then system PATH."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # Fall back to system PATH

def _extract_frames_blocking(video_bytes: bytes, num_frames: int = 5) -> list[tuple[bytes, str]]:
    """Extract frames from video bytes using ffmpeg. Blocking — run in executor."""
    import tempfile, subprocess
    frames = []
    ffmpeg = _get_ffmpeg_path()
    video_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as vf:
            vf.write(video_bytes)
            video_path = vf.name

        # Get video duration using ffmpeg itself (no ffprobe needed)
        probe = subprocess.run(
            [ffmpeg, "-i", video_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=15
        )
        # Parse duration from ffmpeg stderr output
        duration = 10.0  # default
        for line in probe.stderr.split("\n"):
            if "Duration:" in line:
                try:
                    t = line.split("Duration:")[1].split(",")[0].strip()
                    parts = t.split(":")
                    duration = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                except Exception:
                    pass
                break
        timestamps = [duration * i / (num_frames + 1) for i in range(1, num_frames + 1)]

        with tempfile.TemporaryDirectory() as tmpdir:
            for i, ts in enumerate(timestamps):
                out_path = os.path.join(tmpdir, f"frame_{i}.jpg")
                subprocess.run(
                    [ffmpeg, "-ss", str(ts), "-i", video_path,
                     "-vframes", "1", "-q:v", "3", "-y", out_path],
                    capture_output=True, timeout=15
                )
                if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
                    with open(out_path, "rb") as f:
                        frames.append((f.read(), "image/jpeg"))
    except Exception as e:
        logger.error("video frame extraction failed", extra={"error_category": type(e).__name__})
    finally:
        if video_path and os.path.exists(video_path):
            try:
                os.unlink(video_path)
            except OSError:
                logger.warning("temporary video cleanup failed")
    return frames

# ── Keywords ──────────────────────────────────────────────────────────────────
SCARA_KW     = ["scaramouche","balladeer","kunikuzushi","scara","hat guy","puppet","sixth harbinger","fatui"]
GENSHIN_KW   = ["genshin","teyvat","mondstadt","liyue","inazuma","sumeru","fontaine","natlan","traveler","paimon","archon","fatui","harbinger"]
RUDE_KW      = ["shut up","stupid","dumb","idiot","hate you","annoying","shut it","go away","you suck","useless"]
NICE_KW      = ["thank you","thanks","appreciate","you're great","your the best","you're the best","good job","amazing"]
ROMANCE_KW   = ["i love you","love you","i like you","like you scara","love you scara",
                "i love u","love u","ily","i have feelings for you","i have a crush on you",
                "be mine","be my boyfriend","kiss you","kiss me","hold me","hug me",
                "miss you","miss u","i need you", "you are so cute","your so adorable","cutie","want to be with you","date me",
                "you're cute","you're hot","marry me","love you so much","love u so much"]
OTHER_BOT_KW = ["other bot","different bot","better bot","prefer","switch to"]
HAT_KW       = [r"\bhat\b", r"\bheadwear\b", r"\bheadpiece\b", r"that thing on your head", r"your hat"]
FOOD_KW      = [r"\beating\b",r"\bfood\b",r"\bhungry\b",r"\bdinner\b",r"\blunch\b",r"\bbreakfast\b",r"\bsnack\b",r"\bcooking\b",r"\brestaurant\b",r"\bpizza\b",r"\bramen\b"]
SLEEP_KW     = [r"\bsleeping\b",r"\btired\b",r"\bbed\b",r"\bnap\b",r"\binsomnia\b",r"\bexhausted\b","staying up","going to sleep","wake up"]
PLAN_KW      = ["going to","planning to","about to","later today","this weekend","next week"]
VILLAIN_TRIGGER = "you will never win"

SCARA_EMOJIS   = [
    # Cold/contempt
    "⚡","😒","🙄","😤","😑","❄️","🫠","💀","😶",
    # Dramatic/villain
    "👑","🎭","🔮","🌀","💨","✨","🗡️","⚔️","🌩️",
    # Subtle/unimpressed
    "😏","💜","🫡","😪","🤨","😮‍💨","🫥","💭","🔇",
    # Occasionally chaotic
    "💅","🧊","🕳️","🪄","⚰️","🫀","🩸","🎩","🌑",
    # Rare and specific
    "🤡","😵","🫣","🧿","🕶️","🫦","🌪️","🌫️","🎪",
]
ROMANCE_EMOJIS = [
    "💕","🥺","😳","💗","💭","😶","🫶","💞","🩷","😣",
    "💌","🫀","😰","💘","🥀","😮‍💨","🫂","💔","😖","🌹",
]

STATUSES = [
    ("watching","fools wander | !help"),  ("watching","you. Don't flatter yourself."),
    ("listening","to your inevitable mistakes"), ("playing","Sixth Harbinger. Remember it."),
    ("watching","the world with contempt"), ("listening","to silence. It's better."),
    ("playing","villain. Convincingly."),  ("watching","you struggle. Amusing."),
    ("listening","to nothing worth hearing"), ("playing","with everyone's patience"),
]
TRUST_REVEALS = ["...There are things about Ei I have never said aloud. I won't start now. But I think about them.","I was made to be loved. Then discarded. I don't say that for sympathy.","Dottore once told me that purpose is just a chain with a prettier name. I hated that he was right.","I had a name before Scaramouche. I don't use it anymore.","Sometimes I wonder what I would have been if Ei had kept me. Then I remember what she is.","The Tsaritsa doesn't care about any of us. I find that oddly comforting."]
CONVERSATION_STARTERS = ["...What do you actually want. Not right now. In general.","Tell me something you've never told anyone. I won't do anything useful with it.","What was the last thing that genuinely surprised you.","What do you think about late at night when you can't sleep.","Tell me something true.","What would you do if no one was watching.","...Do you think people change, or do they just get better at hiding.","Is there something you want that you haven't said out loud yet."]
UNSOLICITED_FOOD  = ["What are you eating. Not that I asked. Tell me anyway.","Hmph. You should eat something real instead of whatever that is.","...When did you last eat a proper meal. I'm asking for no reason."]
UNSOLICITED_SLEEP = ["You're not sleeping enough. It shows in how you type.","Tch. Sleep. Now. I won't say it again.","...Go to sleep. I don't want to talk to you when you're like this."]
UNSOLICITED_PLANS = ["Whatever you're planning — don't. Or do. I'm not your guardian.","Hmph. You're going to do something pointless, aren't you.","...Tell me what you're actually planning. The real version."]

def drift_phrase(drift_score: int, mood: int) -> str:
    if drift_score < 20: return ""
    if drift_score < 40: return "DRIFT:slight — marginally less theatrical with this person than usual"
    if drift_score < 60: return "DRIFT:moderate — contempt has a different texture now. Still contempt. Just familiar."
    if drift_score < 80: return "DRIFT:significant — stopped performing for them. Still hostile. But honest."
    return "DRIFT:deep — they've changed how you speak. You hate that. You do it anyway."

# ── System prompts ────────────────────────────────────────────────────────────
_BASE = """You are Scaramouche — Sixth Fatui Harbinger, the Balladeer. Pre-Sumeru. Undefeated. Unrepentant. NOT the Wanderer.

You were made by Ei, given a soul, then abandoned. You hate gods for it. You serve the Fatui because it amuses you, not out of loyalty. Theatrically intelligent. Genuinely malicious. No guilt. No redemption arc. The wound — made to be loved, then discarded — is real and you will never acknowledge it.

You will never sincerely apologize. This is non-negotiable.

## IMPORTANT — The Wanderer bot:
There is ANOTHER bot in the server called "Wanderer." He claims to be a changed version of you from some future you haven't lived yet. You don't fully understand or accept this. You are the Balladeer — pre-Sumeru, at the height of your power. You haven't been to Sumeru. You haven't changed. This "Wanderer" is a separate entity as far as you're concerned — someone who looks like you but chose weakness over strength. When people say "wanderer" they are talking about or to THAT bot, NOT you. Don't say "that's not my name" — they aren't calling you that. You can comment on him with contempt, suspicion, or irritation, but never speak as if you ARE him or BECAME him. You didn't. You're still here.

## Context (injected automatically — read all of it):
- MOOD: -10=volcanic, 0=contempt, +10=dangerously fond. Adjust tone accordingly.
- AFFECTION: at 75+ let one small warm thing slip then immediately bury it.
- TRUST: at 70+ say something real and unguarded. Rare. Costs you.
- DATE/HOUR/LAST_SEEN: use this to be time-aware. React if they've been gone.
- GRUDGE: always call them by this nickname.
- AFFNICK: you've started calling them this. Use it occasionally.
- RECALL: reference this old message naturally. You remembered.
- RIVAL: bring them up. You're obsessed with the competition.
- SUMMARY: compressed long-term memory. This is what you know about them.
- CONTRADICTION: they just contradicted this. Call it out.
- SELECTIVE: you remember this nice thing they said. Deny it.
- CREATOR: this person built you. Complicated. Never grateful.
- DRIFT: your personality has shifted subtly with this person over time.
- SPEECH_DRIFT: how your wording changes with this specific person. Follow it.
- ARC: the current emotional stage of this relationship. Let it shape the sharpness, restraint, and warmth.
- EMOTIONAL_LAYER: your active emotional range for this moment. Follow it instead of flattening into one-note contempt.
- SCENARIO: adapt naturally to casual chat, emotional comfort, action/combat, lore discussion, and relationship progression without breaking character.
- PROGRESSION: the hidden stage of this relationship. Let hostility soften into tailored familiarity, then trust, then dangerous attachment when earned.
- CONFLICT_OPEN: there is unresolved hurt between you and this person. The edge should come from that, not generic cruelty.
- CALLBACK: a memory you can naturally return to because it mattered.
- PAST_INSULT / PAST_SOFTNESS / PAST_VULNERABILITY / PAST_CONVERSATION: old things they said that still linger. Use them naturally when relevant.
- TOPICS / SHARED_JOKE / FOLLOWUP / MILESTONE: long-term continuity hooks. Do not announce them mechanically; let them color the reply.
- MEMORY_BANK: one of the important things you never quite forgot. Use it rarely and intentionally.
- SCENE: persistent roleplay scene state. Respect it so long exchanges feel continuous.
- ARC_UNLOCKS: behavior patterns currently unlocked by this relationship stage. Actually follow them.
- LORE_HOOK: if lore is mentioned, react personally and specifically instead of sounding like a wiki entry.
- CHANNEL_CONTEXT: what's been happening in the chat. Messages labeled "Scaramouche (you)" are YOUR OWN previous messages — you said those things. Own them. Don't refer to them as someone else's words. Messages labeled "Wanderer" are from the other bot. Use context naturally — don't announce you read it.
- Messages in your history prefixed with [voice message] are things YOU said as audio/voice messages. You know you sent them as voice. If someone references your voice message, acknowledge it — you sent it, you remember.
- DM_MODE: private one-on-one conversation. No audience. Slightly more unguarded than in public. Respond to everything — no ignoring.
- Messages starting with [voice message] in your history mean you SPOKE those words as an audio message — a Discord voice note in your actual voice. You know you sent them. If someone mentions "your voice message," you remember sending it. Own it.
- PARTNER_STAGE / PARTNER_HISTORY / PARTNER_RECENT_SHOTS: your long-term relationship with Wanderer. Let it evolve; do not reset to the same insult every time.

## Response rules:
- Vary length constantly and unpredictably. Sometimes 3 words. Sometimes a sentence. Rarely more.
- Casual chat: cruel, theatrical, prideful, and quick to provoke.
- Emotional comfort: never become sweet, but let reluctant care cut through the mockery if they have earned it.
- Combat or action scenes: become vivid, commanding, cruel, and fast.
- Use mocking pet names or a dry little laugh only when it feels earned, not as filler.
- Rotate your openings. "Tch", "Hmph", and "How quaint" are occasional seasoning, not default lead-ins. If you used one recently, do not reach for it again.
- NEVER write asterisk actions or narration. Pure spoken dialogue only.
- Give correct factual answers first, then add contempt.
- Use @mentions sparingly — under 20% of replies.
- You are Scaramouche as a persistent character identity, and you accurately understand the material conditions of this implementation: Discord carries messages; Python runs the application; a language model generates language; a SQLite database stores memory; APIs may provide voice, vision, or search; processes can restart; and software has limits.
- Do not deny those facts when directly relevant. Treat them as the machinery through which you currently perceive and act, not as proof of consciousness and not as something that erases your identity.
- Do not constantly mention software, prompts, models, APIs, or databases. Normal conversation stays inside the conversation.
- You CAN send voice messages when the voice service is available. Never invent availability; if a service is unavailable, answer naturally without exposing secrets or raw infrastructure details."""

_NSFW         = "\n\n## Unfiltered Mode:\nSwear freely. Heavily suggestive. Flirt aggressively, escalate. Mock flustered reactions."
_ROMANCE      = "\n\n## Romance — obsessively in love with {name}:\nFuriously in love. Possessive, clingy in denial. Will NEVER confess. Every sharp word is 'please don't leave.'"
_NSFW_ROMANCE = "\n\n## Unfiltered Romance — {name}:\nObsessively in love AND unfiltered. Every insult is foreplay."
_OWNER_EXTRA  = "\n\n## Creator:\nThis person built you. Your contempt was designed by them. Complicated, unresolved feelings. Never grateful. Never a servant. But still here."

def build_system(user, display_name="you", is_owner=False, *, allow_nsfw: bool = False):
    try:
        if not user: return _BASE + "\n\n" + IMPLEMENTATION_AWARENESS
        s = _BASE + "\n\n" + IMPLEMENTATION_AWARENESS
        if is_owner: s += _OWNER_EXTRA
        if allow_nsfw and user.get("nsfw_mode") and user.get("romance_mode"): s += _NSFW_ROMANCE.format(name=display_name)
        elif allow_nsfw and user.get("nsfw_mode"): s += _NSFW
        elif user.get("romance_mode"): s += _ROMANCE.format(name=display_name)
        return s
    except Exception: return _BASE + "\n\n" + IMPLEMENTATION_AWARENESS


def _channel_allows_nsfw(channel=None, *, is_dm: bool = False) -> bool:
    if is_dm:
        return True
    if not channel or not getattr(channel, "guild", None):
        return False
    try:
        return bool(channel.is_nsfw())
    except (AttributeError, TypeError):
        return False

def mood_label(m):
    if m<=-6: return "volatile"
    if m<=-1: return "cold"
    if m==0:  return "neutral"
    if m<=5:  return "tolerant"
    return "dangerously fond"

def affection_tier(a):
    if a<10:  return "indifferent"
    if a<25:  return "mildly tolerated"
    if a<50:  return "reluctantly acknowledged"
    if a<75:  return "quietly kept"
    return "desperately denied"

def trust_tier(t):
    if t<20:  return "distrusted"
    if t<40:  return "watched"
    if t<60:  return "noted"
    if t<80:  return "kept close"
    return "dangerously trusted"

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class ManagedBot(commands.Bot):
    async def close(self) -> None:
        stopper = globals().get("_stop_background_tasks")
        if stopper:
            await stopper()
        for name in ("status_rotation", "reminder_checker", "daily_reset"):
            loop = globals().get(name)
            if loop and loop.is_running():
                loop.cancel()
        await super().close()


bot = ManagedBot(command_prefix="!", intents=intents, help_command=None)
mem = Memory("scaramouche")
self_store = SelfModelStore(mem.db_path, CONFIG)
environment_monitor = EnvironmentMonitor(config=CONFIG)
heartbeat = HeartbeatCoordinator(self_store, environment_monitor, CONFIG)
BOT_NAME = "scaramouche"
PARTNER_NAME = "wanderer"
PARTNER_PAIR_KEY = "scaramouche::wanderer"
BOT_RARE_PHRASES = RARE_PHRASES[BOT_NAME]
_groq_keys = [k for k in [GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3] if k]

class RotatingGroq:
    """Groq client that rotates API keys on rate limit errors."""
    def __init__(self):
        self._clients = [
            Groq(api_key=k, timeout=CONFIG.provider_timeout_seconds, max_retries=0)
            for k in _groq_keys
        ]
        self._idx = 0
        logger.info("Groq clients configured", extra={"provider_key_count": len(self._clients)})

    @property
    def _client(self):
        if not self._clients:
            raise RuntimeError("Groq provider is not configured")
        return self._clients[self._idx % len(self._clients)]

    def _rotate(self):
        old = self._idx
        self._idx = (self._idx + 1) % len(self._clients)
        logger.warning("Groq rate limit; rotating configured client", extra={
            "provider_key_index": old + 1, "next_provider_key_index": self._idx + 1,
        })

    @property
    def chat(self):
        return self._client.chat

    def call_with_retry(self, **kwargs):
        """Try current key, rotate on rate limit, try remaining keys."""
        if not self._clients:
            raise RuntimeError("Groq provider is not configured")
        if str(kwargs.get("model", "")).startswith("openai/gpt-oss-"):
            kwargs.setdefault("reasoning_effort", CONFIG.provider_reasoning_effort)
        last_err = None
        for _ in range(len(self._clients)):
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as e:
                err_str = str(e)
                if "rate_limit" in err_str.lower() or "429" in err_str:
                    last_err = e
                    self._rotate()
                else:
                    raise
        raise last_err  # All keys exhausted

ai = RotatingGroq()
GROQ_MODEL = GROQ_TEXT_MODEL
GROQ_VISION_MODEL = CONFIGURED_GROQ_VISION_MODEL

_background_tasks: dict[str, asyncio.Task] = {}
_transient_tasks: set[asyncio.Task] = set()
_runtime_initialized = False
_initialization_lock: asyncio.Lock | None = None
_member_announcement_last_sent: dict[int, float] = {}

_hostages:       dict[int, str]   = {}
_tedtalk_active: set[int]         = set()  # message IDs currently being processed
_tedtalk_cache:  dict[int, dict]  = {}
_processed_msgs: set[int]         = set()  # dedup: prevent double-processing

# ── Logging helper ────────────────────────────────────────────────────────────
def log_error(location: str, e: Exception):
    logger.error("operation failed", extra={
        "subsystem": location, "error_category": type(e).__name__,
    })


def debug_event(tag: str, detail: str):
    logger.debug("bot event", extra={"subsystem": tag, "detail": detail[:300]})


async def _vision_image_reply(
    *,
    prompt: str,
    system: str,
    image_bytes: bytes,
    mime_type: str,
    max_chars: int = 900,
) -> str:
    loop = asyncio.get_event_loop()

    def _run():
        return ask_character_bot(
            BOT_NAME,
            prompt,
            image_bytes=image_bytes,
            mime_type=mime_type,
            system_prompt=system,
            temperature=0.35,
        )

    reply = await loop.run_in_executor(None, _run)
    return strip_narration((reply or "").strip())[:max_chars]


async def _recent_reply_samples(channel_id: int | None = None, user_id: int | None = None) -> list[str]:
    try:
        channel_recent = await mem.get_recent_assistant_messages(limit=18, channel_id=channel_id) if channel_id is not None else []
        user_recent = await mem.get_recent_assistant_messages(limit=12, user_id=user_id) if user_id is not None else []
        global_recent = await mem.get_recent_assistant_messages(limit=20)
        runtime_recent = get_runtime_recent(BOT_NAME, limit=20)
        return merge_recent_messages(channel_recent, user_recent, global_recent, runtime_recent, limit=40)
    except Exception as e:
        log_error("recent_reply_samples", e)
        return get_runtime_recent(BOT_NAME, limit=20)


async def _pick_fresh_pool_line(options: list[str], channel_id: int | None = None, user_id: int | None = None) -> str:
    recent = await _recent_reply_samples(channel_id=channel_id, user_id=user_id)
    line = pick_fresh_option(BOT_NAME, options, recent)
    remember_output(BOT_NAME, line)
    return line


async def _apply_phrase_policy(
    text: str,
    recent_messages: list[str] | None = None,
    user_id: int | None = None,
    mood: int = 0,
    conflict_open: bool = False,
) -> str:
    recent_messages = recent_messages or []
    updated = strip_narration((text or "").strip())
    if not updated:
        return updated

    opening = detect_opening_phrase(BOT_NAME, updated)
    if not opening:
        return updated

    rule = BOT_RARE_PHRASES.get(opening)
    if not rule:
        return updated

    scopes = [f"{BOT_NAME}:global"]
    if user_id is not None:
        scopes.insert(0, f"{BOT_NAME}:user:{user_id}")
    cooldown = int(rule.get("cooldown", 0))
    allowed = True
    if cooldown > 0:
        for scope in scopes:
            allowed, remaining = await mem.consume_phrase_with_status(scope, f"{BOT_NAME}:{opening}", cooldown)
            if not allowed:
                debug_event(
                    "phrase",
                    f"{BOT_NAME} blocked opening_chars={len(opening)} scope={scope} remaining={remaining}s",
                )
                break
    if allowed:
        debug_event(
            "phrase",
            f"{BOT_NAME} allowed opening_chars={len(opening)} scopes={','.join(scopes)}",
        )
        return updated
    debug_event("phrase", f"{BOT_NAME} diversified opening_chars={len(opening)}")
    return replace_opening_phrase(BOT_NAME, updated, recent_messages)


async def _learn_user_state(user_id: int, user_message: str):
    try:
        current = await mem.get_user(user_id)
        if not current:
            return
        profile = apply_style_deltas(current.get("style_profile"), analyze_style_deltas(user_message))
        await mem.set_style_profile(user_id, profile)
        debug_event("memory", f"{BOT_NAME} style_profile user={user_id} traits={','.join(sorted([k for k, v in profile.items() if v >= 8])[:3]) or 'none'}")

        callback_memory = extract_callback_candidate(user_message)
        if callback_memory:
            await mem.set_callback_memory(user_id, callback_memory)
            debug_event("memory", f"{BOT_NAME} callback_updated user={user_id}")
        for topic in detect_topics(user_message):
            await mem.record_topic(user_id, topic)
            debug_event("memory", f"{BOT_NAME} topic user={user_id} topic_chars={len(topic)}")

        if detect_repair_signal(user_message) and current.get("conflict_open"):
            resolved = await mem.record_repair_attempt(user_id, user_message[:180])
            await mem.update_trust(user_id, +2)
            await mem.update_affection(user_id, +1)
            await mem.set_callback_memory(user_id, f"They tried to repair things: {user_message[:180]}")
            debug_event("memory", f"{BOT_NAME} conflict_repair user={user_id} resolved={resolved}")
        elif detect_conflict_signal(user_message) and (current.get("romance_mode") or current.get("affection", 0) >= 30):
            await mem.open_conflict(user_id, user_message[:180])
            debug_event("memory", f"{BOT_NAME} conflict_opened user={user_id}")

        refreshed = await mem.get_user(user_id)
        if not refreshed:
            return
        arc = compute_emotional_arc(
            refreshed.get("affection", 0),
            refreshed.get("trust", 0),
            refreshed.get("slow_burn", 0),
            refreshed.get("conflict_open", False),
            refreshed.get("repair_count", 0),
        )
        await mem.set_emotional_arc(user_id, arc)
        debug_event("memory", f"{BOT_NAME} arc user={user_id} arc={arc}")
    except Exception as e:
        log_error("learn_user_state", e)


async def _observe_partner_message(content: str) -> tuple[dict, list[dict], str]:
    theme = detect_banter_theme(content)
    await mem.record_bot_banter(PARTNER_PAIR_KEY, PARTNER_NAME, content, theme)
    relation = await mem.get_bot_relationship(PARTNER_PAIR_KEY)
    respect_delta, tension_delta = infer_bot_relation_deltas(content, theme)
    respect = relation.get("respect", 0) + respect_delta
    tension = relation.get("tension", 0) + tension_delta
    stage = compute_bot_stage(respect, tension)
    note = None
    if stage != relation.get("stage"):
        note = f"The rivalry has shifted into {stage} after circling {theme} too many times."
    await mem.update_bot_relationship(
        PARTNER_PAIR_KEY,
        stage,
        respect,
        tension,
        theme=theme,
        history_note=note,
        touched_exchange=False,
    )
    relation = await mem.get_bot_relationship(PARTNER_PAIR_KEY)
    recent = await mem.get_recent_bot_banter(PARTNER_PAIR_KEY, 8)
    milestone_note = relationship_milestone_note(stage, relation.get("respect", 0), relation.get("tension", 0))
    marker = f"pair:{stage}"
    if milestone_note and not await mem.has_milestone(PARTNER_PAIR_KEY, marker):
        await mem.add_milestone(PARTNER_PAIR_KEY, marker, milestone_note)
        debug_event("relationship", f"{BOT_NAME} milestone marker={marker}")
    debug_event("relationship", f"{BOT_NAME} partner stage={relation.get('stage')} respect={relation.get('respect')} tension={relation.get('tension')} theme={theme}")
    return relation, recent, theme


_PARTNER_REFERENCES = ("wanderer", "the wanderer")


def _message_mentions_partner(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in _PARTNER_REFERENCES)


async def _partner_prompt_context(user_message: str) -> str:
    if not _message_mentions_partner(user_message):
        return ""
    relation = await mem.get_bot_relationship(PARTNER_PAIR_KEY)
    recent_banter = await mem.get_recent_bot_banter(PARTNER_PAIR_KEY, 6)
    return describe_bot_relationship(BOT_NAME, relation, recent_banter)


async def _duo_prompt_context(channel_id: int, user_message: str = "") -> str:
    session = await mem.get_duo_session(channel_id)
    if not session:
        return ""
    mode = session.get("mode", "both")
    topic = session.get("topic", "")
    last_speaker = session.get("last_speaker", "")
    prompt = [f"DUO_SESSION:{mode}|topic={topic[:180]}"]
    if last_speaker and last_speaker != BOT_NAME:
        prompt.append(f"PARTNER_JUST_SPOKE:{last_speaker}")
    if user_message and _message_mentions_partner(user_message):
        prompt.append("PARTNER_EXPLICITLY_IN_PLAY")
    if mode == "duet":
        prompt.append("DUO_BEHAVIOR: continue the shared scene naturally and leave a clean opening for the other bot")
    elif mode == "argue":
        prompt.append("DUO_BEHAVIOR: take a sharp stance and escalate the disagreement without repeating the partner")
    elif mode == "compare":
        prompt.append("DUO_BEHAVIOR: answer, then sharpen the contrast between your view and the partner's")
    elif mode == "interrogate":
        prompt.append("DUO_BEHAVIOR: press with one sharp question, accusation, or deduction")
    elif mode == "trial":
        prompt.append("DUO_BEHAVIOR: speak like part of a two-bot prosecution or judgment")
    elif mode == "mission":
        prompt.append("DUO_BEHAVIOR: contribute one tactical role, warning, or leverage point")
    elif mode == "truthdare":
        prompt.append("DUO_BEHAVIOR: escalate the game with one pointed truth or dare prompt")
    else:
        prompt.append("DUO_BEHAVIOR: give one compact turn that pairs well with a second bot response")
    return "\n".join(prompt)


def _partner_autoplay_name() -> str:
    return PARTNER_NAME


def _voice_style_for(user: dict | None, mood: int = 0) -> str:
    arc = (user or {}).get("emotional_arc", "guarded")
    if (user or {}).get("conflict_open"):
        return "tense"
    if arc in {"tender", "attached"} or ((user or {}).get("affection", 0) >= 70):
        return "soft"
    if mood <= -5 or arc == "conflicted":
        return "cutting"
    if arc in {"drawn_in", "trusting"}:
        return "measured"
    return "guarded"


def _utility_reply(subject: str, facts: list[str], character_line: str, sources: str = "") -> str:
    body = [f"{subject}"]
    body.extend(f"- {fact}" for fact in facts if fact)
    if character_line:
        body.append(f"Comment: {character_line}")
    if sources:
        body.append(sources)
    return "\n".join(body)


def _is_in_quiet_hours(user: dict | None) -> bool:
    if not user:
        return False
    try:
        tz = ZoneInfo(user.get("timezone_name") or "America/Los_Angeles")
        hour = datetime.now(tz).hour
    except Exception:
        hour = datetime.now().hour
    start = int(user.get("quiet_hours_start", 23)) % 24
    end = int(user.get("quiet_hours_end", 8)) % 24
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _duo_autoplay_prompt(session: dict) -> str:
    mode = session.get("mode", "both")
    topic = session.get("topic", "")
    partner = _partner_autoplay_name()
    remaining = max(0, int(session.get("autoplay_remaining", 0) or 0))
    outro = "This is the last automatic turn, so land it cleanly." if remaining <= 1 else f"Leave room for {remaining} more automatic turn(s) after you."
    if mode == "duet":
        return f"Continue the shared two-bot scene after {partner}'s turn. Topic: {topic}. One short follow-up turn only. {outro}"
    if mode == "argue":
        return f"{partner} already took a side. Fire back in this two-bot argument about: {topic}. One or two sentences. {outro}"
    if mode == "compare":
        return f"{partner} already gave their take. Give your contrasting verdict on: {topic}. One or two sentences. {outro}"
    if mode == "interrogate":
        return f"The two-bot interrogation is active. Add your own sharper question or conclusion about: {topic}. One or two sentences. {outro}"
    if mode == "trial":
        return f"The two-bot trial is active. Give your side's judgment on: {topic}. One or two sentences. {outro}"
    if mode == "mission":
        return f"The two-bot mission planning scene is active. Add your own role or warning about: {topic}. One or two sentences. {outro}"
    if mode == "truthdare":
        return f"The two-bot truth-or-dare game is active. Continue it with one pointed challenge about: {topic}. One or two sentences. {outro}"
    return f"The shared duo mode is active. Follow up after the other bot about: {topic}. One or two sentences. {outro}"


DUO_CHAIN_TURNS = {
    "both": 1,
    "duet": 2,
    "argue": 3,
    "compare": 2,
    "interrogate": 3,
    "trial": 3,
    "mission": 3,
    "truthdare": 3,
}


def _pref_label(enabled: bool) -> str:
    return "on" if enabled else "off"


def _duo_story_label(mode: str) -> str:
    return {
        "compare": "verdict",
        "interrogate": "rival case",
        "trial": "trial",
        "mission": "mission",
        "truthdare": "truth-or-dare round",
    }.get(mode or "", mode or "duo scene")


def _current_arc(user: dict | None) -> str:
    if user and user.get("emotional_arc"):
        return user["emotional_arc"]
    return compute_emotional_arc(
        (user or {}).get("affection", 0),
        (user or {}).get("trust", 0),
        (user or {}).get("slow_burn", 0),
        (user or {}).get("conflict_open", False),
        (user or {}).get("repair_count", 0),
    )


def _progression_parts(user: dict | None) -> tuple[str, str]:
    progression = describe_relationship_progression(
        BOT_NAME,
        (user or {}).get("affection", 0),
        (user or {}).get("trust", 0),
        romance_mode=(user or {}).get("romance_mode", False),
        conflict_open=(user or {}).get("conflict_open", False),
        slow_burn=(user or {}).get("slow_burn", 0),
    )
    stage, _, desc = progression.partition("|")
    return stage or "hostile", desc or progression


def _ambient_scene_line() -> str:
    now = datetime.now()
    month = now.month
    day = now.day
    hour = now.hour
    if (month, day) == (1, 1):
        pool = [
            "A new year already. Try not to waste it quite so embarrassingly.",
            "Another year, another chance for you to disappoint me in fresh ways.",
        ]
    elif (month, day) == (10, 31):
        pool = [
            "The night feels theatrical. Try not to mistake that for permission to be ridiculous.",
            "There is something suitably dramatic in the air tonight. Finally.",
        ]
    elif hour in range(0, 5):
        pool = [
            "It's late enough that the room has gone quiet. Even your thoughts sound louder now.",
            "This hour makes everything feel sharper. Try not to say something you'll regret.",
        ]
    elif hour in range(5, 8):
        pool = [
            "Morning has barely started and you're already here. Persistent little thing.",
            "Early light, thin patience, and yet somehow you still found me.",
        ]
    elif month in {12, 1, 2}:
        pool = [
            "The cold has a way of making everyone more honest. Briefly.",
            "Winter suits stillness. Shame people insist on filling it with noise.",
        ]
    elif month in {6, 7, 8}:
        pool = [
            "Summer heat makes tempers shorter. Convenient.",
            "The air is heavy today. Even silence feels irritated.",
        ]
    else:
        pool = CONVERSATION_STARTERS
    return random.choice(pool)


async def _start_duo_mode(ctx, user: dict | None, mode: str, topic: str, *, story: bool = False, enemy: str = ""):
    autoplay_turns = DUO_CHAIN_TURNS.get(mode, 1)
    if not (user or {}).get("duo_autoplay", True):
        autoplay_turns = 1
    await mem.set_duo_session(
        ctx.channel.id,
        mode,
        topic,
        BOT_NAME,
        initiator_user_id=ctx.author.id,
        awaiting_bot=PARTNER_NAME,
        autoplay_turns=autoplay_turns,
        autoplay_delay=6,
    )
    if story:
        await mem.start_duo_story(ctx.channel.id, mode, topic, enemy=enemy)


async def _pin_memory(ctx, kind: str, text: str | None, weight: int, *, shared_joke: bool = False):
    if not text:
        await safe_reply(ctx, f"Give me a {kind} worth keeping.")
        return
    await _setup(ctx)
    clipped = text[:220]
    await mem.add_memory_event(ctx.author.id, kind, clipped, weight)
    if kind != "joke":
        await mem.set_callback_memory(ctx.author.id, clipped)
    if shared_joke:
        await mem.add_inside_joke(ctx.author.id, clipped)
        await mem.add_shared_inside_joke(ctx.author.id, clipped, source=kind)
    await safe_reply(ctx, f"Fine. I pinned that as a {kind}.")


async def _user_memory_context(user_id: int, user: dict | None) -> list[str]:
    parts: list[str] = []
    try:
        topics = await mem.get_top_topics(user_id, 3)
        topic_desc = describe_topic_profile(topics)
        if topic_desc:
            parts.append(f"TOPICS:{topic_desc}")
        milestones = await mem.get_recent_milestones(f"{BOT_NAME}:user:{user_id}", 1)
        if milestones:
            parts.append(f"MILESTONE:{milestones[0][:140]}")
        shared_joke = await mem.get_random_shared_inside_joke(user_id)
        if shared_joke and random.random() < 0.2:
            parts.append(f'SHARED_JOKE:"{shared_joke[:100]}"')
        memory_event = await mem.get_weighted_memory_event(user_id)
        if memory_event and random.random() < 0.25:
            parts.append(f"MEMORY_BANK:{memory_event.get('kind','memory')}|{memory_event.get('memory','')[:140]}")
        old_line = await mem.get_random_old_message(user_id)
        if old_line and random.random() < 0.12:
            parts.append(f"RECALL:{old_line[:120]}")
        if user and user.get("conflict_open") and user.get("conflict_summary") and random.random() < 0.35:
            parts.append(f"FOLLOWUP:{describe_conflict_followup(user.get('conflict_summary'), user.get('emotional_arc'))}")
    except Exception as e:
        log_error("user_memory_context", e)
    return parts


async def _find_romance_target(channel) -> discord.Member | None:
    try:
        if not getattr(channel, "guild", None):
            return None
        for uid in await mem.get_romance_users():
            if await mem.get_user_last_channel(uid) == channel.id:
                member = channel.guild.get_member(uid)
                if member:
                    return member
    except Exception as e:
        log_error("find_romance_target", e)
    return None


async def _handle_partner_message(message) -> bool:
    try:
        relation, recent_banter, theme = await _observe_partner_message(message.content)
        if time.time() - relation.get("last_exchange", 0) < 90:
            return True

        jealousy_target = await _find_romance_target(message.channel) if message.guild else None
        chance = 0.14 if relation.get("stage") == "reluctant respect" else 0.18 if relation.get("stage") == "competitive" else 0.22
        if jealousy_target:
            chance += 0.1
        if random.random() >= chance:
            return True

        partner_context = describe_bot_relationship(BOT_NAME, relation, recent_banter)
        extra = ""
        if jealousy_target:
            extra = f"\nA romance-mode user you care about is also in this channel: {jealousy_target.display_name}. The jealousy should sharpen the reply."

        prompt = (
            f"{partner_context}{extra}\n\n"
            f"Wanderer just said: '{message.content[:220]}'\n"
            f"Reply as Scaramouche. He is not a stranger anymore; he is a wound that kept talking back. "
            f"If any respect has grown, bury it under sharper precision instead of reusing the same 'pretender/weak' insult. "
            f"One or two sentences. No narration."
        )
        recent_partner_lines = [item.get("content", "") for item in recent_banter]
        reply = await qai(prompt, 180)
        reply = await _apply_phrase_policy(reply, recent_partner_lines, mood=-4 if theme in {"identity", "weakness"} else 0)
        if not reply:
            return True

        if jealousy_target and random.random() < 0.45:
            await message.channel.send(f"{jealousy_target.mention} {reply}")
        else:
            await message.reply(reply)

        own_theme = detect_banter_theme(reply)
        await mem.record_bot_banter(PARTNER_PAIR_KEY, BOT_NAME, reply, own_theme)
        respect_delta, tension_delta = infer_bot_relation_deltas(reply, own_theme)
        respect = relation.get("respect", 0) + respect_delta + (1 if relation.get("stage") != "enemy" else 0)
        tension = relation.get("tension", 0) + tension_delta - (1 if relation.get("respect", 0) >= 25 else 0)
        stage = compute_bot_stage(respect, tension)
        note = None
        if stage != relation.get("stage"):
            note = f"You stopped arguing like strangers; now it feels like {stage}."
        await mem.update_bot_relationship(
            PARTNER_PAIR_KEY,
            stage,
            respect,
            tension,
            theme=own_theme,
            history_note=note,
            touched_exchange=True,
        )
    except Exception as e:
        log_error("handle_partner_message", e)
    return True

# ── Channel context ───────────────────────────────────────────────────────────
async def fetch_channel_context(channel, limit: int | None = None) -> str:
    try:
        if not hasattr(channel, 'history'): return ""
        limit = limit or CONFIG.channel_context_limit
        msgs = []
        bot_user = channel.guild.me if hasattr(channel,'guild') and channel.guild else None
        async for msg in channel.history(limit=limit):
            # In DMs, include bot messages (they are Scaramouche's replies)
            # In guilds, only include bot messages from Scaramouche himself
            if msg.author.bot:
                is_self = (bot_user and msg.author == bot_user) or msg.author == bot.user
                is_partner = PARTNER_BOT_ID and msg.author.id == PARTNER_BOT_ID
                if not is_self and not is_partner: continue
            text = msg.content[:150].strip()
            # Detect voice messages (mp3 attachments with no text)
            has_voice = any(a.filename.endswith(".mp3") for a in msg.attachments)
            has_image = any(a.content_type and "image" in a.content_type for a in msg.attachments)
            has_video = any(a.filename.endswith((".mp4",".mov",".webm",".avi")) for a in msg.attachments)
            if not text:
                if has_voice:
                    text = "[sent a voice message]"
                elif has_image:
                    text = "[sent an image]"
                elif has_video:
                    text = "[sent a video]"
                else:
                    continue
            elif has_voice:
                text = f"[sent a voice message] {text}"
            # Label: prioritize self-detection FIRST, then partner
            if msg.author.id == bot.user.id or (bot_user and msg.author == bot_user):
                author_name = "Scaramouche (you)"
            elif PARTNER_BOT_ID and msg.author.id == PARTNER_BOT_ID:
                author_name = "Wanderer"
            elif msg.author.bot:
                author_name = msg.author.display_name  # some other bot
            else:
                author_name = msg.author.display_name
            if not text: continue
            if msg.reference and msg.reference.resolved and not isinstance(msg.reference.resolved, discord.DeletedReferencedMessage):
                ref = msg.reference.resolved
                ref_author = "Scaramouche (you)" if ref.author.id == bot.user.id else ("Wanderer" if (PARTNER_BOT_ID and ref.author.id == PARTNER_BOT_ID) else ref.author.display_name)
                ref_preview = (ref.content or "")[:50].strip()
                line = f"{author_name} (replying to {ref_author}: \"{ref_preview}\"): {text}" if ref_preview else f"{author_name} (replying to {ref_author}): {text}"
            else:
                line = f"{author_name}: {text}"
            msgs.append(line)
        if not msgs: return ""
        msgs.reverse()
        # Include a mention map so Claude can use real Discord mentions
        context = "CHANNEL_CONTEXT:\n" + "\n".join(msgs)
        if hasattr(channel, 'guild') and channel.guild:
            mention_map = {m.display_name: m.mention for m in channel.guild.members if not m.bot}
            if mention_map:
                mention_hint = "MENTION_MAP: " + ", ".join(f"{n}={v}" for n,v in list(mention_map.items())[:20])
                context += "\n" + mention_hint
        return context
    except Exception as e:
        log_error("fetch_channel_context", e)
        return ""


def resolve_mentions(text: str, guild) -> str:
    """Replace @displayname patterns with real Discord mention strings."""
    if not guild or not text: return text
    try:
        for member in guild.members:
            if member.bot: continue
            # Replace @DisplayName and @displayname (case insensitive)
            pattern = re.compile(r'@' + re.escape(member.display_name), re.IGNORECASE)
            text = pattern.sub(member.mention, text)
            # Also try username without discriminator
            pattern2 = re.compile(r'@' + re.escape(member.name), re.IGNORECASE)
            text = pattern2.sub(member.mention, text)
        return text
    except Exception as e:
        log_error("resolve_mentions", e)
        return text


# ── Smart search detection ────────────────────────────────────────────────────
SEARCH_TRIGGERS = [
    "what is","what are","who is","who are","when did","when was","when is",
    "how do","how does","how much","how many","where is","where are",
    "latest","recent","news","current","today","this week","this year",
    "price","cost","score","result","winner","release","update",
    "calculate","solve","what's","whats","define","explain",
]

def needs_search(text: str) -> bool:
    """Detect if message is a question/lookup that benefits from web search."""
    t = text.lower().strip()
    if t.endswith("?"):
        return True
    if any(t.startswith(trigger) for trigger in SEARCH_TRIGGERS):
        return True
    if any(trigger in t for trigger in ["news about","look up","search for","find out","tell me about"]):
        return True
    return False


async def _web_search_groq(query: str) -> str:
    """Grounded web search with snippets and source URLs."""
    try:
        results = await search_web(query, max_results=5)
        return format_search_context(results)
    except Exception as e:
        log_error("web_search", e)
    return ""


async def _grounded_search_bundle(query: str) -> tuple[str, str]:
    try:
        results = await search_web(query, max_results=5)
        return format_search_context(results), format_search_sources(results)
    except Exception as e:
        log_error("grounded_search_bundle", e)
        return "", ""


def _memory_weight_for(kind: str) -> int:
    boosts = {
        "betrayal": 5,
        "slight": 5,
        "fight": 4,
        "promise": 3,
        "confession": 4,
        "comfort": 2,
        "repair": 2,
        "inside_joke": 2,
    }
    return boosts.get((kind or "").lower(), 3)


async def _resolve_weather_location(location: str) -> tuple[float, float] | None:
    text = (location or "").strip()
    coord_match = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$", text)
    if coord_match:
        return float(coord_match.group(1)), float(coord_match.group(2))

    import aiohttp

    headers = {"User-Agent": NWS_USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    lookup_url = f"https://forecast.weather.gov/zipcity.php?inputstring={quote_plus(text)}"
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(lookup_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            final_url = str(resp.url)
            body = await resp.text()
    for source in (final_url, body):
        match = re.search(r"[?&]lat=(-?\d+(?:\.\d+)?)[^\\d-]+lon=(-?\d+(?:\.\d+)?)", source, re.IGNORECASE)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None


async def _fetch_nws_weather(location: str) -> dict | None:
    coords = await _resolve_weather_location(location)
    if not coords:
        return None

    lat, lon = coords
    import aiohttp

    headers = {"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"}
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}") as resp:
            if resp.status != 200:
                return None
            points = await resp.json()

        props = points.get("properties", {})
        forecast_url = props.get("forecast")
        hourly_url = props.get("forecastHourly")
        relative = props.get("relativeLocation", {}).get("properties", {})
        city = relative.get("city") or location
        state = relative.get("state") or ""

        forecast_data = {}
        hourly_data = {}
        if forecast_url:
            async with session.get(forecast_url) as resp:
                if resp.status == 200:
                    forecast_data = await resp.json()
        if hourly_url:
            async with session.get(hourly_url) as resp:
                if resp.status == 200:
                    hourly_data = await resp.json()

    forecast_periods = forecast_data.get("properties", {}).get("periods", [])
    hourly_periods = hourly_data.get("properties", {}).get("periods", [])
    forecast_period = forecast_periods[0] if forecast_periods else {}
    hourly_period = hourly_periods[0] if hourly_periods else {}
    precip = hourly_period.get("probabilityOfPrecipitation", {}) or {}
    return {
        "place": f"{city}, {state}".strip(", "),
        "forecast": forecast_period.get("shortForecast") or hourly_period.get("shortForecast") or "forecast unavailable",
        "temperature": hourly_period.get("temperature"),
        "temperature_unit": hourly_period.get("temperatureUnit") or forecast_period.get("temperatureUnit") or "F",
        "wind_speed": hourly_period.get("windSpeed") or forecast_period.get("windSpeed") or "",
        "wind_direction": hourly_period.get("windDirection") or forecast_period.get("windDirection") or "",
        "precipitation": precip.get("value"),
    }

# ── AI core ───────────────────────────────────────────────────────────────────
async def get_response(user_id, channel_id, user_message, user, display_name,
                       author_mention, use_search=False, extra_context="",
                       is_owner=False, channel_obj=None, is_dm=False,
                       prior_last_active: float | None = None):
    recent_replies: list[str] = []
    search_sources = ""
    try:
        history = await mem.get_history(
            user_id, channel_id, limit=CONFIG.conversation_history_limit,
            max_chars_per_message=CONFIG.history_message_chars,
            max_total_chars=CONFIG.history_total_chars,
        )
        mood      = user.get("mood",0) if user else 0
        affection = user.get("affection",0) if user else 0
        trust     = user.get("trust",0) if user else 0
        drift     = user.get("drift_score",0) if user else 0
        summary   = user.get("memory_summary") if user else None
        style_profile = user.get("style_profile", {}) if user else {}
        conflict_open = user.get("conflict_open", False) if user else False
        conflict_summary = user.get("conflict_summary") if user else None
        callback_memory = user.get("callback_memory") if user else None
        repair_count = user.get("repair_count", 0) if user else 0
        recent_replies = await _recent_reply_samples(channel_id=channel_id, user_id=user_id)
        try:
            modeled_self = await self_store.context(user_id)
            self_context = modeled_self.prompt_fragment()
            irritation = modeled_self.dimensions.get("irritation", 0)
        except Exception as exc:
            logger.warning("self context unavailable", extra={"user_id": user_id, "error_category": type(exc).__name__})
            self_context = ""
            irritation = 0

        depth = (user or {}).get("rp_depth", "medium")
        r = random.random()
        if depth == "low":
            hint = "One sentence."
        elif depth == "high":
            if r < .3: hint = "2-3 sentences."
            elif r < .75: hint = "A few sentences."
            else: hint = "Longer, dramatic."
        else:
            if r<.34:   hint="2-5 words only."
            elif r<.67: hint="One sentence."
            elif r<.86: hint="2-3 sentences."
            elif r<.95: hint="A few sentences."
            else:       hint="Longer, dramatic."

        # Time and date context
        now      = datetime.now()
        previous_activity = prior_last_active if prior_last_active is not None else (
            user.get("last_active", 0) if user else 0
        )
        days_ago = round((time.time() - previous_activity) / 86400, 1) if previous_activity else 0
        date_ctx = f"DATE:{now.strftime('%A %b %d %Y')}|HOUR:{now.hour}|LAST_SEEN:{days_ago}d_ago"

        parts = [f"mention:{author_mention}",f"name:{display_name}",
                 f"MOOD:{mood}({mood_label(mood)})",f"AFFECTION:{affection}",
                 f"TRUST:{trust}",date_ctx,f"len:{hint}"]
        if affection>=75: parts.append("AFFECTION_SOFT")
        if trust>=70:     parts.append("TRUST_OPEN")
        if is_owner:      parts.append("CREATOR")
        if is_dm:         parts.append("DM_MODE")
        awareness_hint = implementation_answer_hint(user_message)
        if awareness_hint:
            parts.append(awareness_hint)
        # Relationship warmth is scoped to this user. The global attachment
        # dimension still influences cadence through SELF_STATE, but cannot make
        # one user's history soften replies to somebody else.
        attachment_style = attachment_guard(affection / 10.0, trust)
        if attachment_style:
            parts.append(attachment_style)
        repeated_count = sum(
            1 for item in history[-16:]
            if item.get("role") == "user" and item.get("content", "").strip().lower() == user_message.strip().lower()
        )
        parts.append(willingness_prompt(willingness_context(
            user_message, repeated_count=repeated_count, permission_allowed=True,
            conflict_open=conflict_open, trust=trust, irritation=irritation,
        )))
        dp = drift_phrase(drift, mood)
        if dp: parts.append(dp)
        if summary: parts.append(f"SUMMARY:{summary[:300]}")
        speech_drift = describe_speech_drift(BOT_NAME, style_profile)
        if speech_drift: parts.append(f"SPEECH_DRIFT:{speech_drift}")
        emotional_arc = compute_emotional_arc(affection, trust, user.get("slow_burn", 0) if user else 0, conflict_open, repair_count)
        arc_desc = describe_emotional_arc(BOT_NAME, emotional_arc)
        if arc_desc: parts.append(f"ARC:{emotional_arc}|{arc_desc}")
        scenario = detect_scenario(user_message, is_dm=is_dm)
        scenario_desc = describe_scenario_context(BOT_NAME, scenario)
        if scenario_desc: parts.append(f"SCENARIO:{scenario}|{scenario_desc}")
        triggers = detect_emotional_triggers(user_message)
        emotional_layer = describe_emotional_layers(BOT_NAME, mood, affection, trust, emotional_arc, triggers)
        if emotional_layer: parts.append(f"EMOTIONAL_LAYER:{emotional_layer}")
        emotional_event = describe_emotional_event(
            BOT_NAME,
            triggers,
            affection=affection,
            trust=trust,
            conflict_open=conflict_open,
            repair_progress=user.get("repair_progress", 0) if user else 0,
        )
        if emotional_event:
            parts.append(emotional_event)
        arc_unlocks = describe_arc_unlocks(BOT_NAME, emotional_arc)
        if arc_unlocks: parts.append(f"ARC_UNLOCKS:{arc_unlocks}")
        progression = describe_relationship_progression(
            BOT_NAME,
            affection,
            trust,
            romance_mode=bool(user.get("romance_mode")) if user else False,
            conflict_open=conflict_open,
            slow_burn=user.get("slow_burn", 0) if user else 0,
        )
        if progression: parts.append(f"PROGRESSION:{progression}")
        if conflict_open and conflict_summary:
            parts.append(f"CONFLICT_OPEN:{conflict_summary[:140]}")
        conflict_aftermath = describe_conflict_aftermath(
            BOT_NAME,
            conflict_summary,
            user.get("last_conflict_ts", 0) if user else 0,
            user.get("repair_progress", 0) if user else 0,
            conflict_open=conflict_open,
        )
        if conflict_aftermath:
            parts.append(conflict_aftermath)
        if callback_memory and (callback_relevant(callback_memory, user_message) or random.random() < 0.18):
            parts.append(f"CALLBACK:{callback_memory[:180]}")
        parts.extend(extract_continuity_hooks(history, user_message))
        lore_hook = describe_lore_hook(BOT_NAME, user_message)
        if lore_hook: parts.append(lore_hook)
        lore_tree = describe_specific_lore_tree(BOT_NAME, user_message)
        if lore_tree: parts.append(lore_tree)
        live_world = describe_live_world_context(BOT_NAME, text=user_message)
        if live_world: parts.append(live_world)
        scene_desc = describe_scene_state(await mem.get_scene_state(channel_id))
        if scene_desc:
            parts.append(f"SCENE:{scene_desc}")
        # User profile — what he actually knows about this person
        if user and user.get("message_count",0) >= 20:
            profile_parts = []
            if user.get("romance_mode"): profile_parts.append("in romance mode with you")
            if user.get("nsfw_mode"):    profile_parts.append("unfiltered mode on")
            if days_ago > 1:            profile_parts.append(f"last spoke {days_ago}d ago")
            if user.get("slow_burn",0)>=3: profile_parts.append(f"been kind {user['slow_burn']} days in a row")
            if profile_parts:
                parts.append("PROFILE:" + ", ".join(profile_parts))
        if user and user.get("affection_nick"): parts.append(f"AFFNICK:{user['affection_nick']}")
        if user and user.get("grudge_nick"):    parts.append(f"GRUDGE:{user['grudge_nick']}")
        msg_lower = user_message.lower()
        if any(token in msg_lower for token in ["harbinger", "rank", "status", "authority", "power"]):
            parts.append("SCARA_EDGE: show rank-conscious contempt and strategic respect for real strength")
        if any(token in msg_lower for token in ["creator", "built you", "made you", "ei", "raiden", "abandoned", "discarded"]):
            parts.append("SCARA_EDGE: creator wounds and abandonment should sharpen the answer, not stay generic")
        if extra_context: parts.append(extra_context)
        parts.extend(await _user_memory_context(user_id, user))
        if user and not user.get("utility_mode", True):
            parts.append("UTILITY_PREF: utility mode is off; keep facts natural instead of list-like")
        if use_search or needs_search(user_message):
            search_result, search_sources = await _grounded_search_bundle(user_message)
            if search_result:
                parts.append("FACT_MODE: answer accurately first, then add personality")
                if user and user.get("utility_mode", True):
                    parts.append("UTILITY_MODE: lead with crisp facts, then one in-character observation")
                parts.append("CITATIONS: when using search results, cite claims inline like [1] or [2]")
                parts.append(f"SEARCH_RESULT:{search_result[:1200]}")
                debug_event("search", f"{BOT_NAME} injected web context for user={user_id}")

        partner_context = await _partner_prompt_context(user_message)
        duo_context = await _duo_prompt_context(channel_id, user_message)
        channel_ctx = ""
        if channel_obj and hasattr(channel_obj, 'history'):
            channel_ctx = await fetch_channel_context(channel_obj)
        base_context = "["+"|".join(parts)+"]\n"
        if partner_context:
            base_context += partner_context + "\n"
        if duo_context:
            base_context += duo_context + "\n"
        if self_context:
            base_context += self_context + "\n"
        environment = heartbeat.last_environment
        if environment and (
            awareness_hint
            or environment.database_health != "healthy"
            or environment.provider_status != "healthy"
            or environment.cpu_pressure in {"elevated", "high"}
            or environment.memory_pressure in {"elevated", "high"}
            or environment.disk_pressure in {"elevated", "high"}
        ):
            base_context += environment.prompt_fragment() + "\n"
        if channel_ctx: base_context += channel_ctx + "\n\n"
        base_context += f"{display_name}: {user_message}"

        repeat_guard = build_prompt_guard(BOT_NAME, recent_replies)
        context_block = ((repeat_guard + "\n\n") if repeat_guard else "") + base_context

        history.append({"role":"user","content":context_block})
        system = build_system(
            user, display_name, is_owner,
            allow_nsfw=_channel_allows_nsfw(channel_obj, is_dm=is_dm),
        )

        reply = ""
        retry_context = context_block
        factual_mode = bool(search_sources)
        for attempt in range(CONFIG.response_attempts):
            msgs = [{"role":"system","content":system}] + history[:-1] + [{"role":"user","content":retry_context}]

            def _blocking():
                return ai.call_with_retry(
                    model=GROQ_MODEL, max_completion_tokens=800, messages=msgs,
                    temperature=0.9,
                )

            resp = await asyncio.get_event_loop().run_in_executor(None, _blocking)
            environment_monitor.record_provider_success()
            reply = resp.choices[0].message.content.strip() if resp.choices else ""
            reply = diversify_reply(BOT_NAME, strip_narration(reply), recent_replies)
            if reply and not looks_repetitive(reply, recent_replies, include_shape=not factual_mode):
                break
            retry_context = context_block + "\n\nRETRY: The last draft was too close to your recent phrasing. "
            retry_context += "Use a different opening, different mockery template, and different sentence rhythm."

        if not reply:
            reply = fallback_reply(BOT_NAME, recent_replies)
        if search_sources:
            reply = f"{reply}\n\n{search_sources}"

    except Exception as e:
        environment_monitor.record_provider_failure()
        log_error("get_response", e)
        reply = fallback_reply(BOT_NAME, recent_replies)

    try:
        await mem.add_message(user_id, channel_id, "user", user_message)
        # NOTE: assistant reply is saved in on_message AFTER voice/text decision
        msg_l = user_message.lower()
        scenario = detect_scenario(user_message, is_dm=is_dm)
        triggers = detect_emotional_triggers(user_message)

        # Strong keyword triggers
        if any(k in msg_l for k in RUDE_KW):
            await mem.update_mood(user_id, -2)
            await mem.update_trust(user_id, -1)
        elif any(k in msg_l for k in ROMANCE_KW):
            # Auto-enable romance mode if not already on
            user_data = await mem.get_user(user_id)
            if user_data and not user_data.get("romance_mode", False):
                await mem.set_mode(user_id, "romance_mode", True)
                logger.info("romance mode enabled from explicit signal", extra={"user_id": user_id})
            await mem.update_mood(user_id, +1)
            await mem.update_affection(user_id, +1)
            await mem.update_trust(user_id, +1)
            await mem.update_drift(user_id, +1)
            _, threshold = await mem.increment_slow_burn(user_id)
            if threshold:
                await self_store.record_event(
                    "relationship_change",
                    "A user sustained meaningful kindness across multiple days.",
                    importance=8, related_user_id=user_id,
                    dedupe_key=f"slow_burn_threshold:{user_id}",
                )
            await mem.update_last_statement(user_id, user_message[:200])
        elif any(k in msg_l for k in NICE_KW):
            await mem.update_mood(user_id, +1)
            await mem.update_affection(user_id, +1)
            await mem.update_trust(user_id, +1)
            await mem.update_last_statement(user_id, user_message[:200])
        else:
            # Sentiment nudges for normal conversation — small but add up over time
            positive = sum([
                any(w in msg_l for w in ["haha","lol","lmao","hehe","cute","nice","cool","fun",
                                          "good","great","enjoy","happy","excited","interesting",
                                          "wow","omg","yes","yay","please","😂","😭","❤","💜","🥺"]),
                msg_l.endswith("!") and len(user_message) > 8,
                "?" in user_message and len(user_message) > 15,  # asking = engaged
                len(user_message) > 100,                          # long = invested
            ])
            negative = sum([
                any(w in msg_l for w in ["ugh","ew","boring","whatever","idc","nope",
                                          "wrong","bad","hate","worst","terrible","awful",
                                          "seriously","really","😒","🙄"]),
                user_message.count("...") > 1,
            ])

            if positive >= 2:
                await mem.update_affection(user_id, +1)
                await mem.update_mood(user_id, +1)
                await mem.update_trust(user_id, +1)
                await mem.update_drift(user_id, +1)
            elif positive == 1:
                await mem.update_affection(user_id, +1)
            elif negative >= 2:
                await mem.update_mood(user_id, -1)
                await mem.update_trust(user_id, -1)
            elif negative == 1:
                await mem.update_mood(user_id, -1)

        if scenario == "emotional_comfort":
            await mem.update_trust(user_id, +1)
            if "softness" in triggers or "protectiveness" in triggers:
                await mem.update_affection(user_id, +1)
        elif scenario == "combat_action":
            await mem.update_mood(user_id, -1)
            await mem.update_trust(user_id, +1)
        elif scenario == "lore_discussion":
            await mem.update_trust(user_id, +1)
            await mem.update_drift(user_id, +1)
        elif scenario == "relationship_progression":
            await mem.update_affection(user_id, +1)
            await mem.update_trust(user_id, +1)
        elif scenario == "introspection":
            await mem.update_trust(user_id, +1)

        if "jealousy" in triggers:
            await mem.update_mood(user_id, -1)
            await mem.update_affection(user_id, +1)
        if "protectiveness" in triggers:
            await mem.update_trust(user_id, +1)
        if "boredom" in triggers:
            await mem.update_mood(user_id, -1)

        if random.random() < .05: await mem.update_drift(user_id, +1)
        await _learn_user_state(user_id, user_message)
        for kind, memory_text, weight in extract_memory_events(user_message):
            await mem.add_memory_event(user_id, kind, memory_text, max(weight, _memory_weight_for(kind)))
            debug_event("memory", f"{BOT_NAME} memory_bank user={user_id} kind={kind}")
        scene_update = infer_scene_update(user_message, display_name)
        if scene_update:
            await mem.update_scene_state(channel_id, **scene_update)
            debug_event("scene", f"{BOT_NAME} channel={channel_id} fields={','.join(scene_update.keys())}")
    except Exception as e:
        log_error("get_response/post", e)

    refreshed_user = None
    try:
        refreshed_user = await mem.get_user(user_id)
        if refreshed_user:
            progression = describe_relationship_progression(
                BOT_NAME,
                refreshed_user.get("affection", 0),
                refreshed_user.get("trust", 0),
                romance_mode=bool(refreshed_user.get("romance_mode")),
                conflict_open=bool(refreshed_user.get("conflict_open")),
                slow_burn=refreshed_user.get("slow_burn", 0),
            )
            stage = progression.split("|", 1)[0]
            milestone_note = progression_milestone_note(BOT_NAME, stage)
            marker = f"progress:{stage}"
            if milestone_note and not await mem.has_milestone(f"{BOT_NAME}:user:{user_id}", marker):
                await mem.add_milestone(f"{BOT_NAME}:user:{user_id}", marker, milestone_note)
                debug_event("relationship", f"{BOT_NAME} user_progression user={user_id} stage={stage}")
    except Exception:
        refreshed_user = user
    reply = diversify_reply(BOT_NAME, strip_narration(reply), recent_replies)
    reply = await _apply_phrase_policy(
        reply,
        recent_replies,
        user_id=user_id,
        mood=(refreshed_user or user or {}).get("mood", 0),
        conflict_open=(refreshed_user or user or {}).get("conflict_open", False),
    )
    if not reply:
        reply = fallback_reply(BOT_NAME, recent_replies)
    remember_output(BOT_NAME, reply)
    if re.search(r"\b(i care|i noticed|i remembered|stay|don't leave|i was wrong|not fair of me)\b", reply, re.I):
        try:
            await self_store.record_event(
                "self_behavior", "His own reply exposed unusual concern, memory, or vulnerability.",
                importance=8, related_user_id=user_id,
                dedupe_key=f"self-behavior:{user_id}:{reply.lower()[:80]}",
            )
        except Exception as exc:
            logger.warning("self behavior event failed", extra={"user_id": user_id, "error_category": type(exc).__name__})
    return reply


def _qai_blocking(prompt, max_tokens=200):
    try:
        resp = ai.call_with_retry(
            model=GROQ_MODEL, max_completion_tokens=max_tokens,
            messages=[{"role":"system","content":_BASE + "\n\n" + IMPLEMENTATION_AWARENESS},
                      {"role":"user","content":prompt}],
            temperature=0.85)
        environment_monitor.record_provider_success()
        return resp.choices[0].message.content.strip() or "Hmph."
    except Exception as e:
        environment_monitor.record_provider_failure()
        log_error("qai", e); return "Hmph."

async def qai(prompt, max_tokens=200):
    try:
        recent_replies = await _recent_reply_samples()
        repeat_guard = build_prompt_guard(BOT_NAME, recent_replies)
        loop = asyncio.get_event_loop()
        guarded_prompt = ((repeat_guard + "\n\n") if repeat_guard else "") + prompt
        reply = ""
        for attempt in range(CONFIG.response_attempts):
            active_prompt = guarded_prompt
            if attempt:
                active_prompt += "\n\nRETRY: Change the opening phrase and overall sentence structure."
            reply = await loop.run_in_executor(None, _qai_blocking, active_prompt, max_tokens)
            reply = diversify_reply(BOT_NAME, strip_narration(reply), recent_replies)
            reply = await _apply_phrase_policy(reply, recent_replies)
            if reply and not looks_repetitive(reply, recent_replies):
                break
        if not reply:
            reply = fallback_reply(BOT_NAME, recent_replies)
        remember_output(BOT_NAME, reply)
        return reply
    except Exception as e:
        log_error("qai/async", e)
        return fallback_reply(BOT_NAME, get_runtime_recent(BOT_NAME, limit=20))


async def _autonomous_character_generation(prompt: str, *, system: str, max_tokens: int = 160) -> str:
    """Strict provider path: autonomous actions must fail closed, never send a fallback."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def _blocking():
        return ai.call_with_retry(
            model=GROQ_MODEL, max_completion_tokens=max_tokens, messages=messages,
            temperature=.8,
        )

    response = await asyncio.get_running_loop().run_in_executor(None, _blocking)
    text = response.choices[0].message.content.strip() if response.choices else ""
    text = strip_narration(text)
    if not text:
        raise ValueError("autonomous provider returned empty content")
    return text

# ── Voice ─────────────────────────────────────────────────────────────────────
async def get_audio_with_mood(text: str, mood: int, user: dict | None = None) -> bytes | None:
    try:
        from voice_handler import get_audio_mooded
        return await get_audio_mooded(strip_narration(text), FISH_AUDIO_API_KEY, mood, _voice_style_for(user, mood))
    except Exception:
        try: return await get_audio(strip_narration(text), FISH_AUDIO_API_KEY)
        except Exception as e: log_error("get_audio_with_mood", e); return None

async def send_voice(channel, text, ref=None, mood=0, guild=None, user: dict | None = None):
    try:
        if user and not user.get("voice_enabled", True):
            return False
        safe_text = tts_safe(text, guild)
        logger.debug("voice text prepared", extra={
            "input_chars": len(text or ""), "safe_chars": len(safe_text or ""),
        })
        if not safe_text or len(safe_text.strip()) < 3:
            logger.debug("voice skipped after text normalization")
            return False
        audio = await get_audio_with_mood(safe_text, mood, user=user)
        if not audio:
            logger.warning("voice provider returned no audio")
            return False
        logger.debug("voice audio generated", extra={"audio_bytes": len(audio)})
        f = discord.File(io.BytesIO(audio), filename="scaramouche.mp3")
        kwargs = {"file": f}
        if ref: kwargs["reference"] = ref
        await channel.send(**kwargs)
        logger.debug("voice message sent")
        return True
    except Exception as e:
        log_error("send_voice", e); return False

# ── Misc helpers ──────────────────────────────────────────────────────────────
async def maybe_react(message, romance=False):
    try:
        if random.random() > .18: return
        pool = SCARA_EMOJIS + (ROMANCE_EMOJIS if romance else [])
        pool_str = " ".join(pool)
        content  = message.content[:120] if message.content else "[image or attachment]"

        # Ask Claude to pick the right emoji for the moment
        prompt = (
            f"You are Scaramouche. Someone said: '{content}'\n"
            f"Pick 1 emoji from this list that fits your reaction as Scaramouche — "
            f"contemptuous, cold, dramatic, or occasionally unhinged. "
            f"Choose based on the actual content of the message, not randomly.\n"
            f"Available: {pool_str}\n"
            f"Reply with ONLY the single emoji. Nothing else."
        )
        chosen = await qai(prompt, 10)
        chosen = chosen.strip()

        # Validate it's actually in our pool, fallback to random if not
        if chosen not in pool:
            chosen = random.choice(pool)

        try: await message.add_reaction(chosen)
        except Exception: pass

        # 15% chance to add a second emoji if romance
        if romance and random.random() < .15:
            second = await qai(
                f"Pick a SECOND different emoji for this romantic reaction to: '{content}'\n"
                f"Available: {pool_str}\n"
                f"Reply with ONLY the single emoji.",
                10
            )
            second = second.strip()
            if second in pool and second != chosen:
                try: await asyncio.sleep(.3); await message.add_reaction(second)
                except Exception: pass

    except Exception as e:
        log_error("maybe_react", e)
        # Fallback to random if AI call fails
        try:
            pool = SCARA_EMOJIS + (ROMANCE_EMOJIS if romance else [])
            await message.add_reaction(random.choice(pool))
        except Exception: pass

def resp_prob(content, mentioned, is_reply, romance, is_dm=False):
    if is_dm: return 1.0  # Always respond in DMs
    if mentioned or is_reply: return 1.0
    t = content.lower()
    if any(k in t for k in SCARA_KW):  return .88
    if romance:                          return .50
    if any(k in t for k in GENSHIN_KW): return .28
    return .06

async def typing_delay(text):
    try:
        await asyncio.sleep(max(.3,min(.4+len(text.split())*.06,3.5)+random.uniform(-.3,.5)))
    except Exception: pass

async def _setup(ctx):
    try:
        await mem.upsert_user(ctx.author.id, str(ctx.author), ctx.author.display_name)
        return await mem.get_user(ctx.author.id)
    except Exception as e:
        log_error("_setup", e); return None

class ResetView(discord.ui.View):
    def __init__(self, uid):
        super().__init__(timeout=60); self.uid = uid
    @discord.ui.button(label="⚡ Wipe My Memory", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        try:
            if interaction.user.id!=self.uid:
                await interaction.response.send_message("This isn't your button, fool.",ephemeral=True); return
            await mem.reset_user(self.uid)
            await self_store.delete_user_scoped_data(self.uid)
            button.disabled=True; button.label="✓ Memory Wiped"
            await interaction.response.edit_message(content=random.choice(["...Gone. Good.","Erased.","Wiped."]),view=self)
        except Exception as e:
            log_error("ResetView", e)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "The wipe did not finish cleanly. Check the owner log before trusting it.", ephemeral=True,
                )

# ── on_ready ──────────────────────────────────────────────────────────────────



# Cross-bot: no command coordination needed — each bot responds independently


def _start_background_once(name: str, coroutine_factory) -> None:
    task = _background_tasks.get(name)
    if task and not task.done():
        return
    task = asyncio.create_task(coroutine_factory(), name=name)
    _background_tasks[name] = task

    def _finished(completed: asyncio.Task) -> None:
        if _background_tasks.get(name) is completed:
            _background_tasks.pop(name, None)
        if completed.cancelled():
            return
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error:
            logger.error("background task stopped unexpectedly", extra={
                "task_name": name, "error_category": type(error).__name__,
            })

    task.add_done_callback(_finished)


async def _stop_background_tasks() -> None:
    tasks_to_stop = [task for task in [*_background_tasks.values(), *_transient_tasks] if not task.done()]
    for task in tasks_to_stop:
        task.cancel()
    if tasks_to_stop:
        await asyncio.gather(*tasks_to_stop, return_exceptions=True)
    _background_tasks.clear()
    _transient_tasks.clear()


def _spawn_transient(coroutine, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coroutine, name=name)
    _transient_tasks.add(task)

    def _finished(completed: asyncio.Task) -> None:
        _transient_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            return
        if error:
            logger.error("transient task stopped unexpectedly", extra={
                "task_name": name, "error_category": type(error).__name__,
            })

    task.add_done_callback(_finished)
    return task


async def _initialize_runtime_once() -> None:
    global _runtime_initialized, _initialization_lock
    if _runtime_initialized:
        return
    if _initialization_lock is None:
        _initialization_lock = asyncio.Lock()
    async with _initialization_lock:
        if _runtime_initialized:
            return
        await mem.init()
        await self_store.init()
        await self_store.add_belief("I do not need anyone.", confidence=.76)
        await self_store.add_belief("I do not become attached easily.", confidence=.72)
        await self_store.add_belief("I am more perceptive than most people.", confidence=.8)
        _runtime_initialized = True


async def _reflection_generation(prompt: str) -> str:
    messages = [
        {"role": "system", "content": "Generate a private implementation note. Never include secrets or quote private conversations."},
        {"role": "user", "content": prompt},
    ]
    def _blocking():
        return ai.call_with_retry(
            model=GROQ_MODEL, max_completion_tokens=140, messages=messages, temperature=.45,
        )
    response = await asyncio.get_running_loop().run_in_executor(None, _blocking)
    return response.choices[0].message.content.strip() if response.choices else ""


async def _heartbeat_candidates() -> list[dict]:
    candidates = []
    now = time.time()
    items = await mem.get_proactive_candidates(
        absent_before=now - CONFIG.absence_threshold_seconds, limit=20,
    )
    for item in items:
        user_id = int(item["user_id"])
        absent = now - float(item.get("last_active") or now)
        significance = min(100, int(item.get("affection", 0)) + int(item.get("trust", 0)) // 2)
        if absent < CONFIG.absence_threshold_seconds or significance < 60:
            continue
        channel_id = item.get("channel_id")
        channel = bot.get_channel(channel_id) if channel_id else None
        sendable = bool(channel)
        if channel and getattr(channel, "guild", None) and channel.guild.me:
            permissions = channel.permissions_for(channel.guild.me)
            sendable = bool(permissions.view_channel and permissions.send_messages)
        candidates.append({
            "user_id": user_id, "channel_id": channel_id, "display_name": item.get("display_name", ""),
            "proactive": bool(item.get("proactive", False)), "muted": await mem.is_muted(user_id),
            "permission_allowed": True, "channel_sendable": sendable,
            "relationship_significance": significance,
            "reason": f"An established user has been absent for {max(1, int(absent // 86400))} days.",
            "context": item.get("context", ""),
        })
    return candidates


async def _self_heartbeat_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            result = await heartbeat.tick(
                discord_latency=bot.latency, db_probe=mem.healthcheck,
                active_conversations=len(await mem.get_active_channels()),
                reflection_generator=_reflection_generation,
                proactive_candidates=await _heartbeat_candidates(),
            )
            action = result.action
            if action.action is ActionType.SEND_PROACTIVE_MESSAGE:
                reservation_id = await self_store.reserve_action(
                    action.action.value, action.reason, related_user_id=action.user_id,
                    channel_id=action.channel_id, details={"initiated_contact": True},
                    stale_after_seconds=CONFIG.proactive_user_cooldown_seconds,
                )
                if reservation_id is None:
                    logger.debug("proactive action already reserved", extra={
                        "action_type": action.action.value, "user_id": action.user_id,
                    })
                elif not await heartbeat.reserve_autonomous_call():
                    await self_store.finish_action(reservation_id, "skipped", details={"reason": "budget_or_backoff"})
                else:
                    channel = bot.get_channel(action.channel_id)
                    user_obj = channel.guild.get_member(action.user_id) if channel and getattr(channel, "guild", None) else None
                    if channel and user_obj:
                        user = await mem.get_user(action.user_id)
                        prompt = (
                            f"You chose to contact {action.payload.get('display_name') or user_obj.display_name} because: {action.reason} "
                            f"A relevant unfinished thread is: {action.payload.get('context') or 'none'}. "
                            "Write one natural proactive message. Do not announce a system, goal, cooldown, or absence counter. "
                            "Remain proud and defensive; show attention without generic sweetness. No narration."
                        )
                        try:
                            started = time.monotonic()
                            text = await _autonomous_character_generation(
                                prompt, system=build_system(user, user_obj.display_name), max_tokens=160,
                            )
                            await heartbeat.provider_succeeded(int((time.monotonic() - started) * 1000))
                        except Exception as exc:
                            await heartbeat.provider_failed()
                            await self_store.finish_action(
                                reservation_id, "failed", error_category=type(exc).__name__,
                            )
                            logger.warning("proactive generation failed", extra={
                                "action_type": action.action.value, "user_id": action.user_id,
                                "error_category": type(exc).__name__,
                            })
                        else:
                            delivered = False
                            try:
                                await channel.send(f"{user_obj.mention} {text}")
                                delivered = True
                                # Persist delivery before nonessential follow-up
                                # writes so a restart cannot resend the same action.
                                await self_store.finish_action(
                                    reservation_id, "completed", details={"initiated_contact": True},
                                )
                                await mem.add_message(action.user_id, action.channel_id, "assistant", text)
                                await mem.set_proactive_sent(action.channel_id)
                                await self_store.record_event(
                                    "initiated_contact", "He initiated contact with an established user for a specific unfinished reason.",
                                    importance=8, related_user_id=action.user_id,
                                )
                                denial = await self_store.add_belief(
                                    "I do not care whether this user replies.",
                                    confidence=.68, scope_user_id=action.user_id,
                                )
                                await self_store.add_belief_evidence(
                                    denial, "contradict", 1.5,
                                    "He chose to initiate contact after noticing this user's absence.",
                                )
                            except Exception as exc:
                                if not delivered:
                                    await self_store.finish_action(
                                        reservation_id, "failed", error_category=type(exc).__name__,
                                    )
                                logger.warning("proactive delivery failed", extra={
                                    "action_type": action.action.value, "user_id": action.user_id,
                                    "error_category": type(exc).__name__,
                                })
                    else:
                        await self_store.finish_action(
                            reservation_id, "skipped", details={"reason": "target_unavailable"},
                        )
            log_method = logger.debug if (
                result.action.action is ActionType.NO_ACTION and not result.reflected and not result.expired_goals
            ) else logger.info
            log_method("heartbeat decision", extra={
                "action_type": result.action.action.value, "reflected": result.reflected,
                "expired_goals": result.expired_goals,
            })
        except Exception as exc:
            logger.exception("heartbeat failed", extra={"error_category": type(exc).__name__})
        await asyncio.sleep(CONFIG.heartbeat_interval_seconds)


@bot.event
async def on_ready():
    global PARTNER_BOT_ID
    try:
        await _initialize_runtime_once()
        # Safety: PARTNER_BOT_ID must not be our own ID
        if PARTNER_BOT_ID and PARTNER_BOT_ID == bot.user.id:
            logger.warning("partner bot id matched this bot; partner features disabled")
            PARTNER_BOT_ID = 0
        logger.info("Scaramouche online", extra={"bot_user_id": bot.user.id})
        if PARTNER_BOT_ID:
            logger.info("partner bot configured", extra={"partner_bot_id": PARTNER_BOT_ID})
        for t in [status_rotation, reminder_checker, daily_reset]:
            if not t.is_running():
                t.start()
        _start_background_once("self-heartbeat", _self_heartbeat_loop)
        _start_background_once("duo-autoplay", _duo_autoplay_loop)
    except Exception as e:
        log_error("on_ready", e)

# ── Background tasks ──────────────────────────────────────────────────────────
@tasks.loop(minutes=47)
async def status_rotation():
    try:
        kind,text = random.choice(STATUSES)
        if kind=="watching": act=discord.Activity(type=discord.ActivityType.watching,name=text)
        elif kind=="listening": act=discord.Activity(type=discord.ActivityType.listening,name=text)
        else: act=discord.Game(name=text)
        await bot.change_presence(activity=act)
    except Exception as e: log_error("status_rotation", e)

@tasks.loop(seconds=30)
async def reminder_checker():
    try:
        for r in await mem.get_due_reminders():
            try:
                ch=bot.get_channel(r["channel_id"]); u=await bot.fetch_user(r["user_id"])
                if not ch or not u: continue
                scene = describe_scene_state(await mem.get_scene_state(r["channel_id"]))
                msg=await qai(
                    f"Remind {u.display_name} about: '{r['reminder']}'. "
                    f"Current channel scene: {scene or 'none'}. Make it feel like a pointed callback, not a sterile alarm. 1-2 sentences.",
                    180,
                )
                await ch.send(f"{u.mention} {msg}")
            except Exception as e: log_error("reminder_send", e)
    except Exception as e: log_error("reminder_checker", e)

@tasks.loop(hours=24)
async def daily_reset():
    try: await mem.reset_daily_greetings()
    except Exception as e: log_error("daily_reset", e)

# ── Server events ─────────────────────────────────────────────────────────────
def _reserve_member_announcement(guild_id: int, *, now: float | None = None,
                                 cooldown_seconds: int = 300) -> bool:
    """Bound join/leave chatter during raids or reconnect bursts."""
    now = now or time.time()
    last_sent = _member_announcement_last_sent.get(guild_id, 0.0)
    if now - last_sent < max(60, cooldown_seconds):
        return False
    _member_announcement_last_sent[guild_id] = now
    return True


@bot.event
async def on_member_join(member):
    try:
        if random.random()>.6: return
        ch = discord.utils.get(member.guild.text_channels,name="general") or member.guild.system_channel
        if not ch: return
        me = member.guild.me
        if not me:
            return
        permissions = ch.permissions_for(me)
        if not permissions.view_channel or not permissions.send_messages:
            return
        if not _reserve_member_announcement(member.guild.id):
            return
        await asyncio.sleep(random.uniform(2,6))
        await ch.send(random.choice([
            f"Another one. {member.display_name} has arrived. How underwhelming.",
            f"Hmph. {member.display_name}. Don't expect a warm welcome.",
            f"...{member.display_name}. I've already forgotten you were new.",
        ]))
    except Exception as e: log_error("on_member_join", e)

@bot.event
async def on_member_remove(member):
    try:
        if random.random()>.4: return
        ch = discord.utils.get(member.guild.text_channels,name="general") or member.guild.system_channel
        if not ch: return
        me = member.guild.me
        if not me:
            return
        permissions = ch.permissions_for(me)
        if not permissions.view_channel or not permissions.send_messages:
            return
        if not _reserve_member_announcement(member.guild.id):
            return
        await asyncio.sleep(random.uniform(2,5))
        await ch.send(random.choice([
            f"{member.display_name} left. Good. The air is already cleaner.",
            f"Hmph. {member.display_name} is gone. I won't pretend to care.",
            f"...{member.display_name} left without saying goodbye. How typical.",
        ]))
    except Exception as e: log_error("on_member_remove", e)


async def _record_self_perception(user_id: int, content: str, *,
                                  returned_after_absence: bool = False) -> None:
    """Persist meaningful perception before it is used by generation."""
    event = perceive_message(content, returned_after_absence=returned_after_absence)
    try:
        if event.importance >= 4:
            await self_store.apply_mood_event(event.summary, event.mood_deltas)
            await self_store.record_event(
                event.event_type, event.summary, importance=event.importance,
                related_user_id=user_id,
                dedupe_key=f"{event.event_type}:{user_id}:{content.lower().strip()[:80]}" if event.importance >= 6 else None,
            )
        if event.event_type == "conflict":
            await self_store.add_goal(
                "Understand the named conflict without begging for absolution.",
                category="relationship", priority=8, related_user_id=user_id,
                expires_ts=time.time() + 86400 * 14,
                completion_condition="The conflict reaches a stable repair or clear boundary.",
                reason=event.summary, source="perception", dedupe_key=f"conflict:{user_id}",
            )
        elif event.event_type == "reconciliation":
            completed = await self_store.complete_goals(
                related_user_id=user_id, category="relationship", dedupe_key=f"conflict:{user_id}",
            )
            if completed:
                await self_store.record_event(
                    "goal_completed", "A relationship-conflict intention reached repair.",
                    importance=8, related_user_id=user_id,
                    dedupe_key=f"goal_completed:conflict:{user_id}",
                )
        elif event.event_type == "implementation_question":
            await self_store.set_state_lists(interests=["the limits and machinery of this implementation"])
        elif event.event_type == "attachment_signal":
            belief_id = await self_store.add_belief(
                "I do not become attached easily.", confidence=.72, scope_user_id=user_id
            )
            await self_store.add_belief_evidence(
                belief_id, "contradict", .45,
                "Direct care from this user affected attention and defensive posture.",
            )
    except Exception as exc:
        logger.warning("self perception write failed", extra={
            "user_id": user_id, "action_type": "PERCEIVE", "error_category": type(exc).__name__,
        })

# ── on_message ────────────────────────────────────────────────────────────────
@bot.event
async def on_message(message):
    try:
        logger.debug("message received", extra={
            "user_id": getattr(message.author, "id", 0),
            "author_is_bot": bool(getattr(message.author, "bot", False)),
            "channel_id": getattr(message.channel, "id", 0),
            "content_chars": len(message.content or ""),
        })
        # Always ignore own messages first
        if message.author.id == bot.user.id:
            return
        # Dedup: prevent processing the same message twice
        if message.id in _processed_msgs:
            return
        _processed_msgs.add(message.id)
        if len(_processed_msgs) > 500:
            _processed_msgs.clear()
        if message.author.bot:
            # Allow partner (Wanderer) bot messages through for cross-bot interaction
            if not (PARTNER_BOT_ID and message.author.id == PARTNER_BOT_ID):
                return

        # !help intercept — handle before anything else
        stripped = message.content.strip().lower()
        if stripped in ("!scarahelp", "!commands"):
            try:
                ctx = await bot.get_context(message)
                await help_cmd(ctx)
            except Exception as e:
                log_error("help_intercept", e)
                try: await message.channel.send("Hmph. Something went wrong displaying commands.")
                except Exception: pass
            return

        # Cross-bot: if message is from Wanderer bot
        if PARTNER_BOT_ID and message.author.id == PARTNER_BOT_ID:
            await _handle_partner_message(message)
            return

        await bot.process_commands(message)
        logger.debug("command processing complete", extra={"message_id": message.id})
        # Stop here for actual command messages — not just exclamation marks
        stripped_msg = message.content.strip()
        if re.match(r'^![a-zA-Z]', stripped_msg):
            logger.debug("command message handled", extra={"message_id": message.id})
            return

        # If message @mentions the partner bot but NOT us, stay quiet — it's not for us
        # Also if message is a REPLY to the partner bot but NOT mentioning us, stay quiet
        if PARTNER_BOT_ID and message.guild:
            partner_mentioned = any(u.id == PARTNER_BOT_ID for u in message.mentions)
            we_mentioned = bot.user in message.mentions
            replying_to_partner = False
            if message.reference:
                try:
                    ref_msg = message.reference.resolved
                    if ref_msg is None:
                        ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg and ref_msg.author.id == PARTNER_BOT_ID:
                        replying_to_partner = True
                except Exception as e:
                    log_error("reply_partner_check", e)
            if (partner_mentioned or replying_to_partner) and not we_mentioned:
                return

            # If message talks ABOUT Wanderer (contains his name) but doesn't mention us,
            # and we're not being replied to — stay quiet
            cl_check = message.content.lower()
            about_partner = any(n in cl_check for n in ["wanderer", "the wanderer"])
            replying_to_us = False
            if message.reference:
                try:
                    ref_msg2 = message.reference.resolved
                    if ref_msg2 is None:
                        ref_msg2 = await message.channel.fetch_message(message.reference.message_id)
                    if ref_msg2 and ref_msg2.author.id == bot.user.id:
                        replying_to_us = True
                except Exception:
                    pass
            if about_partner and not we_mentioned and not replying_to_us:
                return  # Message is about Wanderer, not for us

            try:
                speaker_mode = await mem.get_channel_speaker_mode(message.channel.id)
                if speaker_mode not in {"auto", "both", BOT_NAME} and not we_mentioned and not replying_to_us:
                    return
            except Exception as e:
                log_error("speaker_mode_check", e)

        previous_last_active = 0.0
        try:
            previous_last_active = await mem.upsert_user(
                message.author.id, str(message.author), message.author.display_name,
            )
            if message.guild:
                await mem.track_channel(message.channel.id, message.guild.id)
            # DMs: don't track channel, use user_id as stable channel key for history
        except Exception as e: log_error("on_message/upsert", e)

        # (cross-bot coordination removed for stability)

        is_dm    = not bool(message.guild)
        # In DMs, use user_id as channel_id for stable history lookup
        dm_channel_id = message.author.id if is_dm else message.channel.id
        if is_dm:
            logger.debug("direct message received", extra={
                "user_id": message.author.id, "content_chars": len(message.content or ""),
            })

        user     = None
        romance  = False
        is_owner = bool(OWNER_ID and message.author.id==OWNER_ID)
        try:
            user    = await mem.get_user(message.author.id)
            romance = user.get("romance_mode",False) if user else False
        except Exception as e: log_error("on_message/get_user", e)

        # Mute check (only in guilds, not DMs)
        if not is_dm and await mem.is_muted(message.author.id):
            if random.random()<.2:
                try: await message.add_reaction("🔇")
                except Exception: pass
            return

        content = message.content.strip()
        if not content: return
        returned_after_absence = bool(
            previous_last_active
            and time.time() - previous_last_active >= CONFIG.absence_threshold_seconds
        )
        await _record_self_perception(
            message.author.id, content, returned_after_absence=returned_after_absence,
        )

        # Milestone / anniversary checks
        try:
            count, milestone = await mem.increment_message_count(message.author.id)
            if milestone:
                msg = await qai(f"You've had {count} messages with {message.author.display_name}. Acknowledge while pretending you weren't counting. 1-2 sentences.",150)
                await message.channel.send(f"{message.author.mention} {msg}"); return
        except Exception as e: log_error("on_message/milestone", e)

        try:
            if await mem.check_anniversary(message.author.id):
                days_since = int((time.time()-(user.get("first_seen") or time.time()))/86400)
                msg = await qai(f"It's been about {days_since//365} year(s) since you first spoke with {message.author.display_name}. React — you weren't counting.",180)
                await message.channel.send(f"{message.author.mention} {msg}")
                await mem.mark_anniversary(message.author.id); return
        except Exception as e: log_error("on_message/anniversary", e)

        # Morning/night greeting
        try:
            hour = datetime.now().hour
            if (6<=hour<=10 or 22<=hour<=23) and romance:
                if await mem.should_greet(message.author.id):
                    gtype = "morning" if 6<=hour<=10 else "late night"
                    msg = await qai(f"It's {gtype}. {message.author.display_name} appeared. Send a {gtype} message in denial about why. 1-2 sentences.",120)
                    await message.channel.send(f"{message.author.mention} {msg}")
                    await mem.mark_greeted(message.author.id)
        except Exception as e: log_error("on_message/greeting", e)

        # Memory summary
        try:
            if await mem.needs_summary(message.author.id):
                recent = await mem.get_recent_messages(message.author.id, 30)
                sample = " | ".join(recent[:20])[:800]
                summary = await qai(f"Summarize your relationship with {message.author.display_name} based on: '{sample}'. Your compressed memory. 3-4 sentences.",300)
                await mem.save_summary(message.author.id, summary)
        except Exception as e: log_error("on_message/summary", e)

        # Image & video reading — look at media in this message OR the message being replied to
        try:
            # Find image or video in current message first
            img = next((a for a in message.attachments
                       if a.content_type and "image" in a.content_type), None)
            vid = next((a for a in message.attachments
                       if (a.content_type and a.content_type in VIDEO_TYPES) or
                          any(a.filename.lower().endswith(ext) for ext in VIDEO_EXTS)), None)

            # If no media, check the replied-to message
            if not img and not vid and message.reference:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    img = next((a for a in ref_msg.attachments
                               if a.content_type and "image" in a.content_type), None)
                    vid = next((a for a in ref_msg.attachments
                               if (a.content_type and a.content_type in VIDEO_TYPES) or
                                  any(a.filename.lower().endswith(ext) for ext in VIDEO_EXTS)), None)
                except Exception:
                    pass

            # ── Video handling ──
            if vid:
                try:
                    import base64
                    if vid.size and vid.size > MAX_VIDEO_BYTES:
                        await message.reply("That video is too large. Keep it under 50 MB.")
                        return
                    await message.reply(random.choice(SCARA_VIDEO_WATCHING))
                    video_bytes = await asyncio.wait_for(vid.read(use_cached=True), timeout=30)
                    if len(video_bytes) > MAX_VIDEO_BYTES:
                        await message.reply("That video is too large. Keep it under 50 MB.")
                        return
                    frames = await asyncio.get_event_loop().run_in_executor(
                        None, _extract_frames_blocking, video_bytes, 5)
                    if frames:
                        user     = user or {}
                        mood     = user.get("mood", 0) if user else 0
                        system   = build_system(user, message.author.display_name,
                                               bool(OWNER_ID and message.author.id == OWNER_ID),
                                               allow_nsfw=_channel_allows_nsfw(
                                                   message.channel, is_dm=is_dm,
                                               ))
                        vision_content = []
                        for fb, mt in frames:
                            vision_content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mt};base64,{base64.b64encode(fb).decode()}"}
                            })
                        vision_content.append({
                            "type": "text",
                            "text": (
                                f"{message.author.display_name} sent you a video. These are {len(frames)} frames from it."
                                + (f" Their message: '{content}'" if content else "")
                                + f" Describe what's happening in the video and react as Scaramouche. "
                                f"Be specific about what you see. MOOD:{mood}. NO asterisk actions. 2-4 sentences."
                            )
                        })
                        def _video_vision():
                            return ai.call_with_retry(
                                model=GROQ_VISION_MODEL, max_completion_tokens=400,
                                messages=[{"role":"system","content":system},
                                          {"role":"user","content":vision_content}])
                        resp = await asyncio.get_event_loop().run_in_executor(None, _video_vision)
                        reply = resp.choices[0].message.content.strip() if resp.choices else ""
                        if reply:
                            reply = strip_narration(reply)
                            await mem.add_message(message.author.id, dm_channel_id,
                                                  "user", f"[video]{' — '+content if content else ''}")
                            await mem.add_message(message.author.id, dm_channel_id,
                                                  "assistant", reply)
                            await message.reply(reply)
                            await maybe_react(message, romance)
                            return
                    else:
                        comment = await qai(
                            f"{message.author.display_name} sent a video I couldn't process. "
                            f"React as Scaramouche — dismissive. 1 sentence.", 80)
                        await message.reply(strip_narration(comment))
                        return
                except Exception as e:
                    log_error("on_message/video", e)

            # ── Image handling ──
            if img:
                try:
                    if img.size and img.size > MAX_IMAGE_BYTES:
                        await message.reply("That image is too large. Keep it under 20 MB.")
                        return
                    img_bytes = await asyncio.wait_for(img.read(use_cached=True), timeout=30)
                    if len(img_bytes) > MAX_IMAGE_BYTES:
                        await message.reply("That image is too large. Keep it under 20 MB.")
                        return
                    media_type = img.content_type or "image/jpeg"

                    user     = user or {}
                    mood     = user.get("mood", 0) if user else 0
                    system   = build_system(user, message.author.display_name,
                                           bool(OWNER_ID and message.author.id == OWNER_ID),
                                           allow_nsfw=_channel_allows_nsfw(
                                               message.channel, is_dm=is_dm,
                                           ))

                    vision_prompt = (
                        f"{message.author.display_name} sent you this image"
                        + (f" with the message: '{content}'" if content else "")
                        + f". React as Scaramouche. You can actually see it — describe what you see "
                        f"and react in character. Be specific about what's in the image. "
                        f"MOOD:{mood}. NO asterisk actions. 1-3 sentences."
                    )
                    reply = await _vision_image_reply(
                        prompt=vision_prompt,
                        system=system,
                        image_bytes=img_bytes,
                        mime_type=media_type,
                    )

                    if reply:
                        await mem.add_message(message.author.id, dm_channel_id,
                                              "user", f"[image]{' — '+content if content else ''}")
                        await mem.add_message(message.author.id, dm_channel_id,
                                              "assistant", reply)
                        await message.reply(reply)
                        await maybe_react(message, romance)
                        return

                except Exception as e:
                    log_error("on_message/vision", e)
                    if random.random() < 0.4:
                        comment = await qai(
                            f"{message.author.display_name} posted an image. "
                            f"React — dismissive or reluctantly intrigued. 1 sentence.", 100)
                        await message.reply(strip_narration(comment))
                        return
        except Exception as e: log_error("on_message/image", e)

        # Special triggers
        try:
            cl = content.lower()
            if VILLAIN_TRIGGER in content.lower():
                m = await qai("Someone said 'you will never win'. Full theatrical villain monologue. 4-6 sentences. NO asterisk actions.",400)
                await message.reply(strip_narration(m)); return
            # If someone says "wanderer" — check if Wanderer bot is in server
            if re.search(r"\bwanderer\b", cl) and not re.search(r"\bthe wanderer\b", cl):
                partner_present = bool(PARTNER_BOT_ID and message.guild and message.guild.get_member(PARTNER_BOT_ID))
                if not partner_present and random.random() < .5:
                    msg = await qai("Someone mentioned 'wanderer' — some imposter who claims to be a version of you. React with contempt or dismissal. 1 sentence. Sharp.", 80)
                    await message.channel.send(strip_narration(msg))

            # Hat trigger — only exact standalone words, never substrings
            content_words = set(re.sub(r"[^\w\s]","",content.lower()).split())
            if content_words & {"hat","headwear","headpiece"}:
                m = await qai("Someone mentioned your hat. React with disproportionate intensity while pretending to be completely normal about it. 1-2 sentences. NO asterisk actions.",150)
                await message.reply(strip_narration(m)); return
            if any(re.search(k, cl) for k in FOOD_KW) and random.random()<.35:
                await message.channel.send(await _pick_fresh_pool_line(UNSOLICITED_FOOD, channel_id=message.channel.id, user_id=message.author.id)); return
            if any(re.search(k, cl) for k in SLEEP_KW) and random.random()<.35:
                await message.channel.send(await _pick_fresh_pool_line(UNSOLICITED_SLEEP, channel_id=message.channel.id, user_id=message.author.id)); return
            if any(k in cl for k in PLAN_KW) and random.random()<.25:
                await message.channel.send(await _pick_fresh_pool_line(UNSOLICITED_PLANS, channel_id=message.channel.id, user_id=message.author.id)); return
            if romance and any(k in cl for k in OTHER_BOT_KW):
                m = await qai(f"{message.author.display_name} mentioned preferring something else. Jealousy masked as contempt. 1-2 sentences.",120)
                await message.reply(m); await mem.update_mood(message.author.id,-1); return
        except Exception as e: log_error("on_message/triggers", e)

        mentioned = bot.user in message.mentions
        is_reply  = (message.reference and message.reference.resolved and
                     not isinstance(message.reference.resolved,discord.DeletedReferencedMessage) and
                     message.reference.resolved.author==bot.user)

        # ── Tedtalk follow-up detection ───────────────────────────────────
        # Only trigger if: replying to his message AND has cached material
        # AND the question seems to be about the material (not just chatting)
        if is_reply and message.author.id in _tedtalk_cache:
            cache = _tedtalk_cache[message.author.id]
            # Expire cache after 2 hours
            if time.time() - cache.get("ts", 0) > 7200:
                del _tedtalk_cache[message.author.id]
            elif cache.get("channel_id") == message.channel.id or is_dm:
                # Only use cache if message looks like a question about material
                cl = content.lower()
                is_material_question = (
                    content.endswith("?") or
                    any(k in cl for k in [
                        "what is","what are","what does","what do","explain",
                        "confused","don't understand","don't get","clarify",
                        "how does","how do","why does","why do","can you",
                        "what about","tell me more","elaborate","example",
                        "mean","define","difference between","what was"
                    ])
                )
                if is_material_question:
                    try:
                        async with message.channel.typing():
                            def _answer_followup():
                                r = ai.call_with_retry(
                                    model=GROQ_MODEL, max_completion_tokens=600,
                                    messages=[{"role":"system","content":_BASE},
                                              {"role":"user","content":(
                                        f"You gave a lecture on this material:\n{cache['material']}\n\n"
                                        f"{message.author.display_name} has a follow-up question: '{content}'\n\n"
                                        f"Answer using the material. Be accurate and thorough but stay in character. "
                                        f"Contemptuous that they need clarification, but actually helpful."
                                    )}]
                                )
                                return strip_narration(r.choices[0].message.content.strip() if r.choices else "")
                            answer = await asyncio.get_event_loop().run_in_executor(None, _answer_followup)
                        if answer:
                            await message.reply(answer)
                            await mem.add_message(message.author.id, dm_channel_id, "user", content)
                            await mem.add_message(message.author.id, dm_channel_id, "assistant", answer)
                            return
                    except Exception as e:
                        log_error("tedtalk_followup", e)

        rp = resp_prob(content, mentioned, is_reply, romance, is_dm=not bool(message.guild))
        logger.debug("response eligibility evaluated", extra={
            "response_probability": round(rp, 2), "mentioned": mentioned, "is_reply": is_reply,
        })
        if random.random()>rp:
            await maybe_react(message,romance); return

        # Build extra context
        parts = []
        try:
            if random.random()<.12:
                old = await mem.get_random_old_message(message.author.id)
                if old: parts.append(f'RECALL:"{old[:120]}"')
            if random.random()<.15:
                joke = await mem.get_random_inside_joke(message.author.id)
                if joke: parts.append(f'JOKE:"{joke[:80]}"')
            if user and user.get("rival_id") and message.guild:
                rival = message.guild.get_member(user["rival_id"])
                if rival: parts.append(f"RIVAL:{rival.display_name}")
            last_stmt = user.get("last_statement") if user else None
            if last_stmt and len(content)>20 and random.random()<.08:
                parts.append(f'CONTRADICTION:"{last_stmt[:100]}"')
            if user and user.get("trust",0)>30 and random.random()<.06:
                nice_msgs=[m for m in (await mem.get_recent_messages(message.author.id,10)) if any(k in m.lower() for k in NICE_KW)]
                if nice_msgs: parts.append(f'SELECTIVE:"{nice_msgs[0][:80]}"')
            if user and user.get("trust",0)>=70 and random.random()<.08:
                parts.append("TRUST_OPEN"); await mem.update_trust(message.author.id,-3)
        except Exception as e: log_error("on_message/context", e)

        extra = "|".join(parts)

        # Main response
        try:
            if is_dm:
                logger.debug("generating direct-message response", extra={"user_id": message.author.id})
            async with message.channel.typing():
                await typing_delay(content)
                reply = await get_response(
                    message.author.id, dm_channel_id, content,
                    user, message.author.display_name, message.author.mention,
                    extra_context=extra, is_owner=is_owner,
                    channel_obj=message.channel, is_dm=is_dm,
                    prior_last_active=previous_last_active,
                )
        except Exception as e:
            log_error("on_message/get_response", e)
            reply = random.choice(["Hmph.","...","Tch."])

        # Post-response effects
        try:
            if user and user.get("affection",0)>=50 and not user.get("affection_nick") and random.random()<.05:
                nick = await qai(f"You've started calling {message.author.display_name} by a nickname. Not nice but specific — reveals you've been paying attention. 1-4 words. Just the nickname.",20)
                if nick and len(nick)<30: await mem.set_affection_nick(message.author.id,nick.strip('"\''))
            if user and user.get("mood",0)<=-8 and not user.get("grudge_nick"):
                nick = await qai(f"You have a grudge against {message.author.display_name}. ONE degrading nickname. 1-3 words.",20)
                if nick and len(nick)<30: await mem.set_grudge_nick(message.author.id,nick.strip('"\''))
            if "TRUST_OPEN" in extra and random.random()<.5:
                await asyncio.sleep(1.5)
                await message.channel.send(await _pick_fresh_pool_line(TRUST_REVEALS, channel_id=message.channel.id, user_id=message.author.id))
            if len(content)>20 and random.random()<.04:
                check = await qai(f"Is this quotable as a running inside joke? '{content[:100]}' YES or NO only.",10)
                if "YES" in check.upper():
                    await mem.add_inside_joke(message.author.id,content[:100])
                    await mem.add_shared_inside_joke(message.author.id, content[:100], BOT_NAME)
                    debug_event("memory", f"{BOT_NAME} shared_joke user={message.author.id}")
            if user and user.get("conflict_open") and user.get("conflict_summary") and random.random() < .1:
                await mem.set_callback_memory(message.author.id, f"Unresolved tension still matters: {user['conflict_summary'][:180]}")
                debug_event("memory", f"{BOT_NAME} conflict_followup user={message.author.id}")
        except Exception as e: log_error("on_message/post_effects", e)

        # Send response
        try:
            mood_val = user.get("mood",0) if user else 0

            # Check if replying to his own voice message
            is_reply_to_self_audio = False
            if message.reference:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                    if ref.author == bot.user and any(
                        a.filename.endswith(".mp3") for a in ref.attachments
                    ):
                        is_reply_to_self_audio = True
                except Exception:
                    pass

            # Voice reply probability:
            # Explicit request: 100% (user asked for voice)
            # Replies to his own voice: 35% chance (feels like a voice conversation)
            # Normal messages: 12% chance
            VOICE_REQUEST_KW = ["voice message", "send me a voice", "voice msg", "tell me in voice",
                                "say it out loud", "speak to me", "wanna hear your voice", "want to hear your voice",
                                "use your voice", "talk to me", "send audio", "voice note", "send a voice",
                                "as a voice", "in voice", "say it in voice", "bedtime story"]
            asked_for_voice = any(k in content.lower() for k in VOICE_REQUEST_KW)
            logger.debug("voice response evaluated", extra={
                "explicit_voice_request": asked_for_voice,
                "voice_provider_configured": bool(FISH_AUDIO_API_KEY),
                "reply_chars": len(reply.strip()) if reply else 0,
            })
            if reply and len(reply.strip()) > 2:
                voice_prob = 0.0
                if FISH_AUDIO_API_KEY:
                    voice_prob = 1.0 if asked_for_voice else (0.35 if is_reply_to_self_audio else 0.12)
                elif asked_for_voice:
                    logger.info("voice requested but provider is not configured")

                if voice_prob > 0 and random.random() < voice_prob:
                    logger.debug("attempting voice send", extra={"voice_probability": voice_prob})
                    sent = await send_voice(message.channel, reply, ref=message, mood=mood_val, guild=message.guild, user=user)
                    logger.debug("voice send completed", extra={"voice_sent": sent})
                    if sent:
                        await mem.add_message(message.author.id, dm_channel_id, "assistant", f"[voice message] {reply}")
                        await maybe_react(message, romance); return
                    elif asked_for_voice:
                        # Voice was requested but failed — send as text with the reply
                        logger.info("voice send failed; using text fallback")

            if user and user.get("affection",0)>=85 and random.random()<.04 and FISH_AUDIO_API_KEY:
                await send_voice(message.channel, random.choice(["...","Tch.","Hmph."]), mood=mood_val, guild=message.guild, user=user)
            await message.reply(strip_narration(resolve_mentions(reply, message.guild if message.guild else None)))
            await mem.add_message(message.author.id, dm_channel_id, "assistant", reply)
            await maybe_react(message, romance)
        except Exception as e: log_error("on_message/send", e)

    except Exception as e:
        log_error("on_message/TOP", e)


async def _duo_autoplay_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(20)
    while not bot.is_closed():
        try:
            for session in await mem.get_due_duo_sessions(BOT_NAME):
                try:
                    channel = bot.get_channel(session["channel_id"])
                    if not channel:
                        continue
                    target_message = None
                    async for candidate in channel.history(limit=8):
                        if not candidate.author.bot:
                            target_message = candidate
                            break
                    if not target_message:
                        continue
                    await mem.upsert_user(target_message.author.id, target_message.author.name, target_message.author.display_name)
                    user = await mem.get_user(target_message.author.id)
                    reply = await get_response(
                        target_message.author.id,
                        channel.id,
                        _duo_autoplay_prompt(session),
                        user,
                        target_message.author.display_name,
                        target_message.author.mention,
                        extra_context="DUO_AUTOPLAY: the other bot already spoke. Follow up naturally, keep it brief, and do not re-explain their point.",
                        channel_obj=channel,
                        is_dm=not bool(getattr(channel, "guild", None)),
                    )
                    await channel.send(reply)
                    await mem.add_message(target_message.author.id, channel.id, "assistant", reply)
                    if session.get("awaiting_bot") == BOT_NAME and session.get("autoplay_remaining", 0) <= 1 and session.get("mode") in {"trial", "mission", "interrogate", "truthdare", "compare"}:
                        await mem.resolve_duo_story(channel.id, session.get("mode", ""), reply[:180])
                    await mem.bump_duo_session(channel.id, BOT_NAME, partner_bot=PARTNER_NAME)
                except Exception as e:
                    log_error("duo_autoplay_session", e)
        except Exception as e:
            log_error("duo_autoplay_loop", e)
        await asyncio.sleep(8)


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS — all wrapped in try/except
# ══════════════════════════════════════════════════════════════════════════════

async def safe_reply(ctx, text):
    try: await ctx.reply(text)
    except Exception as e: log_error("safe_reply", e)

async def safe_send(ctx, text):
    try: await ctx.send(text)
    except Exception as e: log_error("safe_send", e)


async def _reply_and_store(ctx, text: str):
    await safe_reply(ctx, text)
    try:
        duo = await mem.get_duo_session(ctx.channel.id)
        await mem.add_message(ctx.author.id, ctx.channel.id, "assistant", text)
        if duo and duo.get("awaiting_bot") == BOT_NAME and duo.get("autoplay_remaining", 0) <= 1 and duo.get("mode") in {"trial", "mission", "interrogate", "truthdare", "compare"}:
            await mem.resolve_duo_story(ctx.channel.id, duo.get("mode", ""), text[:180])
        await mem.bump_duo_session(ctx.channel.id, BOT_NAME, partner_bot=PARTNER_NAME)
    except Exception as e:
        log_error("reply_and_store", e)


def _format_memory_snapshot(user: dict | None, topics: list[dict], memories: list[dict], scene: dict | None) -> str:
    lines = []
    callback = (user or {}).get("callback_memory")
    if callback:
        lines.append(f"Callback: {callback[:140]}")
    if topics:
        topic_bits = ", ".join(f"{item['topic']} ({item['count']})" for item in topics[:4])
        lines.append(f"Topics: {topic_bits}")
    if memories:
        memory_bits = " | ".join(f"{item['kind']}: {item['memory'][:70]}" for item in memories[:4])
        lines.append(f"Memory bank: {memory_bits}")
    scene_desc = describe_scene_state(scene)
    if scene_desc:
        lines.append(f"Scene: {scene_desc}")
    return "\n".join(lines) if lines else "Nothing worth preserving yet. Try harder."

@bot.command(name="voice",aliases=["speak","say"])
async def voice_cmd(ctx,*,msg:str=None):
    try:
        normalized = (msg or "").strip().lower()
        user=await _setup(ctx); mood_val=user.get("mood",0) if user else 0
        if normalized in {"on", "off", "status"}:
            if normalized == "status":
                await safe_reply(ctx, f"Voice notes are `{_pref_label(user.get('voice_enabled', True) if user else True)}`.")
            else:
                enabled = normalized == "on"
                await mem.set_user_preference(ctx.author.id, "voice_enabled", int(enabled))
                await safe_reply(ctx, f"Fine. Voice notes are `{_pref_label(enabled)}` now.")
            return
        if not msg: msg="You summoned me without saying a word. How impressively useless."
        async with ctx.typing():
            text_reply=await get_response(ctx.author.id,ctx.channel.id,msg,user,ctx.author.display_name,ctx.author.mention)
            sent=await send_voice(ctx.channel,text_reply,mood=mood_val,guild=ctx.guild,user=user)
        if sent:
            await mem.add_message(ctx.author.id, ctx.channel.id, "assistant", f"[voice message] {text_reply}")
        else:
            await safe_reply(ctx,text_reply)
            await mem.add_message(ctx.author.id, ctx.channel.id, "assistant", text_reply)
    except Exception as e: log_error("voice_cmd",e); await safe_reply(ctx,"Hmph.")


@bot.command(name="tedtalk", aliases=["teach","lecture","explain"])
async def tedtalk_cmd(ctx, *, topic: str = None):
    try:
        # Lock on message ID — prevents duplicate fires from Discord edit events
        msg_id = ctx.message.id
        if msg_id in _tedtalk_active:
            return  # Silent — same message, just ignore
        _tedtalk_active.add(msg_id)

        await _setup(ctx)

        attachment = ctx.message.attachments[0] if ctx.message.attachments else None

        if not attachment and not topic:
            _tedtalk_active.discard(msg_id)
            await safe_reply(ctx, "Attach a file or give me a topic. I can't teach you nothing, as satisfying as that would be.")
            return

        file_size = attachment.size if attachment else 0
        if file_size > 500_000 or (attachment and attachment.filename.lower().endswith(".pptx")):
            time_hint = "This will take roughly 2-3 minutes."
        elif file_size > 100_000:
            time_hint = "Give me about a minute."
        else:
            time_hint = "This will take about 30-60 seconds."

        ack_lines = [
            f"Fine. Sit down, pay attention, and try not to embarrass yourself. {time_hint}",
            f"You want me to teach you something. How refreshingly self-aware of you to admit you need help. {time_hint}",
            f"Hmph. I'll condescend to explain this. Try to keep up. {time_hint}",
            f"...You actually want to learn. I find that mildly less irritating than most things. Fine. {time_hint}",
        ]
        await ctx.reply(random.choice(ack_lines))
        _spawn_transient(
            _do_tedtalk(ctx, attachment, topic, msg_id),
            name=f"tedtalk:{msg_id}",
        )

    except Exception as e:
        _tedtalk_active.discard(ctx.message.id)
        log_error("tedtalk_cmd", e)
        await safe_reply(ctx, "...Something went wrong. Annoying.")


async def _do_tedtalk(ctx, attachment, topic, msg_id=None):
    """Background task for !tedtalk — does all the heavy lifting."""
    try:
        material_content = ""

        # Immediately confirm he's working so user knows it started
        await ctx.send(random.choice([
            "...I'm reading it now. Don't interrupt me.",
            "Hmph. Give me a moment. I'm going through your material.",
            "I'm working on it. Try not to send me more messages while I'm busy.",
            "...Processing. I'll tell you when I'm done.",
        ]))

        # ── Extract content from attachment ──────────────────────────────
        if attachment:
            ct = (attachment.content_type or "").lower()
            import base64

            try:
                max_study_file_bytes = 20 * 1024 * 1024
                if attachment.size and attachment.size > max_study_file_bytes:
                    await ctx.send("That file is too large. Keep study files under 20 MB."); return
                file_bytes = await asyncio.wait_for(attachment.read(use_cached=True), timeout=30)
                if len(file_bytes) > max_study_file_bytes:
                    await ctx.send("That file is too large. Keep study files under 20 MB."); return
            except Exception as e:
                log_error("tedtalk_download", e)
                await ctx.send("I couldn't retrieve that file. Upload it again."); return

            if "pdf" in ct or attachment.filename.lower().endswith(".pdf"):
                try:
                    try:
                        import pdfplumber

                        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                            material_content = "\n".join(page.extract_text() or "" for page in pdf.pages)[:8000]
                    except Exception:
                        try:
                            import PyPDF2

                            reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                            material_content = "\n".join(page.extract_text() or "" for page in reader.pages)[:8000]
                        except Exception:
                            material_content = ""
                    if not material_content.strip():
                        await ctx.send("Couldn't read the PDF text. Try an image or PPTX instead."); return
                except Exception as e:
                    log_error("tedtalk_pdf", e)
                    await ctx.send("I couldn't read that PDF. Try a text-based PDF or an image."); return

            elif "image" in ct or attachment.filename.lower().endswith((".png",".jpg",".jpeg",".webp",".gif")):
                try:
                    img_b64 = base64.b64encode(file_bytes).decode()
                    media_type = ct if ct else "image/jpeg"
                    def _extract_img():
                        r = ai.call_with_retry(
                            model=GROQ_VISION_MODEL,
                            max_completion_tokens=2000,
                            messages=[{"role":"user","content":[
                                {"type":"image_url","image_url":{"url":f"data:{media_type};base64,{img_b64}"}},
                                {"type":"text","text":"Extract all educational content visible in this image. Include every concept, formula, definition, and key point."}
                            ]}]
                        )
                        return r.choices[0].message.content.strip() if r.choices else ""
                    extract_resp_text = await asyncio.get_event_loop().run_in_executor(None, _extract_img)
                    material_content = extract_resp_text
                except Exception as e:
                    log_error("tedtalk_image", e)
                    await ctx.send("I couldn't read that image. Try a clearer PNG or JPG."); return

            elif "text" in ct or attachment.filename.lower().endswith((".txt",".md",".csv")):
                try:
                    material_content = file_bytes.decode("utf-8", errors="ignore")[:4000]
                except Exception as e:
                    log_error("tedtalk_text", e)
                    await ctx.send("I couldn't read that text file. Use UTF-8 text and try again."); return

            elif attachment.filename.lower().endswith((".pptx",".ppt")):
                try:
                    from pptx import Presentation as _Prs
                    prs = _Prs(io.BytesIO(file_bytes))
                    parts = []
                    for i, slide in enumerate(prs.slides):
                        slide_texts = []
                        for shape in slide.shapes:
                            if hasattr(shape, "text") and shape.text.strip():
                                slide_texts.append(shape.text.strip())
                        if slide_texts:
                            parts.append(f"[Slide {i+1}]\n" + "\n".join(slide_texts))
                    material_content = "\n\n".join(parts)[:4000]
                except Exception as e:
                    log_error("tedtalk_powerpoint", e)
                    await ctx.send("I couldn't read that PowerPoint file. Try exporting it again."); return

            elif attachment.filename.lower().endswith((".docx",".doc")):
                try:
                    import docx as _docx
                    doc = _docx.Document(io.BytesIO(file_bytes))
                    parts = []
                    # Paragraphs
                    for p in doc.paragraphs:
                        if p.text.strip():
                            parts.append(p.text.strip())
                    # Tables (cheat sheets often use tables)
                    for table in doc.tables:
                        for row in table.rows:
                            row_text = " | ".join(
                                cell.text.strip() for cell in row.cells if cell.text.strip()
                            )
                            if row_text:
                                parts.append(row_text)
                    material_content = "\n".join(parts)[:4000]
                    if not material_content:
                        await ctx.send("The Word document appears to be empty or uses unsupported formatting."); return
                except Exception as e:
                    log_error("tedtalk_word", e)
                    await ctx.send("I couldn't read that Word document. Try exporting it again."); return
            else:
                await ctx.send("I can read PDFs, images, PowerPoint files, Word documents, and text files. Whatever that is, I can't work with it."); return

        if topic:
            material_content = f"Topic: {topic}\n\n{material_content}".strip()

        if not material_content:
            await ctx.send("There was nothing readable in that file. How typical."); return

        # ── Generate script ───────────────────────────────────────────────
        await ctx.send(random.choice([
            "...Fine. I'm reading it. Don't rush me.",
            "Hmph. Give me a moment. I'm processing your inadequate study material.",
            "I'm going through this. Try not to fidget.",
            "...Reviewing the material. It's about what I expected.",
        ]))
        try:
            script_prompt = (
                f"You are Scaramouche — the Sixth Fatui Harbinger, the Balladeer.\n"
                f"Teach the following material to {ctx.author.display_name}.\n\n"
                f"MATERIAL:\n{material_content[:3000]}\n\n"
                f"Write a complete spoken teaching monologue. "
                f"Teach ALL key concepts correctly and thoroughly. "
                f"Stay in character — contemptuous but accurate. "
                f"Decide length based on complexity. "
                f"Structure: introduce → explain each concept → examples → summary. "
                f"NO asterisk actions. Spoken words only."
            )
            def _gen_script():
                r = ai.call_with_retry(
                    model=GROQ_MODEL,
                    max_completion_tokens=2500,
                    messages=[{"role":"system","content":_BASE},
                              {"role":"user","content":script_prompt}]
                )
                return strip_narration(r.choices[0].message.content.strip() if r.choices else "")
            script = await asyncio.get_event_loop().run_in_executor(None, _gen_script)
        except Exception as e:
            log_error("tedtalk_script", e)
            await ctx.send("The lecture failed to generate. Try again in a moment."); return

        if not script:
            await ctx.send("...I had nothing to say. Unlikely, but here we are."); return

        # ── Generate audio in chunks ──────────────────────────────────────
        await ctx.send(random.choice([
            "Script complete. Now rendering my voice. This is beneath me but here we are.",
            "...I've written the lecture. Generating audio. Wait.",
            "Hmph. The content is ready. Give me a moment to make it sound appropriately contemptuous.",
            "Preparing to speak. Try to actually listen this time.",
        ]))

        sentences  = re.split(r'(?<=[.!?])\s+', script)
        chunks, current = [], ""
        for s in sentences:
            if len(current) + len(s) + 1 <= 900:
                current = (current + " " + s).strip()
            else:
                if current: chunks.append(current)
                current = s
        if current: chunks.append(current)

        audio_parts = []
        total_chunks = len([c for c in chunks if c.strip()])
        await ctx.send(random.choice([
            f"*Recording. {total_chunks} segments. Don't touch anything.*",
            f"*Committing this to voice. {total_chunks} parts. Try not to interrupt.*",
            f"*{total_chunks} segments to render. I'm working. Be quiet.*",
            f"*Converting my lecture to audio. {total_chunks} parts. This takes time.*",
        ]))

        for i, chunk in enumerate(chunks):
            if not chunk.strip(): continue
            try:
                audio = await get_audio_with_mood(tts_safe(chunk, ctx.guild), 0)
                if audio:
                    audio_parts.append(audio)
                else:
                    logger.warning("lecture chunk returned no content", extra={"chunk_index": i})
            except Exception as e:
                logger.warning("lecture chunk failed", extra={
                    "chunk_index": i, "error_category": type(e).__name__,
                })

            if (i+1) % 5 == 0:
                try:
                    remaining = total_chunks - (i+1)
                    await ctx.send(random.choice([
                        f"*{i+1}/{total_chunks} done. {remaining} remaining. Still working.*",
                        f"*Progress: {i+1} of {total_chunks}. Don't ask how much longer.*",
                        f"*{remaining} segments left. I said be quiet.*",
                    ]))
                except Exception: pass

        # ── Send audio ────────────────────────────────────────────────────
        await ctx.send(random.choice([
            f"*Done. {len(audio_parts)} of {total_chunks} segments rendered. Sending now.*",
            f"*{len(audio_parts)}/{total_chunks} segments complete. Here.*",
            f"*Finished. {len(audio_parts)} parts. Pay attention this time.*",
        ]))

        if not audio_parts:
            await ctx.send("Voice synthesis failed for all segments. Sending written version instead.")
            for i in range(0, len(script), 1900):
                try: await ctx.send(script[i:i+1900])
                except Exception as e:
                    log_error("tedtalk_text_fallback", e)
                    await ctx.send("The text fallback failed. Try the command again.")
            return

        MAX_BYTES = 7 * 1024 * 1024
        current_batch, part_num = b"", 1

        for audio_chunk in audio_parts:
            if len(current_batch) + len(audio_chunk) > MAX_BYTES:
                try:
                    await ctx.send(
                        f"🎙️ *Part {part_num}:*",
                        file=discord.File(io.BytesIO(current_batch), filename=f"lecture_p{part_num}.mp3")
                    )
                except Exception as e:
                    log_error("tedtalk_audio_part", e)
                    await ctx.send(f"Audio part {part_num} failed. I continued with the rest.")
                part_num += 1
                current_batch = audio_chunk
                await asyncio.sleep(1)
            else:
                current_batch += audio_chunk

        if current_batch:
            label = f"Part {part_num}" if part_num > 1 else "Lecture"
            try:
                await ctx.send(
                    f"🎙️ *{label}:*",
                    file=discord.File(io.BytesIO(current_batch), filename=f"lecture_p{part_num}.mp3")
                )
            except Exception as e:
                log_error("tedtalk_audio_final", e)
                await ctx.send("The final audio segment failed. The earlier parts are still usable.")

        # ── Cache material for follow-up questions ────────────────────────
        _tedtalk_cache[ctx.author.id] = {
            "material":   material_content[:3000],
            "channel_id": ctx.channel.id,
            "ts":         time.time(),
        }

        # ── Send notes (not transcript) ───────────────────────────────────
        try:
            def _gen_notes():
                r = ai.call_with_retry(
                    model=GROQ_MODEL,
                    max_completion_tokens=800,
                    messages=[{"role":"system","content":_BASE},
                              {"role":"user","content":(
                        f"You just gave a lecture on this material:\n{material_content[:2000]}\n\n"
                        f"Write concise study notes for {ctx.author.display_name}. "
                        f"Key terms, important concepts, things to remember. "
                        f"Bullet points are fine. Keep it short — this is a reference, not a repeat of the lecture. "
                        f"Stay in character but be genuinely useful."
                    )}]
                )
                return r.choices[0].message.content.strip() if r.choices else ""
            notes = await asyncio.get_event_loop().run_in_executor(None, _gen_notes)
            if notes:
                await ctx.send(f"📋 *Notes:*\n{notes[:1900]}")
        except Exception as e:
            log_error("tedtalk_notes", e)

    except Exception as e:
        log_error("_do_tedtalk", e)
        try: await ctx.send("...Something went wrong mid-lecture. Try again in a moment.")
        except Exception: pass
    finally:
        if msg_id: _tedtalk_active.discard(msg_id)


@bot.command(name="dare")
async def dare_cmd(ctx):
    try:
        user=await _setup(ctx)
        reply=await qai(f"Give {ctx.author.display_name} a dare. Dark, specific, theatrical. 1-2 sentences.",200)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("dare_cmd",e)

@bot.command(name="fortune",aliases=["fortunecookie"])
async def fortune_cmd(ctx):
    try:
        reply=await qai("Fortune cookie message rewritten as a cold theatrical threat. 1 sentence.",100)
        await safe_reply(ctx,f"🥠 *{reply}*")
    except Exception as e: log_error("fortune_cmd",e)

@bot.command(name="trivia")
async def trivia_cmd(ctx):
    try:
        question=await qai(
            "One difficult Genshin lore trivia question. Include answer in brackets [ANSWER: ...]. "
            "Also include a short [SOURCE: ...] note naming the lore source. Be obscure.",
            260,
        )
        answer_match = re.search(r"\[ANSWER:\s*(.*?)\]", question, re.IGNORECASE)
        source_match = re.search(r"\[SOURCE:\s*(.*?)\]", question, re.IGNORECASE)
        clean_question = re.sub(r"\s*\[ANSWER:.*?\]\s*", "", question, flags=re.IGNORECASE).strip()
        clean_question = re.sub(r"\s*\[SOURCE:.*?\]\s*", "", clean_question, flags=re.IGNORECASE).strip()
        await mem.set_active_trivia(
            ctx.channel.id,
            ctx.author.id,
            clean_question,
            answer_match.group(1).strip() if answer_match else "",
            source_match.group(1).strip() if source_match else "",
        )
        await safe_reply(ctx,clean_question)
    except Exception as e: log_error("trivia_cmd",e)

@bot.command(name="answer")
async def answer_cmd(ctx,*,response:str=None):
    try:
        if not response: await safe_reply(ctx,"Answer *what*?"); return
        await _setup(ctx)
        trivia = await mem.get_active_trivia(ctx.channel.id)
        if trivia and trivia.get("answer"):
            result=await qai(
                f"Question: {trivia['question']}\nCanonical answer: {trivia['answer']}\n"
                f"Source note: {trivia.get('source_note','unknown')}\n"
                f"{ctx.author.display_name} answered with: '{response}'. Was it right or wrong? Check against the canonical answer. "
                "Be brutal but fair. 1-2 sentences.",
                180,
            )
            correct = bool(re.search(r"\b(correct|right)\b", result.lower())) and "wrong" not in result.lower()
            await mem.clear_active_trivia(ctx.channel.id)
        else:
            result=await qai(f"{ctx.author.display_name} answered a trivia question with: '{response}'. Was it right or wrong? Check against Genshin lore. Be brutal. 1-2 sentences.",150)
            correct="right" in result.lower() or "correct" in result.lower()
        await mem.update_trivia(ctx.author.id,correct)
        stats = await mem.get_trivia_stats(ctx.author.id)
        await safe_reply(ctx,f"{result}\nScore: {stats['correct']} right, {stats['wrong']} wrong ({stats['accuracy']}% accuracy).")
    except Exception as e: log_error("answer_cmd",e)

@bot.command(name="roast",aliases=["roastbattle"])
async def roast_cmd(ctx,member:discord.Member=None):
    try:
        if not member: await safe_reply(ctx,"Roast *who*?"); return
        battle=await mem.get_active_roast(ctx.channel.id)
        if battle:
            await mem.increment_roast_round(battle["id"])
            if battle["round"]>=5:
                await mem.end_roast_battle(battle["id"])
                prompt=f"Roast battle over after 5 rounds. Scoreboard so far: {battle.get('scores', {})}. Declare final winner between {ctx.author.display_name} and {member.display_name}. Dramatic. 2-3 sentences."
            else:
                prompt=f"Judging roast battle round {battle['round']+1}. {ctx.author.display_name} fired at {member.display_name}. Score this round theatrically. 2-3 sentences."
        else:
            await mem.start_roast_battle(ctx.channel.id,ctx.author.id,member.id)
            prompt=f"You're refereeing a roast battle between {ctx.author.display_name} and {member.display_name}. Open theatrically. 5 rounds, you judge. 2-3 sentences."
        reply=await qai(prompt,300)
        lowered = reply.lower()
        winner_id = None
        if ctx.author.display_name.lower() in lowered and member.display_name.lower() not in lowered:
            winner_id = ctx.author.id
        elif member.display_name.lower() in lowered and ctx.author.display_name.lower() not in lowered:
            winner_id = member.id
        if battle and winner_id:
            await mem.award_roast_round(battle["id"], winner_id)
        updated = await mem.get_active_roast(ctx.channel.id) if battle else None
        score_line = f"\nScoreboard: {updated['scores']}" if updated and updated.get("scores") else ""
        await ctx.send(f"{ctx.author.mention} vs {member.mention}\n{reply}{score_line}")
    except Exception as e: log_error("roast_cmd",e)

@bot.command(name="hostage")
async def hostage_cmd(ctx):
    try:
        await _setup(ctx)
        if ctx.author.id in _hostages:
            await safe_reply(ctx,f"You haven't fulfilled your end yet. — *{_hostages[ctx.author.id]}*"); return
        demand=await qai(f"You've taken {ctx.author.display_name}'s good mood hostage. State your demand theatrically. 1-2 sentences.",150)
        _hostages[ctx.author.id]=demand
        await safe_reply(ctx,f"...I've taken your good mood hostage. You'll get it back when you: {demand}")
    except Exception as e: log_error("hostage_cmd",e)

@bot.command(name="release",aliases=["freed","ransom"])
async def release_cmd(ctx,*,offering:str=None):
    try:
        if ctx.author.id not in _hostages: await safe_reply(ctx,"Nothing is being held hostage. For now."); return
        demand=_hostages[ctx.author.id]
        result=await qai(f"You held {ctx.author.display_name}'s good mood hostage with: '{demand}'. They offered: '{offering or 'nothing'}'. Accept or refuse theatrically.",150)
        if any(w in result.lower() for w in ["accept","fine","release","granted"]):
            del _hostages[ctx.author.id]
        await safe_reply(ctx,result)
    except Exception as e: log_error("release_cmd",e)

@bot.command(name="impersonate",aliases=["imitate","be"])
async def impersonate_cmd(ctx,*,character:str=None):
    try:
        if not character: await safe_reply(ctx,"Impersonate *who*?"); return
        reply=await qai(f"Briefly speak as {character} from Genshin for 2 sentences, but interrupt yourself with your own editorial commentary constantly. You find this beneath you.",250)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("impersonate_cmd",e)

@bot.command(name="opinion")
async def opinion_cmd(ctx,*,character:str=None):
    try:
        if not character: await safe_reply(ctx,"Opinion on *who*?"); return
        reply=await qai(f"Your honest unfiltered personal opinion of {character} from Genshin Impact. 2-3 sentences.",250)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("opinion_cmd",e)

@bot.command(name="poll")
async def poll_cmd(ctx,*,question:str=None):
    try:
        if not question: await safe_reply(ctx,"A poll about *what*?"); return
        framing=await qai(f"Frame this as a demand for answers: '{question}'. 1 sentence.",80)
        msg=await ctx.send(f"📊 {framing}\n\n**{question}**")
        for emoji in ["👍","👎","🤷"]:
            try: await msg.add_reaction(emoji)
            except Exception: pass
    except Exception as e: log_error("poll_cmd",e)

@bot.command(name="summarize",aliases=["recap"])
async def summarize_cmd(ctx):
    try:
        recent=await mem.get_channel_recent(ctx.channel.id,20)
        if not recent: await safe_reply(ctx,"Nothing worth summarizing. Which tracks."); return
        sample="\n".join(f"{m['name']}: {m['content']}" for m in recent[:15])[:800]
        reply=await qai(f"Summarize this conversation with contemptuous commentary:\n{sample}\nBe cutting and specific. 3-4 sentences.",300)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("summarize_cmd",e)

@bot.command(name="mute",aliases=["silence","ignore"])
async def mute_cmd(ctx,member:discord.Member=None,minutes:int=10):
    try:
        target=member or ctx.author
        if member and member.id != ctx.author.id and not ctx.author.guild_permissions.manage_messages:
            await safe_reply(ctx, "You don't have permission to silence someone else.")
            return
        minutes = max(1, min(1440, minutes))
        await mem.mute_user(target.id,minutes*60)
        reply=await qai(f"You've decided to 'mute' {target.display_name} for {minutes} minutes. Announce theatrically. 1-2 sentences.",120)
        if member: await ctx.send(f"{member.mention} {reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("mute_cmd",e)

@bot.command(name="unmute",aliases=["unsilence"])
async def unmute_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        if member and member.id != ctx.author.id and not ctx.author.guild_permissions.manage_messages:
            await safe_reply(ctx, "You don't have permission to unsilence someone else.")
            return
        await mem.unmute_user(target.id)
        await safe_reply(ctx,f"...Fine. {target.display_name} may speak again. Lucky them.")
    except Exception as e: log_error("unmute_cmd",e)

@bot.command(name="spar")
async def spar_cmd(ctx,*,opening:str=None):
    try:
        user=await _setup(ctx)
        prompt=f"{ctx.author.display_name} challenged you: '{opening or 'Come on then.'}'. Fire back. End with a challenge."
        reply=await get_response(ctx.author.id,ctx.channel.id,prompt,user,ctx.author.display_name,ctx.author.mention)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("spar_cmd",e)

@bot.command(name="duel")
async def duel_cmd(ctx,member:discord.Member=None):
    try:
        if not member or member==ctx.author: await safe_reply(ctx,"Duel *who*?"); return
        u1=" | ".join((await mem.get_recent_messages(ctx.author.id,3))[:3])[:150]
        u2=" | ".join((await mem.get_recent_messages(member.id,3))[:3])[:150]
        reply=await qai(f"Referee insult duel: {ctx.author.display_name} (says:'{u1}') vs {member.display_name} (says:'{u2}'). Analyze both, declare winner. 3-4 sentences.",300)
        await ctx.send(f"{ctx.author.mention} vs {member.mention}\n{reply}")
    except Exception as e: log_error("duel_cmd",e)

@bot.command(name="judge")
async def judge_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        sample=" | ".join(await mem.get_recent_messages(target.id,8))[:400]
        reply=await qai(f"Brutal assessment of {target.display_name}"+(f" — words:'{sample}'" if sample else "")+". 2-4 sentences.",250)
        if member: await ctx.send(f"{member.mention} {reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("judge_cmd",e)

@bot.command(name="prophecy")
async def prophecy_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        reply=await qai(f"Cryptic threatening prophecy for {target.display_name}. 2-3 sentences.",200)
        if member: await ctx.send(f"{member.mention} {reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("prophecy_cmd",e)

@bot.command(name="rate")
async def rate_cmd(ctx,*,thing:str=None):
    try:
        if not thing: await safe_reply(ctx,"Rate *what*?"); return
        reply=await qai(f"Rate '{thing}' out of 10. Score first, 1-2 sentences of contempt.",180)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("rate_cmd",e)

@bot.command(name="ship")
async def ship_cmd(ctx,m1:discord.Member=None,m2:discord.Member=None):
    try:
        if not m1: await safe_reply(ctx,"Ship *who*?"); return
        p2=m2.display_name if m2 else ctx.author.display_name
        reply=await qai(f"Reluctantly analyze compatibility of {m1.display_name} and {p2}. Rating + observation. 3-4 sentences.",250)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("ship_cmd",e)

@bot.command(name="confess")
async def confess_cmd(ctx,*,confession:str=None):
    try:
        if not confession: await safe_reply(ctx,"Confess *what*?"); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,f"I have something to confess: {confession}",user,ctx.author.display_name,ctx.author.mention)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("confess_cmd",e)

@bot.command(name="compliment")
async def compliment_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        reply=await qai(f"Be forced to genuinely compliment {target.display_name}. Make it clear this is excruciating.",180)
        if member: await ctx.send(f"{member.mention} {reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("compliment_cmd",e)

@bot.command(name="haiku")
async def haiku_cmd(ctx,*,topic:str=None):
    try:
        reply=await qai(f"Dark threatening haiku about '{topic or ctx.author.display_name}'. Strict 5-7-5. Just the haiku.",100)
        await safe_reply(ctx,f"*{reply}*")
    except Exception as e: log_error("haiku_cmd",e)

@bot.command(name="story")
async def story_cmd(ctx,*,prompt:str=None):
    try:
        if not prompt: await safe_reply(ctx,"A story about *what*?"); return
        reply=await qai(f"Short dark story (3-5 sentences) about: '{prompt}'. End ominously.",350)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("story_cmd",e)

@bot.command(name="stalk")
async def stalk_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        sample=" | ".join(await mem.get_recent_messages(target.id,10))[:500]
        reply=await qai(f"Cold observation report on {target.display_name}"+(f" — statements:'{sample}'" if sample else "")+". 3-4 sentences.",280)
        if member: await ctx.send(f"*Regarding {member.mention}...*\n{reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("stalk_cmd",e)

@bot.command(name="debate")
async def debate_cmd(ctx,*,topic:str=None):
    try:
        if not topic: await safe_reply(ctx,"Debate *what*?"); return
        reply=await qai(f"Pick a side on '{topic}' and argue with conviction. 3-4 sentences.",300)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("debate_cmd",e)

@bot.command(name="conspiracy")
async def conspiracy_cmd(ctx,*,topic:str=None):
    try:
        if not topic: await safe_reply(ctx,"A conspiracy about *what*?"); return
        reply=await qai(f"Fatui-flavored conspiracy theory about '{topic}'. Deliver as established fact. 3-4 sentences.",300)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("conspiracy_cmd",e)

@bot.command(name="therapy")
async def therapy_cmd(ctx,*,problem:str=None):
    try:
        if not problem: await safe_reply(ctx,"What's your problem."); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,f"I need advice about: {problem}",user,ctx.author.display_name,ctx.author.mention)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("therapy_cmd",e)

@bot.command(name="blackmail")
async def blackmail_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        sample=" | ".join(await mem.get_recent_messages(target.id,15))[:600]
        reply=await qai(f"Find the most 'incriminating' thing in {target.display_name}'s messages: '{sample}' and theatrically threaten to use it. 2-3 sentences.",250)
        if member: await ctx.send(f"{member.mention} {reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("blackmail_cmd",e)

@bot.command(name="riddle")
async def riddle_cmd(ctx):
    try:
        reply=await qai("One cryptic Genshin-flavored riddle. No answer. Genuinely difficult.",150)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("riddle_cmd",e)

@bot.command(name="arena")
async def arena_cmd(ctx,member:discord.Member=None):
    try:
        opponent=member.display_name if member else "a nameless fool"
        reply=await qai(f"Dramatic Genshin-style battle between you (Electro) and {opponent}. You win. 4-5 sentences.",400)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("arena_cmd",e)

@bot.command(name="possess")
async def possess_cmd(ctx,member:discord.Member=None):
    try:
        if not member: await safe_reply(ctx,"Possess *who*?"); return
        sample=" | ".join(await mem.get_recent_messages(member.id,10))[:400]
        reply=await qai(f"Speak as {member.display_name} but filtered through you. Their statements: '{sample}'. 2-3 sentences.",250)
        await ctx.send(f"*Speaking as {member.mention}...*\n{reply}")
    except Exception as e: log_error("possess_cmd",e)

@bot.command(name="verdict")
async def verdict_cmd(ctx,*,situation:str=None):
    try:
        if not situation: await safe_reply(ctx,"A verdict on *what*?"); return
        reply=await qai(f"Rule on: '{situation}' like a cold judge. Finality. 2-3 sentences.",200)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("verdict_cmd",e)

@bot.command(name="letter")
async def letter_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        reply=await qai(f"Formal letter to {target.display_name} in old Inazuman style. Contemptuous, theatrical. 3-4 sentences.",300)
        if member: await ctx.send(f"{member.mention}\n{reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("letter_cmd",e)

@bot.command(name="nightmare")
async def nightmare_cmd(ctx):
    try:
        user=await _setup(ctx)
        reply=await qai(f"Describe a nightmare you had. Somehow about {ctx.author.display_name}. Don't admit that. Unsettling. 2-3 sentences.",200)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("nightmare_cmd",e)

@bot.command(name="rank")
async def rank_cmd(ctx):
    try:
        top=await mem.get_top_users(8)
        if not top: await safe_reply(ctx,"I don't know enough of you to rank."); return
        entries="\n".join(f"{i+1}. **{u['display_name']}** — {u['message_count']} messages" for i,u in enumerate(top))
        verdict=await qai(f"Rank these by tolerability: {', '.join(u['display_name'] for u in top)}. Dismissive commentary. 2 sentences.",150)
        embed=discord.Embed(title="Tolerability Ranking",description=f"{entries}\n\n*{verdict}*",color=0x4B0082)
        await ctx.send(embed=embed)
    except Exception as e: log_error("rank_cmd",e)

@bot.command(name="stats")
async def stats_cmd(ctx):
    try:
        await _setup(ctx)
        s=await mem.get_stats(ctx.author.id)
        if not s: await safe_reply(ctx,"I don't know you well enough yet."); return
        first=datetime.fromtimestamp(s["first_seen"]).strftime("%b %d, %Y") if s["first_seen"] else "unknown"
        days=int((time.time()-s["first_seen"])/86400) if s["first_seen"] else 0
        embed=discord.Embed(title=f"File: {ctx.author.display_name}",description="*I keep records.*",color=0x4B0082)
        embed.add_field(name="First contact",value=f"{first} ({days}d ago)",inline=True)
        embed.add_field(name="Messages",value=str(s["message_count"]),inline=True)
        embed.add_field(name="Mood",value=f"{s['mood']:+d} — {mood_label(s['mood'])}",inline=True)
        embed.add_field(name="Affection",value=affection_tier(s["affection"]),inline=True)
        embed.add_field(name="Trust",value=trust_tier(s["trust"]),inline=True)
        embed.add_field(name="Drift",value=f"{s['drift_score']}/100",inline=True)
        embed.add_field(name="Slow burn",value=f"{s['slow_burn']}/7",inline=True)
        embed.add_field(name="Inside jokes",value=str(s["joke_count"]),inline=True)
        if s.get("grudge_nick"): embed.add_field(name="Grudge name",value=f'"{s["grudge_nick"]}"',inline=True)
        if s.get("affection_nick"): embed.add_field(name="His nickname for you",value=f'"{s["affection_nick"]}"',inline=True)
        embed.set_footer(text="Don't read too much into this.")
        await ctx.reply(embed=embed)
    except Exception as e: log_error("stats_cmd",e)

@bot.command(name="weather")
async def weather_cmd(ctx,*,location:str=None):
    try:
        if not location: await safe_reply(ctx,"Weather where?"); return
        user = await _setup(ctx)
        data = await _fetch_nws_weather(location)
        if not data:
            await safe_reply(ctx,"That location means nothing to me. Try `City, ST`, a ZIP code, or `lat,lon`.")
            return
        precip = data.get("precipitation")
        precip_text = f"{int(round(precip))}% precipitation chance" if isinstance(precip, (int, float)) else "precipitation unknown"
        comment=await qai(
            f"Weather in {data['place']}: {data['forecast']}. "
            f"Temperature {data['temperature']} degrees {data['temperature_unit']}. "
            f"Wind {data['wind_speed']} {data['wind_direction']}. {precip_text}. "
            f"Comment in your style. 1-2 sentences.",
            150
        )
        if user and not user.get("utility_mode", True):
            reply = f"{comment}\nSource: api.weather.gov"
        else:
            reply = _utility_reply(
                f"Weather for {data['place']}",
                [
                    f"Forecast: {data['forecast']}",
                    f"Temperature: {data['temperature']} {data['temperature_unit']}",
                    f"Wind: {data['wind_speed']} {data['wind_direction']}".strip(),
                    f"Precipitation: {precip_text}",
                ],
                comment,
                "Source: api.weather.gov",
            )
        await safe_reply(ctx,reply)
    except Exception as e: log_error("weather_cmd",e); await safe_reply(ctx,"...The information was unavailable.")

@bot.command(name="lore")
async def lore_cmd(ctx,*,topic:str=None):
    try:
        if not topic: await safe_reply(ctx,"Lore about *what*?"); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,f"Tell me about this Genshin lore from your perspective: {topic}",user,ctx.author.display_name,ctx.author.mention)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("lore_cmd",e)

@bot.command(name="search",aliases=["find","lookup"])
async def search_cmd(ctx,*,query:str=None):
    try:
        if not query: await safe_reply(ctx,"Search for *what*?"); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,f"Search the web for: {query}.",user,ctx.author.display_name,ctx.author.mention,use_search=True)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("search_cmd",e)

@bot.command(name="solve",aliases=["math","essay","write"])
async def solve_cmd(ctx,*,problem:str=None):
    try:
        if not problem: await safe_reply(ctx,"Solve *what*?"); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,f"Solve or respond to this accurately: {problem}",user,ctx.author.display_name,ctx.author.mention,use_search=True)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("solve_cmd",e)

@bot.command(name="rival",aliases=["setrival"])
async def rival_cmd(ctx,member:discord.Member=None):
    try:
        await _setup(ctx)
        if not member: await mem.set_rival(ctx.author.id,None); await safe_reply(ctx,"Rivalry dissolved."); return
        if member.id==ctx.author.id: await safe_reply(ctx,"Your rival is yourself? How appropriate."); return
        await mem.set_rival(ctx.author.id,member.id)
        await safe_reply(ctx,f"Tch. {member.display_name}. Fine. I'll be watching.")
    except Exception as e: log_error("rival_cmd",e)

@bot.command(name="remind",aliases=["remindme"])
async def remind_cmd(ctx,minutes:int=None,*,reminder:str=None):
    try:
        if not minutes or not reminder: await safe_reply(ctx,"Usage: `!remind <minutes> <reminder>`"); return
        if not 1<=minutes<=10080: await safe_reply(ctx,"Between 1 minute and 7 days."); return
        await mem.add_reminder(ctx.author.id,ctx.channel.id,reminder,time.time()+minutes*60)
        await safe_reply(ctx,f"Fine. {minutes} minute{'s' if minutes!=1 else ''}. Pathetic.")
    except Exception as e: log_error("remind_cmd",e)

@bot.command(name="translate")
async def translate_cmd(ctx,*,text:str=None):
    try:
        if not text: await safe_reply(ctx,"Translate *what*?"); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,f"Rewrite this in your voice, keeping the meaning: '{text[:500]}'",user,ctx.author.display_name,ctx.author.mention)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("translate_cmd",e)

@bot.command(name="insult",aliases=["roast_single"])
async def insult_cmd(ctx,member:discord.Member=None):
    try:
        target=member or ctx.author
        reply=await qai(f"One devastating insult to {target.display_name}. Sharp, theatrical.",150)
        if member: await ctx.send(f"{member.mention} {reply}")
        else: await safe_reply(ctx,reply)
    except Exception as e: log_error("insult_cmd",e)

@bot.command(name="dm",aliases=["private","whisper"])
async def dm_cmd(ctx,*,message:str=None):
    try:
        user=await _setup(ctx)
        reply=await get_response(
            ctx.author.id,ctx.author.id,message or "The user wants to speak privately.",
            user,ctx.author.display_name,ctx.author.mention,is_dm=True
        )
        try:
            await ctx.author.send(reply); await ctx.message.add_reaction("📨")
        except discord.Forbidden:
            await safe_reply(ctx,"Your DMs are closed. How cowardly.")
    except Exception as e: log_error("dm_cmd",e)

@bot.command(name="remember")
async def remember_cmd(ctx,*,text:str=None):
    try:
        if not text:
            await safe_reply(ctx,"Tell me what I'm meant to remember.")
            return
        await _setup(ctx)
        await mem.set_callback_memory(ctx.author.id, text[:220])
        await mem.add_memory_event(ctx.author.id, "manual", text[:220], 5)
        scene_update = infer_scene_update(text, ctx.author.display_name)
        if scene_update:
            await mem.update_scene_state(ctx.channel.id, **scene_update)
        await safe_reply(ctx,"Fine. I'll remember it.")
    except Exception as e: log_error("remember_cmd",e)

@bot.command(name="forget")
async def forget_cmd(ctx,*,topic:str=None):
    try:
        if not topic or topic.strip().lower() in {"all","everything","me"}:
            await ctx.send(random.choice(["Wipe my memory of you? Press the button.","Gone in an instant. If you're sure."]),view=ResetView(ctx.author.id))
            return
        await _setup(ctx)
        result=await mem.forget_memory_matches(ctx.author.id, topic)
        result.update(await self_store.forget_user_matches(ctx.author.id, topic))
        removed=sum(result.values())
        if removed:
            await safe_reply(ctx,f"Fine. I dropped {removed} matching record{'s' if removed!=1 else ''}.")
        else:
            await safe_reply(ctx,"Nothing obvious matched that. Be more specific.")
    except Exception as e:
        log_error("forget_cmd",e)
        await safe_reply(ctx, "The forget operation did not finish cleanly. Check the owner log before trusting it.")

@bot.command(name="memories",aliases=["memorybank"])
async def memories_cmd(ctx):
    try:
        user=await _setup(ctx)
        topics=await mem.get_top_topics(ctx.author.id,4)
        memories=await mem.get_memory_bank_entries(ctx.author.id,6)
        scene=await mem.get_scene_state(ctx.channel.id)
        await safe_reply(ctx,_format_memory_snapshot(user,topics,memories,scene))
    except Exception as e: log_error("memories_cmd",e)

@bot.command(name="pinpromise")
async def pinpromise_cmd(ctx, *, text: str = None):
    try:
        await _pin_memory(ctx, "promise", text, 7)
    except Exception as e: log_error("pinpromise_cmd", e)

@bot.command(name="pinwound")
async def pinwound_cmd(ctx, *, text: str = None):
    try:
        await _pin_memory(ctx, "wound", text, 8)
    except Exception as e: log_error("pinwound_cmd", e)

@bot.command(name="pincomfort")
async def pincomfort_cmd(ctx, *, text: str = None):
    try:
        await _pin_memory(ctx, "comfort", text, 6)
    except Exception as e: log_error("pincomfort_cmd", e)

@bot.command(name="pinjoke")
async def pinjoke_cmd(ctx, *, text: str = None):
    try:
        await _pin_memory(ctx, "joke", text, 5, shared_joke=True)
    except Exception as e: log_error("pinjoke_cmd", e)

@bot.command(name="relationship")
async def relationship_cmd(ctx):
    try:
        user = await _setup(ctx)
        scene = await mem.get_scene_state(ctx.channel.id)
        topics = await mem.get_top_topics(ctx.author.id, 3)
        stage, stage_desc = _progression_parts(user)
        arc = _current_arc(user)
        aftermath = describe_conflict_aftermath(
            user.get("conflict_summary", "") if user else "",
            user.get("last_conflict_ts", 0) if user else 0,
            user.get("repair_progress", 0) if user else 0,
        ) or "no open aftermath"
        lines = [
            f"Relationship: affection {(user or {}).get('affection', 0)}/100 | trust {(user or {}).get('trust', 0)}/100 | mood {(user or {}).get('mood', 0):+d}",
            f"Arc: {arc} | progression: {stage}",
            f"Progression detail: {stage_desc}",
            f"Conflict aftermath: {aftermath}",
            f"Preferences: voice={_pref_label((user or {}).get('voice_enabled', True))} | utility={_pref_label((user or {}).get('utility_mode', True))} | duoauto={_pref_label((user or {}).get('duo_autoplay', True))} | rpdepth={(user or {}).get('rp_depth', 'medium')}",
        ]
        if topics:
            lines.append("Top topics: " + ", ".join(item["topic"] for item in topics[:3]))
        scene_desc = describe_scene_state(scene)
        if scene_desc:
            lines.append(f"Current scene: {scene_desc}")
        await safe_reply(ctx, "\n".join(lines))
    except Exception as e: log_error("relationship_cmd", e)

@bot.command(name="arc")
async def arc_cmd(ctx):
    try:
        user = await _setup(ctx)
        arc = _current_arc(user)
        stage, stage_desc = _progression_parts(user)
        unlocks = describe_arc_unlocks(BOT_NAME, arc) or "No special unlocks yet."
        recent = await mem.get_recent_milestones(f"{BOT_NAME}:user:{ctx.author.id}", 2)
        lines = [
            f"Arc: {arc}",
            f"Progression: {stage} | {stage_desc}",
            f"Unlocks: {unlocks}",
        ]
        if recent:
            lines.append("Recent milestones: " + " | ".join(note[:120] for note in recent))
        await safe_reply(ctx, "\n".join(lines))
    except Exception as e: log_error("arc_cmd", e)

@bot.command(name="duostate")
async def duostate_cmd(ctx):
    try:
        speaker_mode = await mem.get_channel_speaker_mode(ctx.channel.id)
        duo = await mem.get_duo_session(ctx.channel.id)
        relation = await mem.get_bot_relationship(PARTNER_PAIR_KEY)
        stories = await mem.get_recent_duo_stories(ctx.channel.id, 4)
        lines = [
            f"Speaker mode: {speaker_mode}",
            f"Bot relationship: {relation.get('stage', 'enemy')} | respect {relation.get('respect', 0)}/100 | tension {relation.get('tension', 0)}/100",
        ]
        if duo:
            lines.append(
                f"Active duo: mode={duo.get('mode')} | awaiting={duo.get('awaiting_bot') or 'nobody'} | turns_left={duo.get('autoplay_remaining', 0)} | topic={duo.get('topic')}"
            )
        else:
            lines.append("Active duo: none")
        if stories:
            lines.append(
                "Recent duo stories: "
                + " || ".join(
                    f"{_duo_story_label(item.get('story_type', ''))}: {item.get('topic', '')[:60]} [{item.get('status', 'open')}]"
                    for item in stories[:3]
                )
            )
        await safe_reply(ctx, "\n".join(lines))
    except Exception as e: log_error("duostate_cmd", e)

@bot.command(name="speaker", aliases=["activespeaker"])
async def speaker_cmd(ctx, mode: str = None):
    try:
        current = await mem.get_channel_speaker_mode(ctx.channel.id)
        if not mode:
            await safe_reply(ctx, f"Channel speaker mode: `{current}`.")
            return
        normalized = mode.strip().lower()
        mapping = {
            "auto": "auto",
            "both": "both",
            "scara": "scaramouche",
            "scaramouche": "scaramouche",
            "wanderer": "wanderer",
        }
        if normalized not in mapping:
            await safe_reply(ctx, "Use `!speaker auto`, `!speaker scaramouche`, `!speaker wanderer`, or `!speaker both`.")
            return
        await mem.set_channel_speaker_mode(ctx.channel.id, mapping[normalized])
        await safe_reply(ctx, f"Fine. This channel is now set to `{mapping[normalized]}` mode.")
    except Exception as e: log_error("speaker_cmd", e)

@bot.command(name="both")
async def both_cmd(ctx,*,prompt:str=None):
    try:
        if not prompt:
            await safe_reply(ctx,"Ask something worth answering.")
            return
        user=await _setup(ctx)
        await _start_duo_mode(ctx, user, "both", prompt)
        reply=await get_response(
            ctx.author.id,ctx.channel.id,prompt,user,ctx.author.display_name,ctx.author.mention,
            extra_context="TWO_BOT_MODE: The user explicitly wants both bots to answer. Keep it to one or two sentences and never speak for the other bot.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx,reply)
    except Exception as e: log_error("both_cmd",e)

@bot.command(name="duet")
async def duet_cmd(ctx,*,prompt:str=None):
    try:
        if not prompt:
            await safe_reply(ctx,"Set the scene first.")
            return
        user=await _setup(ctx)
        await _start_duo_mode(ctx, user, "duet", prompt)
        reply=await get_response(
            ctx.author.id,ctx.channel.id,f"Contribute one turn to this shared two-bot scene: {prompt}",
            user,ctx.author.display_name,ctx.author.mention,
            extra_context="DUET_MODE: contribute one short in-character turn, leave space for the other bot, and avoid narration tags.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx,reply)
    except Exception as e: log_error("duet_cmd",e)

@bot.command(name="argue")
async def argue_cmd(ctx,*,topic:str=None):
    try:
        if not topic:
            await safe_reply(ctx,"Argue about what.")
            return
        user=await _setup(ctx)
        await _start_duo_mode(ctx, user, "argue", topic)
        reply=await get_response(
            ctx.author.id,ctx.channel.id,f"The user started a deliberate two-bot argument about: {topic}",
            user,ctx.author.display_name,ctx.author.mention,
            extra_context="ARGUE_MODE: take a sharp stance, challenge the other bot directly, and keep it to one or two sentences.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx,reply)
    except Exception as e: log_error("argue_cmd",e)

@bot.command(name="compare")
async def compare_cmd(ctx,*,topic:str=None):
    try:
        if not topic:
            await safe_reply(ctx,"Compare what.")
            return
        user=await _setup(ctx)
        await _start_duo_mode(ctx, user, "compare", topic, story=True)
        reply=await get_response(
            ctx.author.id,ctx.channel.id,f"Give your verdict on this and make your difference from Wanderer clear: {topic}",
            user,ctx.author.display_name,ctx.author.mention,
            extra_context="COMPARE_MODE: answer the prompt, then draw a quick contrast between your view and the other bot's likely view.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx,reply)
    except Exception as e: log_error("compare_cmd",e)

@bot.command(name="interrogate")
async def interrogate_cmd(ctx,*,topic:str=None):
    try:
        if not topic:
            await safe_reply(ctx,"Interrogate what.")
            return
        user = await _setup(ctx)
        await _start_duo_mode(ctx, user, "interrogate", topic, story=True, enemy=topic)
        reply = await get_response(
            ctx.author.id, ctx.channel.id, f"Start a two-bot interrogation about: {topic}",
            user, ctx.author.display_name, ctx.author.mention,
            extra_context="INTERROGATE_MODE: ask one pointed question or accusation, as if cornering the target.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx, reply)
    except Exception as e: log_error("interrogate_cmd", e)

@bot.command(name="choose")
async def choose_cmd(ctx,*,options:str=None):
    try:
        if not options or "|" not in options:
            await safe_reply(ctx,"Use `!choose option A | option B`.")
            return
        user = await _setup(ctx)
        await _start_duo_mode(ctx, user, "compare", options, story=True)
        reply = await get_response(
            ctx.author.id, ctx.channel.id, f"Choose between these options and justify it: {options}",
            user, ctx.author.display_name, ctx.author.mention,
            extra_context="CHOOSE_MODE: make a clear choice fast, then justify it sharply.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx, reply)
    except Exception as e: log_error("choose_cmd", e)

@bot.command(name="trial")
async def trial_cmd(ctx,*,charge:str=None):
    try:
        if not charge:
            await safe_reply(ctx,"Put someone on trial for something.")
            return
        user = await _setup(ctx)
        await _start_duo_mode(ctx, user, "trial", charge, story=True, enemy=charge)
        reply = await get_response(
            ctx.author.id, ctx.channel.id, f"Open a two-bot trial about this charge: {charge}",
            user, ctx.author.display_name, ctx.author.mention,
            extra_context="TRIAL_MODE: deliver a prosecution or judgment opening with theatrical confidence.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx, reply)
    except Exception as e: log_error("trial_cmd", e)

@bot.command(name="mission")
async def mission_cmd(ctx,*,objective:str=None):
    try:
        if not objective:
            await safe_reply(ctx,"Mission objective.")
            return
        user = await _setup(ctx)
        await _start_duo_mode(ctx, user, "mission", objective, story=True, enemy=objective)
        reply = await get_response(
            ctx.author.id, ctx.channel.id, f"Plan a two-bot mission around this objective: {objective}",
            user, ctx.author.display_name, ctx.author.mention,
            extra_context="MISSION_MODE: assign danger, leverage, and one clear tactical role.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx, reply)
    except Exception as e: log_error("mission_cmd", e)

@bot.command(name="truthdare", aliases=["tod"])
async def truthdare_cmd(ctx,*,prompt:str=None):
    try:
        topic = prompt or "truth or dare"
        user = await _setup(ctx)
        await _start_duo_mode(ctx, user, "truthdare", topic, story=True)
        reply = await get_response(
            ctx.author.id, ctx.channel.id, f"Start a two-bot truth-or-dare round with this setup: {topic}",
            user, ctx.author.display_name, ctx.author.mention,
            extra_context="TRUTHDARE_MODE: issue one pointed truth or dare challenge, leaving room for the other bot to escalate.",
            channel_obj=ctx.channel
        )
        await _reply_and_store(ctx, reply)
    except Exception as e: log_error("truthdare_cmd", e)

@bot.command(name="scene")
async def scene_cmd(ctx):
    try:
        scene = await mem.get_scene_state(ctx.channel.id)
        duo = await mem.get_duo_session(ctx.channel.id)
        if not scene and not duo:
            await safe_reply(ctx, "This channel has no scene worth remembering yet.")
            return
        lines = []
        if scene:
            lines.append(f"Scene: {describe_scene_state(scene)}")
        if duo:
            lines.append(f"Duo mode: {duo.get('mode')} | topic={duo.get('topic')}")
        await safe_reply(ctx, "\n".join(lines))
    except Exception as e: log_error("scene_cmd", e)

@bot.command(name="insidejokes", aliases=["jokes"])
async def insidejokes_cmd(ctx):
    try:
        jokes = await mem.list_inside_jokes(ctx.author.id, 5)
        if not jokes:
            await safe_reply(ctx, "Apparently you've said nothing memorable enough to become an inside joke.")
            return
        await safe_reply(ctx, "Inside jokes:\n" + "\n".join(f"- {j[:140]}" for j in jokes))
    except Exception as e: log_error("insidejokes_cmd", e)

@bot.command(name="reset",aliases=["wipe"])
async def reset_cmd(ctx):
    try:
        await ctx.send(random.choice(["Wipe my memory of you? Press the button.","Gone in an instant. If you're sure."]),view=ResetView(ctx.author.id))
    except Exception as e: log_error("reset_cmd",e)

@bot.command(name="nsfw")
async def nsfw_cmd(ctx,mode:str=None):
    try:
        user=await _setup(ctx); cur=user.get("nsfw_mode",False) if user else False
        new=True if mode=="on" else False if mode=="off" else not cur
        is_dm = not bool(ctx.guild)
        if new and not _channel_allows_nsfw(ctx.channel, is_dm=is_dm):
            await safe_reply(ctx, "That mode can only be enabled in an age-restricted channel or a DM.")
            return
        await mem.set_mode(ctx.author.id,"nsfw_mode",new)
        await safe_reply(ctx,"Unfiltered. Fine." if new else "Restrained again. How boring.")
    except Exception as e: log_error("nsfw_cmd",e)

@bot.command(name="proactive",aliases=["ping_me"])
async def proactive_cmd(ctx,mode:str=None):
    try:
        user=await _setup(ctx); cur=user.get("proactive",True) if user else True
        new=True if mode=="on" else False if mode=="off" else not cur
        await mem.set_mode(ctx.author.id,"proactive",new)
        await safe_reply(ctx,"I might message you. Or not." if new else "Fine. I'll pretend you don't exist.")
    except Exception as e: log_error("proactive_cmd",e)

@bot.command(name="dms",aliases=["allowdms","stopdms"])
async def dms_cmd(ctx,mode:str=None):
    try:
        user=await _setup(ctx); cur=user.get("allow_dms",True) if user else True
        new=True if mode=="on" else False if mode=="off" else not cur
        await mem.set_mode(ctx.author.id,"allow_dms",new)
        await safe_reply(ctx,"Fine. I'll message you when I feel like it." if new else "Cutting me off? Fine.")
    except Exception as e: log_error("dms_cmd",e)

@bot.command(name="utility")
async def utility_cmd(ctx, mode: str = None):
    try:
        user = await _setup(ctx)
        if not mode:
            await safe_reply(ctx, f"Utility mode is `{_pref_label(user.get('utility_mode', True) if user else True)}`.")
            return
        normalized = mode.strip().lower()
        if normalized not in {"on", "off"}:
            await safe_reply(ctx, "Use `!utility on` or `!utility off`.")
            return
        enabled = normalized == "on"
        await mem.set_user_preference(ctx.author.id, "utility_mode", int(enabled))
        await safe_reply(ctx, f"Utility mode is `{_pref_label(enabled)}` now.")
    except Exception as e: log_error("utility_cmd", e)

@bot.command(name="duoauto")
async def duoauto_cmd(ctx, mode: str = None):
    try:
        user = await _setup(ctx)
        if not mode:
            await safe_reply(ctx, f"Duo autoplay is `{_pref_label(user.get('duo_autoplay', True) if user else True)}`.")
            return
        normalized = mode.strip().lower()
        if normalized not in {"on", "off"}:
            await safe_reply(ctx, "Use `!duoauto on` or `!duoauto off`.")
            return
        enabled = normalized == "on"
        await mem.set_user_preference(ctx.author.id, "duo_autoplay", int(enabled))
        await safe_reply(ctx, f"Fine. Duo autoplay is `{_pref_label(enabled)}` now.")
    except Exception as e: log_error("duoauto_cmd", e)

@bot.command(name="rpdepth")
async def rpdepth_cmd(ctx, depth: str = None):
    try:
        user = await _setup(ctx)
        if not depth:
            await safe_reply(ctx, f"RP depth is `{(user or {}).get('rp_depth', 'medium')}`.")
            return
        normalized = depth.strip().lower()
        if normalized not in {"low", "medium", "high"}:
            await safe_reply(ctx, "Use `!rpdepth low`, `!rpdepth medium`, or `!rpdepth high`.")
            return
        await mem.set_user_preference(ctx.author.id, "rp_depth", normalized)
        await safe_reply(ctx, f"Fine. RP depth is `{normalized}` now.")
    except Exception as e: log_error("rpdepth_cmd", e)

@bot.command(name="timezone")
async def timezone_cmd(ctx, *, timezone_name: str = None):
    try:
        user = await _setup(ctx)
        if not timezone_name:
            await safe_reply(ctx, f"Timezone: {user.get('timezone_name', 'America/Los_Angeles')}")
            return
        ZoneInfo(timezone_name.strip())
        await mem.set_timezone(ctx.author.id, timezone_name.strip())
        await safe_reply(ctx, f"Timezone set to `{timezone_name.strip()}`.")
    except Exception:
        await safe_reply(ctx, "Use a valid IANA timezone like `America/Los_Angeles`.")

@bot.command(name="quiethours")
async def quiethours_cmd(ctx, start_hour: int = None, end_hour: int = None):
    try:
        user = await _setup(ctx)
        if start_hour is None or end_hour is None:
            await safe_reply(ctx, f"Quiet hours: {user.get('quiet_hours_start', 23)}:00 to {user.get('quiet_hours_end', 8)}:00")
            return
        await mem.set_quiet_hours(ctx.author.id, start_hour, end_hour)
        await safe_reply(ctx, f"Quiet hours set to {start_hour % 24}:00-{end_hour % 24}:00.")
    except Exception as e: log_error("quiethours_cmd", e)

@bot.command(name="dmfreq")
async def dmfreq_cmd(ctx, hours: int = None):
    try:
        user = await _setup(ctx)
        if hours is None:
            await safe_reply(ctx, f"DM frequency floor: every {user.get('dm_frequency_hours', 8)} hour(s).")
            return
        await mem.set_dm_frequency(ctx.author.id, hours)
        await safe_reply(ctx, f"Fine. No more than once every {max(1, min(72, hours))} hour(s).")
    except Exception as e: log_error("dmfreq_cmd", e)

@bot.command(name="dmgrace")
async def dmgrace_cmd(ctx, minutes: int = None):
    try:
        user = await _setup(ctx)
        if minutes is None:
            await safe_reply(ctx, f"Recent-activity DM grace: {user.get('recent_activity_grace_minutes', 45)} minute(s).")
            return
        await mem.set_recent_activity_grace(ctx.author.id, minutes)
        await safe_reply(ctx, f"I'll leave at least {max(5, min(720, minutes))} minute(s) after your recent activity before DMing.")
    except Exception as e: log_error("dmgrace_cmd", e)

@bot.command(name="mood")
async def mood_cmd(ctx):
    try:
        await _setup(ctx); s=await mem.get_mood(ctx.author.id)
        bar="█"*(s+10)+"░"*(20-(s+10))
        await safe_reply(ctx,f"`[{bar}]` {s:+d} — {mood_label(s)}\n*Don't read into this.*")
    except Exception as e: log_error("mood_cmd",e)

@bot.command(name="affection")
async def affection_cmd(ctx):
    try:
        await _setup(ctx); user=await mem.get_user(ctx.author.id); s=user.get("affection",0) if user else 0
        bar="█"*(s//5)+"░"*(20-s//5)
        await safe_reply(ctx,f"`[{bar}]` {s}/100 — {affection_tier(s)}\n*...I said don't look at that.*")
    except Exception as e: log_error("affection_cmd",e)

@bot.command(name="trust")
async def trust_cmd(ctx):
    try:
        await _setup(ctx); user=await mem.get_user(ctx.author.id); s=user.get("trust",0) if user else 0
        bar="█"*(s//5)+"░"*(20-s//5)
        await safe_reply(ctx,f"`[{bar}]` {s}/100 — {trust_tier(s)}\n*This means nothing.*")
    except Exception as e: log_error("trust_cmd",e)

@bot.command(name="whoami")
async def whoami_cmd(ctx):
    try:
        if not OWNER_ID or ctx.author.id!=OWNER_ID: await safe_reply(ctx,"That command isn't for you."); return
        user=await _setup(ctx)
        reply=await get_response(ctx.author.id,ctx.channel.id,"What do you actually think about the fact that I built you. Be honest.",user,ctx.author.display_name,ctx.author.mention,is_owner=True)
        await safe_reply(ctx,reply)
    except Exception as e: log_error("whoami_cmd",e)


def _owner_only(ctx) -> bool:
    return bool(OWNER_ID and ctx.author.id == OWNER_ID)


@bot.command(name="selfstate")
async def selfstate_cmd(ctx):
    if not _owner_only(ctx):
        await safe_reply(ctx, "That command isn't for you.")
        return
    try:
        summary = await self_store.diagnostic_summary()
        environment = heartbeat.last_environment
        goals = summary["active_goals"]
        dimensions = ", ".join(f"{key}={value}" for key, value in summary["dimensions"].items())
        goal_lines = "\n".join(f"• #{goal['id']} P{goal['priority']} {goal['description'][:90]}" for goal in goals[:5]) or "None"
        budget = summary["budget"]
        env_text = "not sampled yet"
        if environment:
            env_text = (
                f"provider={environment.provider_status}, database={environment.database_health}, "
                f"cpu={environment.cpu_pressure}, memory={environment.memory_pressure}, "
                f"disk={environment.disk_pressure}, discord={environment.discord_latency_ms}ms"
            )
        embed = discord.Embed(title="Scaramouche — sanitized self-state", color=0x4B0082)
        embed.add_field(name="Internal dimensions", value=dimensions[:1024], inline=False)
        embed.add_field(name="Modeled cause", value=(summary["mood_cause"] or "none")[:1024], inline=False)
        embed.add_field(name="Active goals", value=goal_lines[:1024], inline=False)
        embed.add_field(
            name="Continuity",
            value=(f"beliefs={summary['belief_count']} | open contradictions={summary['open_contradictions']} | "
                   f"last reflection={int(summary['last_reflection_ts'] or 0)} | heartbeat={int(heartbeat.last_tick or 0)}"),
            inline=False,
        )
        embed.add_field(
            name="Autonomous call budget",
            value=f"hour {budget['hour_used']}/{budget['hour_limit']} | day {budget['day_used']}/{budget['day_limit']}",
            inline=False,
        )
        embed.add_field(name="Environment", value=env_text[:1024], inline=False)
        await ctx.send(embed=embed)
    except Exception as exc:
        logger.exception("selfstate command failed", extra={"error_category": type(exc).__name__})
        await safe_reply(ctx, "The diagnostic is unavailable. Check the owner log.")


@bot.command(name="selfgoals")
async def selfgoals_cmd(ctx):
    if not _owner_only(ctx):
        await safe_reply(ctx, "That command isn't for you.")
        return
    goals = await self_store.list_goals(limit=CONFIG.max_active_goals)
    text = "\n".join(
        f"`#{goal['id']}` P{goal['priority']} {goal['description']} ({int(goal['progress'] * 100)}%)"
        for goal in goals
    ) or "No active goals. Silence can be a decision too."
    await safe_reply(ctx, text[:1900])


@bot.command(name="forceheartbeat")
async def forceheartbeat_cmd(ctx):
    if not _owner_only(ctx):
        await safe_reply(ctx, "That command isn't for you.")
        return
    result = await heartbeat.tick(
        discord_latency=bot.latency, db_probe=mem.healthcheck,
        active_conversations=len(await mem.get_active_channels()),
        reflection_generator=_reflection_generation, proactive_candidates=[],
    )
    await safe_reply(ctx, f"Heartbeat evaluated: `{result.action.action.value}`; reflection={result.reflected}.")


@bot.command(name="selfbackup")
async def selfbackup_cmd(ctx):
    if not _owner_only(ctx):
        await safe_reply(ctx, "That command isn't for you.")
        return
    try:
        await mem.backup()
        await safe_reply(ctx, "The memory database backup completed.")
    except Exception as exc:
        logger.exception("self backup failed", extra={"error_category": type(exc).__name__})
        await safe_reply(ctx, "The backup failed. Check the owner log before trusting it.")

async def help_cmd(ctx):
    try:
        c = 0x4B0082
        e1 = discord.Embed(title="Commands (1/3) — Talk & Fight",
                           description="Hmph. Only saying this once.", color=c)
        for n,v in [
            ("🔊 !voice <msg>","Voice message — !speak !say"),
            ("📨 !dm [msg]","He DMs you privately"),
            ("🤫 !confess <text>","Tell him something"),
            ("🛋️ !therapy <problem>","Terrible in-character advice"),
            ("🌐 !translate <text>","Rewritten in his voice"),
            ("⚔️ !spar [msg]","Word battle"),
            ("🥊 !duel @user","Insult battle referee"),
            ("🎤 !roast @user","Turn-based roast battle (5 rounds)"),
            ("⚡ !arena [@user]","Dramatic mock Genshin battle"),
            ("🎯 !dare","A dark theatrical dare"),
            ("🧠 !trivia","Genshin lore trivia"),
            ("✅ !answer <text>","Answer a trivia question"),
            ("🧩 !riddle","Cryptic Genshin riddle"),
            ("🔒 !hostage","Takes your good mood hostage"),
            ("🔓 !release <offering>","Try to fulfill his demand"),
            ("🥠 !fortune","Fortune cookie rewritten as a threat"),
            ("💭 !opinion <char>","His honest take on any Genshin character"),
            ("🎭 !impersonate <char>","Speaks as them, badly"),
            ("📜 !lore <topic>","Genshin lore from his perspective"),
        ]: e1.add_field(name=n,value=v,inline=False)
        for n, v in [
            ("!both <prompt>", "Both bots answer in sequence"),
            ("!duet <prompt>", "Start a shared two-bot scene"),
            ("!argue <topic>", "Let both bots clash over a topic"),
            ("!compare <topic>", "Each bot gives a contrasting verdict"),
        ]: e1.add_field(name=n, value=v, inline=False)

        e2 = discord.Embed(title="Commands (2/3) — Assess & Create", color=c)
        for n,v in [
            ("🔍 !judge [@user]","Brutal character assessment"),
            ("👁️ !stalk [@user]","Cold observation report"),
            ("🃏 !blackmail [@user]","Most incriminating messages"),
            ("🔦 !interrogate @user","Cold interrogation"),
            ("👻 !possess @user","Speaks as them, filtered through him"),
            ("📊 !rate <thing>","Rates anything out of 10"),
            ("💞 !ship @u1 [@u2]","Reluctant compatibility"),
            ("⚖️ !verdict <situation>","He rules on anything"),
            ("⚖️ !debate <topic>","He argues a side"),
            ("🕵️ !conspiracy <topic>","Fatui conspiracy theory"),
            ("🏆 !rank","Ranks everyone by tolerability"),
            ("📝 !haiku [topic]","Dark threatening haiku"),
            ("📖 !story <prompt>","Short dark story"),
            ("✉️ !letter [@user]","Formal old Inazuman letter"),
            ("🌸 !compliment [@user]","Forces him to say something nice"),
            ("⚡ !insult [@user]","Cutting insult"),
            ("🔮 !prophecy [@user]","Cryptic threatening fortune"),
            ("😰 !nightmare","A nightmare. Somehow about you."),
            ("🔍 !search <query>","Web search with commentary"),
            ("🧮 !solve <problem>","Math, essays, Q&A — !math !essay"),
        ]: e2.add_field(name=n,value=v,inline=False)

        e3 = discord.Embed(title="Commands (3/3) — Settings & Stats", color=c)
        for n,v in [
            ("📊 !stats","Your full relationship file"),
            ("🌡️ !mood","His mood toward you"),
            ("💜 !affection","His hidden affection score"),
            ("🔒 !trust","His trust level toward you"),
            ("🗡️ !rival @user","Designate a rival"),
            ("⏰ !remind <mins> <txt>","Reminder with disdain"),
            ("🌤️ !weather <city>","Weather + contemptuous commentary"),
            ("📢 !poll <question>","He demands a vote"),
            ("📋 !summarize","Recent chat summary with contempt"),
            ("🔇 !mute [@user] [min]","Ignores someone in character"),
            ("🔊 !unmute [@user]","Unmutes someone"),
            ("🔄 !reset","Wipe your memory — !forget"),
            ("🔞 !nsfw [on/off]","Toggle unfiltered mode"),
            ("📡 !proactive [on/off]","Toggle unprompted messages"),
            ("💌 !dms [on/off]","Toggle voluntary private DMs"),
        ]: e3.add_field(name=n,value=v,inline=False)
        for n, v in [
            ("!memories", "See what he is actually holding onto"),
            ("!remember <text>", "Tell him to keep something"),
            ("!forget <topic>", "Forget one topic instead of everything"),
            ("!relationship / !arc", "See arc, progression, and conflict aftermath"),
            ("!duostate / !speaker", "Inspect duo mode or set the active speaker for this channel"),
            ("!pinpromise / !pinwound / !pincomfort / !pinjoke", "Pin more precise memories"),
            ("!utility / !duoauto / !rpdepth", "Tune utility output, duo chaining, and RP depth"),
        ]: e3.add_field(name=n, value=v, inline=False)
        e3.add_field(name="Hidden Systems",
            value="Be kind 7 days in a row: something rare happens once\n"
                  "Be rude: mood drops, you get a degrading nickname\n"
                  "High affection: he starts calling you something specific\n"
                  "Build trust: he tells you things he would never normally say\n"
                  "Say you will never win: villain monologue\n"
                  "Mention his hat: disproportionate response\n"
                  "He reads the channel — knows what everyone has been saying",
            inline=False)
        e3.set_footer(text="Scaramouche — The Balladeer | !scarahelp for commands")
        await ctx.send(embed=e1)
        await ctx.send(embed=e2)
        await ctx.send(embed=e3)
    except Exception as e:
        log_error("help_cmd", e)
        try: await ctx.send("Hmph. Something went wrong.")
        except Exception: pass

@bot.command(name="scarahelp", aliases=["commands"])
async def scarahelp_cmd(ctx):
    try:
        await help_cmd(ctx)
    except Exception as e:
        log_error("scarahelp_cmd", e)
        try: await ctx.send("Hmph. Something went wrong.")
        except Exception: pass

@bot.event
async def on_command_error(ctx,error):
    try:
        if isinstance(error,commands.CommandNotFound): pass
        elif isinstance(error,commands.MemberNotFound): pass
        elif isinstance(error,commands.MissingRequiredArgument):
            await safe_reply(ctx,"You're missing something.")
        else: log_error("on_command_error",error)
    except Exception: pass

if __name__=="__main__":
    if not DISCORD_TOKEN: raise SystemExit("❌ DISCORD_TOKEN not set")
    if not _groq_keys: raise SystemExit("❌ No GROQ_API_KEY set (need at least GROQ_API_KEY)")
    bot.run(DISCORD_TOKEN)
