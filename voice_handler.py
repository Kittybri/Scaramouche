"""
voice_handler.py — Scaramouche Bot (The Balladeer)
Voice ID: fb95ab47841a4db189cb35fb619d4ea1
"""

from __future__ import annotations

import io
import asyncio
import logging
import httpx
import ormsgpack

VOICE_ID      = "fb95ab47841a4db189cb35fb619d4ea1"  # Scaramouche voice
FISH_API_URL  = "https://api.fish.audio/v1/tts"
log = logging.getLogger(__name__)


def _fish_tts_blocking(text: str, api_key: str, chunk_length: int = 220) -> bytes | None:
    try:
        payload = ormsgpack.packb({
            "text":         text[:1500],
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
            "model":         "s2-pro",
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post(FISH_API_URL, content=payload, headers=headers)
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


async def generate_tts_gtts(text: str) -> bytes | None:
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=text[:800], lang="en", slow=False).write_to_fp(buf)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        log.warning("gTTS generation failed", extra={"error_category": type(e).__name__})
        return None


async def get_audio(text: str, fish_audio_key: str) -> bytes | None:
    if fish_audio_key:
        audio = await generate_tts_fish_audio(text, fish_audio_key)
        if audio: return audio
    return await generate_tts_gtts(text)


def _style_tts_text(text: str, style: str = "guarded") -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if style == "soft":
        return cleaned.replace("...", ".  ").replace("—", ". ")
    if style == "tense":
        return cleaned.replace(",", ". ").replace(";", ". ")
    if style in {"cutting", "distant"}:
        return cleaned.replace(" and ", ". ").replace(" but ", ". ")
    if style in {"measured", "curious"}:
        return cleaned.replace("...", ", ")
    return cleaned


async def get_audio_mooded(text: str, fish_audio_key: str, mood: int = 0, style: str = "guarded") -> bytes | None:
    if not fish_audio_key:
        return await generate_tts_gtts(text)
    # Mood affects pacing
    if mood <= -6:   chunk = 140
    elif mood <= -1: chunk = 190
    elif mood <= 5:  chunk = 220
    else:            chunk = 260
    if style == "soft":
        chunk += 35
    elif style == "tense":
        chunk = max(120, chunk - 30)
    elif style in {"cutting", "distant"}:
        chunk = max(130, chunk - 20)
    elif style in {"measured", "curious"}:
        chunk += 10
    styled_text = _style_tts_text(text, style)

    def _blocking():
        return _fish_tts_blocking(styled_text, fish_audio_key, chunk)

    try:
        audio = await asyncio.get_event_loop().run_in_executor(None, _blocking)
        if audio: return audio
        return await generate_tts_gtts(text)
    except Exception as e:
        log.warning("mooded voice generation failed", extra={"error_category": type(e).__name__})
        return await generate_tts_gtts(text)
