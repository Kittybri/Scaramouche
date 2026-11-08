"""
voice_handler.py — Scaramouche Bot (The Balladeer)
Voice ID: fb95ab47841a4db189cb35fb619d4ea1
"""

from __future__ import annotations

import io
import os
import re
import asyncio
import logging
import threading
import httpx
import ormsgpack

VOICE_ID      = "fb95ab47841a4db189cb35fb619d4ea1"  # Scaramouche voice
FISH_API_URL  = "https://api.fish.audio/v1/tts"
log = logging.getLogger(__name__)


_local = threading.local()
# A connection the server dropped while it sat idle: the request never reached Fish, so it is safe to send again.
_RECONNECT_ERRORS = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError, httpx.WriteError)


def _fish_post(payload: bytes, headers: dict) -> httpx.Response:
    """POST to Fish over a connection kept open per worker thread.

    A new connection (and TLS handshake) for every piece of a reply added to every pause in his speech. A kept
    connection the server has since closed is retried once on a fresh one; timeouts are not retried.
    """
    for attempt in (1, 2):
        client = getattr(_local, "client", None)
        if client is None:
            client = _local.client = httpx.Client(timeout=60, limits=httpx.Limits(keepalive_expiry=30))
        try:
            return client.post(FISH_API_URL, content=payload, headers=headers)
        except _RECONNECT_ERRORS:
            _local.client = None
            client.close()
            if attempt == 2:
                raise


def _fish_tts_blocking(text: str, api_key: str, chunk_length: int = 220, *, speed: float = 1.0) -> bytes | None:
    try:
        request = {
            "text":         text[:1500],
            "reference_id": VOICE_ID,
            "format":       "mp3",
            "mp3_bitrate":  192,
            "latency":      "balanced",
            "normalize":    True,
            "chunk_length": chunk_length,
        }
        if speed != 1.0:
            request["prosody"] = {"speed": speed, "volume": 0}
        payload = ormsgpack.packb(request)
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type":  "application/msgpack",
            "model":         "s2-pro",
        }
        resp = _fish_post(payload, headers)
        if resp.status_code == 200:
            log.debug("Fish Audio response received", extra={"audio_bytes": len(resp.content)})
            return resp.content
        log.warning("Fish Audio request failed", extra={"http_status": resp.status_code})
        return None
    except Exception as e:
        log.warning("Fish Audio request failed", extra={"error_category": type(e).__name__})
        return None


async def generate_tts_fish_audio(text: str, api_key: str, chunk_length: int = 220) -> bytes | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fish_tts_blocking, text, api_key, chunk_length)


GTTS_REQUEST_TIMEOUT_S = 15


def _gtts_blocking(text: str) -> bytes:
    from gtts import gTTS
    buf = io.BytesIO()
    gTTS(text=text[:800], lang="en", slow=False, timeout=GTTS_REQUEST_TIMEOUT_S).write_to_fp(buf)
    return buf.getvalue()


async def generate_tts_gtts(text: str) -> bytes | None:
    # gTTS makes its web requests synchronously and with no timeout by default: called directly here it froze the
    # whole bot (every message and the Discord heartbeat) for as long as Google took, so it runs in a worker thread
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _gtts_blocking, text)
    except Exception as e:
        log.warning("gTTS generation failed", extra={"error_category": type(e).__name__})
        return None


async def get_audio(text: str, fish_audio_key: str) -> bytes | None:
    if fish_audio_key:
        audio = await generate_tts_fish_audio(text, fish_audio_key)
        if audio: return audio
    return await generate_tts_gtts(text)


# How he says it, per voice style: a Fish S2 cue (plain words in square brackets before a sentence, lasting to the
# end of that sentence) and a small speed change. His mood used to be shown by rewriting punctuation, which dropped
# words: in a cold mood "you and I" came out as "you. I". The words are now left alone.
_DELIVERY = {
    "heated":                   ("angry, sharp", 1.05),
    "tense":                    ("tense, clipped", 1.03),
    "cutting":                  ("cold and contemptuous", 0.96),
    "cold":                     ("cold, distant", 0.98),
    "distant":                  ("distant, flat", 0.98),
    "mocking":                  ("mocking, smug", 1.0),
    "curious":                  ("curious", 1.0),
    "measured":                 ("measured, deliberate", 0.98),
    "concerned":                ("quieter, concerned despite himself", 0.97),
    "soft":                     ("softer", 0.97),
    "reconciliation":           ("softer, reluctant", 0.97),
    "restrained_vulnerability": ("quiet, guarded, a little vulnerable", 0.96),
}


def _expressive() -> bool:
    """VOICE_EMOTION_TAGS=off turns the cues and speed changes off (plain delivery), without a code change."""
    return (os.getenv("VOICE_EMOTION_TAGS") or "on").strip().lower() not in {"off", "0", "false", "no"}


def _delivery(style: str) -> tuple:
    cue, speed = _DELIVERY.get(style, ("", 1.0))
    return (cue, speed) if _expressive() else ("", 1.0)


def _style_tts_text(text: str, style: str = "guarded") -> str:
    # Cues come only from here: square brackets in the reply itself (from the model or a user) are removed, since
    # Fish S2 would take them as instructions.
    cleaned = re.sub(r"\s{2,}", " ", re.sub(r"\[[^\[\]]{0,80}\]", " ", text or "")).strip()
    cue = _delivery(style)[0]
    if not cleaned or not cue:
        return cleaned
    styled = " ".join(f"[{cue}] {sentence}" for sentence in re.split(r"(?<=[.!?…])\s+", cleaned) if sentence)
    # Fish is sent at most 1500 characters: cues never push words out, so a very long text is voiced plainly.
    return styled if len(styled) <= 1500 else cleaned


async def get_audio_mooded(text: str, fish_audio_key: str, mood: int = 0, style: str = "guarded") -> bytes | None:
    if not fish_audio_key:
        return await generate_tts_gtts(text)
    # Fish's chunk_length is a generation/buffering control, not speech speed.
    chunk = 220
    styled_text = _style_tts_text(text, style)
    speed = _delivery(style)[1]

    def _blocking():
        return _fish_tts_blocking(styled_text, fish_audio_key, chunk, speed=speed)

    try:
        audio = await asyncio.get_event_loop().run_in_executor(None, _blocking)
        if audio: return audio
        return await generate_tts_gtts(text)
    except Exception as e:
        log.warning("mooded voice generation failed", extra={"error_category": type(e).__name__})
        return await generate_tts_gtts(text)
