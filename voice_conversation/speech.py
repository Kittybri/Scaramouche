"""Ephemeral per-speaker PCM segmentation and replaceable final STT."""

from __future__ import annotations

import asyncio
import audioop
import io
import os
import re
import time
import wave
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Limits:
    start_ms: int = 120
    end_ms: int = 900
    minimum_ms: int = 240
    barge_ms: int = 320
    max_seconds: float = 12
    participants: int = 4
    frame_queue: int = 128
    stt_queue: int = 2
    stale_seconds: float = 15
    # A sentence ends once the last end_ms are mostly not speech (below this share of speech frames), not only
    # after end_ms of unbroken silence that background noise never allows.
    endpoint_ratio: float = 0.35
    # ...and only counts as speech if some 400 ms stretch of it is at least this dense; scattered noise frames are not.
    dense_ratio: float = 0.4

    @classmethod
    def from_env(cls):
        return cls(
            start_ms=int(os.getenv("VOICE_SPEECH_START_MS", "120")),
            end_ms=int(os.getenv("VOICE_SPEECH_END_MS", "900")),
            minimum_ms=int(os.getenv("VOICE_SPEECH_MIN_MS", "240")),
            barge_ms=int(os.getenv("VOICE_BARGE_MS", "320")),
            max_seconds=float(os.getenv("VOICE_MAX_UTTERANCE_SECONDS", "12")),
            endpoint_ratio=float(os.getenv("VOICE_ENDPOINT_SPEECH_RATIO", "0.35")),
            dense_ratio=float(os.getenv("VOICE_MIN_SPEECH_DENSITY", "0.4")),
        )

    def __post_init__(self):
        if not (60 <= self.start_ms <= self.minimum_ms <= 1000):
            raise ValueError("Invalid speech thresholds")
        if not (300 <= self.end_ms <= 2000 and 200 <= self.barge_ms <= 1500):
            raise ValueError("Invalid silence/barge thresholds")
        if not (2 <= self.max_seconds <= 20 and 1 <= self.participants <= 8):
            raise ValueError("Invalid voice bounds")
        if not (
            1 <= self.stt_queue <= 4
            and 16 <= self.frame_queue <= 256
            and 2 <= self.stale_seconds <= 30
        ):
            raise ValueError("Invalid queue bounds")
        if not (0.05 <= self.endpoint_ratio <= 0.8 and 0.1 <= self.dense_ratio <= 1):
            raise ValueError("Invalid speech density bounds")


@dataclass
class Utterance:
    user_id: int
    pcm: bytes
    started: float
    ended: float
    epoch: int = 0


@dataclass
class Segment:
    pcm: bytearray = field(default_factory=bytearray)
    start: float = 0
    last: float = 0
    speech_ms: int = 0
    silence_ms: int = 0
    run_ms: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=45))  # speech flags of the last end_ms
    dense: deque = field(default_factory=lambda: deque(maxlen=20))   # the last 400 ms
    dense_best: int = 0                                              # most speech frames in any 400 ms so far


class Segmenter:
    """Accept exactly 20ms/48k/stereo PCM; retain at most one bounded segment/user."""

    def __init__(self, limits=Limits(), vad=None):
        if vad is None:
            import webrtcvad

            vad = webrtcvad.Vad(2)
        self.vad, self.limits, self.users = vad, limits, {}
        self.metrics = Counter()

    def feed(self, uid, pcm, now=None):
        now = time.monotonic() if now is None else now
        if len(pcm) != 3840:
            return None, 0
        mono = audioop.tomono(pcm, 2, 0.5, 0.5)
        mono, _ = audioop.ratecv(mono, 2, 1, 48000, 16000, None)
        speech = self.vad.is_speech(mono, 16000)
        self.metrics["vad_speech_frames" if speech else "vad_nonspeech_frames"] += 1
        s = self.users.get(uid)
        if s is None:
            if not speech or len(self.users) >= self.limits.participants:
                return None, 0
            s = self.users[uid] = Segment(start=now, recent=deque(maxlen=max(1, self.limits.end_ms // 20)))
        s.last = now
        s.pcm.extend(mono)
        s.recent.append(speech)
        s.dense.append(speech)
        s.dense_best = max(s.dense_best, sum(s.dense))
        if speech:
            s.speech_ms += 20
            s.run_ms += 20
            s.silence_ms = 0
        else:
            s.silence_ms += 20
            if s.silence_ms >= 60:
                s.run_ms = 0
        sustained = s.run_ms
        # A short VAD-negative gap can be a consonant or a quiet syllable.
        # Keep the bounded candidate until normal endpointing; minimum_ms still
        # rejects clicks/noise when the candidate finishes. run_ms above remains
        # consecutive speech, so pauses do not accidentally trigger natural barge-in.
        # Background noise (breathing, a fan, keys) makes the detector fire now and
        # then, so waiting for end_ms of unbroken silence kept every candidate open to
        # the max_seconds cap: noise went to STT as "speech", and a spoken "stop" was
        # only heard after he had finished. End once the last end_ms are mostly quiet.
        mostly_quiet = (
            len(s.recent) == s.recent.maxlen
            and sum(s.recent) < self.limits.endpoint_ratio * s.recent.maxlen
        )
        if s.silence_ms >= self.limits.end_ms or mostly_quiet or len(s.pcm) >= int(
            self.limits.max_seconds * 32000
        ):
            return self.finish(uid, now), sustained
        return None, sustained

    def finish(self, uid, now):
        s = self.users.pop(uid, None)
        if not s:
            return None
        if s.speech_ms < self.limits.minimum_ms:
            self.metrics["vad_short_segments_dropped"] += 1
            return None
        if s.dense_best < self.limits.dense_ratio * s.dense.maxlen:
            # enough detector hits in total, but never a dense stretch: scattered noise, not words
            self.metrics["vad_noise_segments_dropped"] += 1
            return None
        return Utterance(uid, bytes(s.pcm), s.start, now)

    def speaking(self):
        """People in the middle of real speech: an open candidate with a dense stretch, not just noise frames.

        Waiting for "nobody is talking" used to wait on any open candidate, and with background noise one is
        almost always open, so replies were held back (up to the stale limit) and then dropped.
        """
        need = self.limits.dense_ratio * 20
        return {uid for uid, s in self.users.items() if s.dense_best >= need}

    def expire(self, now):
        return [
            u
            for uid, s in list(self.users.items())
            if (now - s.last) * 1000 >= self.limits.end_ms
            for u in [self.finish(uid, now)]
            if u
        ]


# What Whisper writes for silence or noise (an audio-only model trained on subtitled video), never worth an answer.
_PHANTOM = re.compile(
    r"(?:thank you(?: (?:very|so) much)?(?: for (?:watching|listening))?|thanks(?: (?:for (?:watching|listening)|a lot))?"
    r"|(?:please )?(?:like and )?subscribe(?: to (?:my|the|our) channel)?|bye(?: bye)?|goodbye|you|so|uh|um|hmm+|mm+"
    r"|ah|oh|music|applause|laughter|silence|blank audio|no audio|inaudible"
    r"|(?:subtitles|captions|transcription|translation|translated|untertitel|sous titres|sottotitoli)(?: .*)?"
    r"|.*amara(?: org)?.*|ご視聴ありがとうございました|字幕.*)"
)


def is_phantom(text: str | None) -> bool:
    """True for empty text and for Whisper's stock silence/noise phrases ("Thank you.", "Thanks for watching!")."""
    t = re.sub(r"[\[\]()♪♫*~_\-]", " ", (text or "").lower())
    t = re.sub(r"[^\w.!?' ]+", " ", t)
    parts = [p.strip(" '.!?") for p in re.split(r"[.!?]+(?=\s|$)", t)]
    parts = [re.sub(r"\s+", " ", p) for p in parts if p.strip(" '.!?")]
    return all(_PHANTOM.fullmatch(p) for p in parts)


def heard_text(payload) -> str:
    """The words in a Whisper response, without what it makes up from silence or noise.

    With response_format=verbose_json every segment says how likely it is to be silence; a segment Whisper itself
    rates as probably silence (no_speech_prob > 0.6 with avg_logprob < -1, its own rule) or as a repetition loop
    (compression_ratio > 2.4) is dropped, and so is a whole result that is only a stock phrase.
    """
    if not isinstance(payload, dict):
        return ""
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        kept = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            try:
                no_speech = float(segment.get("no_speech_prob") or 0)
                logprob = float(segment.get("avg_logprob") or 0)
                compression = float(segment.get("compression_ratio") or 0)
            except (TypeError, ValueError):
                no_speech = logprob = compression = 0.0
            if (no_speech > 0.6 and logprob < -1.0) or compression > 2.4:
                continue
            kept.append(str(segment.get("text") or "").strip())
        text = " ".join(part for part in kept if part)
    else:
        text = payload.get("text", "")
        if not isinstance(text, str):
            return ""
    text = text.strip()[:2000]
    return "" if is_phantom(text) else text


class STTProvider(Protocol):
    supports_partials: bool

    async def transcribe(self, pcm: bytes) -> str: ...


class GroqSTT:
    """Final utterance STT, not a realtime/partial transcription API. No disk I/O."""

    supports_partials = False

    def __init__(self, key):
        self.key = key

    async def transcribe(self, pcm):
        import aiohttp

        if not self.key:
            raise RuntimeError("stt_not_configured")
        if not pcm or len(pcm) > 640000:
            return ""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        form = aiohttp.FormData()
        form.add_field(
            "file", buf.getvalue(), filename="utterance.wav", content_type="audio/wav"
        )
        form.add_field("model", "whisper-large-v3-turbo")
        # verbose_json carries per-segment silence probabilities (see heard_text); temperature 0 avoids the
        # sampled retries that produce most invented text
        form.add_field("response_format", "verbose_json")
        form.add_field("temperature", "0")
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as client:
                async with client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": "Bearer " + self.key},
                    data=form,
                ) as response:
                    if response.status != 200:
                        raise RuntimeError("stt_http_failure")
                    raw = await response.content.read(65537)
                    if len(raw) > 65536:
                        raise RuntimeError("stt_response_too_large")
                    import json

                    return heard_text(json.loads(raw))
        except asyncio.TimeoutError:
            raise RuntimeError("stt_timeout") from None


def chunks(text, size=320):
    """Sentence-first chunks, with word boundaries for an oversized sentence."""
    result, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text.strip()):
        for word in sentence.split():
            if current and len(current) + len(word) + 1 > size:
                result.append(current)
                current = ""
            current = (current + " " + word).strip()
        if len(current) >= 100:
            result.append(current)
            current = ""
    if current:
        result.append(current)
    return result


def spoken_pieces(text, opening=40, size=320):
    """chunks(), with the opening voiced on its own: the first sentence (or the first few, up to `opening`
    characters). He waited for a whole first chunk of 100 characters or more before saying anything; the pieces
    after the opening are made while it plays."""
    text = (text or "").strip()
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    head, count = "", 0
    for count, sentence in enumerate(sentences, 1):
        head = f"{head} {sentence}".strip()
        if len(head) >= opening:
            break
    if len(head) < opening or len(head) > size or count == len(sentences):
        return chunks(text, size)
    return [head] + chunks(" ".join(sentences[count:]), size)
