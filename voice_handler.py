"""
voice_handler.py — Scaramouche Bot (The Balladeer)
Text-to-speech: Fish Audio first, Google TTS as a fallback.

Every network call here is blocking, so it runs in an executor and never stalls the Discord event loop.
"""

import asyncio
import io
import logging
import os
import re

import httpx
import ormsgpack

log = logging.getLogger("scaramouche.voice")

VOICE_ID = os.getenv("FISH_VOICE_ID") or "fb95ab47841a4db189cb35fb619d4ea1"  # Scaramouche voice
FISH_MODEL = os.getenv("FISH_AUDIO_MODEL") or "s2-pro"
FISH_API_URL = "https://api.fish.audio/v1/tts"
FISH_MAX_CHARS = 1500
GTTS_MAX_CHARS = 1500  # gTTS splits long text itself; this only bounds how long one request can take


def _fish_tts_blocking(text: str, api_key: str, chunk_length: int = 220) -> bytes | None:
    try:
        payload = ormsgpack.packb({
            "text":         text[:FISH_MAX_CHARS],
            "reference_id": VOICE_ID,
            "format":       "mp3",
            "mp3_bitrate":  192,
            "latency":      "balanced",
            "normalize":    True,
            "chunk_length": chunk_length,
        })
        headers = {
            "authorization": f"Bearer {api_key}",
            "content-type":  "application/msgpack",
            "model":         FISH_MODEL,
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(FISH_API_URL, content=payload, headers=headers)
        if resp.status_code == 200:
            log.info("Fish Audio: %s bytes", f"{len(resp.content):,}")
            return resp.content
        log.warning("Fish Audio: HTTP %s: %s", resp.status_code, resp.text[:200])
        return None
    except Exception as e:  # noqa: BLE001 - voice is optional, any failure falls back to gTTS
        log.warning("Fish Audio: %s: %s", type(e).__name__, e)
        return None


def _gtts_blocking(text: str) -> bytes | None:
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text[:GTTS_MAX_CHARS], lang="en", slow=False).write_to_fp(buf)
        return buf.getvalue() or None
    except Exception as e:  # noqa: BLE001
        log.warning("gTTS: %s: %s", type(e).__name__, e)
        return None


async def generate_tts_fish_audio(text: str, api_key: str, chunk_length: int = 220) -> bytes | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fish_tts_blocking, text, api_key, chunk_length)


async def generate_tts_gtts(text: str) -> bytes | None:
    if not (text or "").strip():
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _gtts_blocking, text)


async def get_audio(text: str, fish_audio_key: str) -> bytes | None:
    if not (text or "").strip():
        return None
    if fish_audio_key:
        audio = await generate_tts_fish_audio(text, fish_audio_key)
        if audio:
            return audio
    return await generate_tts_gtts(text)


def _style_tts_text(text: str, style: str = "guarded") -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if style == "soft":
        return cleaned.replace("...", ".  ").replace("—", ". ")
    if style == "tense":
        # only break at clause punctuation followed by a space, so "1,000" is still read as one number
        return re.sub(r"[,;]\s+", ". ", cleaned)
    if style in {"cutting", "distant"}:
        return cleaned.replace(" and ", ". ").replace(" but ", ". ")
    if style in {"measured", "curious"}:
        return cleaned.replace("...", ", ")
    return cleaned


def _chunk_for(mood: int, style: str) -> int:
    # Mood affects pacing
    if mood <= -6:
        chunk = 140
    elif mood <= -1:
        chunk = 190
    elif mood <= 5:
        chunk = 220
    else:
        chunk = 260
    if style == "soft":
        chunk += 35
    elif style == "tense":
        chunk = max(120, chunk - 30)
    elif style in {"cutting", "distant"}:
        chunk = max(130, chunk - 20)
    elif style in {"measured", "curious"}:
        chunk += 10
    return chunk


async def get_audio_mooded(text: str, fish_audio_key: str, mood: int = 0, style: str = "guarded") -> bytes | None:
    if not (text or "").strip():
        return None
    if not fish_audio_key:
        return await generate_tts_gtts(text)
    styled_text = _style_tts_text(text, style)
    audio = await generate_tts_fish_audio(styled_text, fish_audio_key, _chunk_for(mood, style))
    if audio:
        return audio
    return await generate_tts_gtts(text)
