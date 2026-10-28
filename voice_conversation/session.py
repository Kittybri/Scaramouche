"""Single-owner asyncio session; receive threads only enqueue bounded PCM."""

from __future__ import annotations

import asyncio
import contextlib
from difflib import SequenceMatcher
import io
import queue
import re
import time
from collections import Counter, deque
from enum import Enum

from .speech import Limits, Segmenter, chunks


def is_interrupt_keyword(text: str, name: str) -> bool:
    """Recognize short spoken barge-in commands despite ordinary STT variation."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    while words and words[0] in {"hey", "okay", "ok", "um", "uh", "please"}:
        words.pop(0)
    if not words:
        return False

    # A command may follow the character's name or a little ASR filler. Keep the
    # window short so a later conversational use of these words is not a barge-in.
    opening = words[:6]
    if any(word in {"wait", "stop", "no", "listen"} for word in opening):
        return True
    if any(
        opening[index : index + 2] in (["hold", "on"], ["shut", "up"])
        for index in range(max(0, len(opening) - 1))
    ):
        return True

    aliases = {name.lower()}
    if name.lower() == "scaramouche":
        aliases.add("scara")
    # Preserve the old name-only barge-in while accepting common ways speech
    # recognition splits or slightly misspells the character name.
    for width in range(1, min(3, len(opening)) + 1):
        candidate = "".join(opening[:width])
        for alias in aliases:
            if candidate == alias:
                return True
            if len(candidate) >= 7 and SequenceMatcher(None, candidate, alias).ratio() >= 0.78:
                return True
    return False


def is_spoken_leave_command(text: str, name: str) -> bool:
    """Only the character's exact spoken leave phrase ends a live session."""
    # ASR may insert punctuation (notably a comma after the name), but no
    # extra or missing spoken words are accepted.
    return re.findall(r"[a-z0-9]+", (text or "").lower()) == [
        name.lower(), "leave", "voice", "channel"
    ]


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    USER_SPEAKING = "USER_SPEAKING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SYNTHESIZING = "SYNTHESIZING"
    BOT_SPEAKING = "BOT_SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    CANCELLED = "CANCELLED"
    RECOVERING = "RECOVERING"


class Playback:
    def __init__(self, vc):
        self.vc, self.done, self.source = vc, None, None

    async def play(self, audio):
        import discord

        if not audio or len(audio) > 8_000_000 or self.vc.is_playing():
            raise RuntimeError("playback_unavailable")
        loop = asyncio.get_running_loop()
        done = self.done = loop.create_future()
        source = self.source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)

        def complete(error):
            if not done.done():
                done.set_result(not bool(error))

        try:
            self.vc.play(
                source, after=lambda error: loop.call_soon_threadsafe(complete, error)
            )
            return await asyncio.wait_for(asyncio.shield(done), 90)
        finally:
            if self.done is done:
                await self.stop()
                self.done = self.source = None
            source.cleanup()

    async def stop(self):
        # VoiceRecvClient.stop() also stops LISTENING. Never call it here.
        if self.source is not None:
            player = getattr(self.vc, "_player", None)
            getattr(self.vc, "stop_playing", self.vc.stop)()
            # Wait for the send thread to exit before another chunk can start.
            if player is not None:
                await asyncio.to_thread(player.join, 1)
                if player.is_alive():
                    raise RuntimeError("playback_stop_timeout")
        if self.done and not self.done.done():
            self.done.set_result(False)


class Session:
    def __init__(
        self,
        *,
        channel_id,
        initiator,
        name,
        backend,
        stt,
        playback,
        respond,
        synthesize,
        remember,
        notify,
        on_spoken_leave=None,
        limits=Limits(),
        vad=None,
    ):
        self.channel_id, self.initiator, self.name = channel_id, initiator, name.lower()
        self.backend, self.stt, self.playback = backend, stt, playback
        self.respond, self.synthesize, self.remember, self.notify = (
            respond,
            synthesize,
            remember,
            notify,
        )
        self.on_spoken_leave = on_spoken_leave
        self.leave_requested = False
        self.leave_task = None
        self.limits, self.segmenter = limits, Segmenter(limits, vad)
        self.participants, self.epochs = set(), Counter()
        self.focus, self.focus_until = initiator, 0.0
        self.mode, self.interrupt_mode = "CONVERSATION", "KEYWORD"
        self.user_interrupt = {}
        self.state, self.active = State.IDLE, False
        self.generation, self.current_user = 0, None
        self.response_task = None
        self.pending_response_task = None
        self.pending_response_user = None
        self.workers = []
        self.jobs = asyncio.Queue(maxsize=limits.stt_queue)
        self.provider_slots = {"llm": asyncio.Lock(), "tts": asyncio.Lock()}
        self.providers = set()
        self.metrics = Counter()
        self.events = deque(maxlen=16)
        self.interruption = None
        self.completed, self.total = 0, 0
        self.last_speech, self.started = 0.0, time.monotonic()
        self.reported_failures = set()
        self.recent_outputs = deque(maxlen=4)
        self.feature_router = None

    def busy(self):
        return bool(self.response_task and not self.response_task.done())

    def clear_pending_response(self):
        task = self.pending_response_task
        self.pending_response_task = self.pending_response_user = None
        if task and not task.done():
            task.cancel()
            self.metrics["dropped_pending_responses"] += 1
        return task

    def decision(self, kind, uid):
        """Bounded diagnostics: categories and attribution, never speech content."""
        self.metrics[kind] += 1
        self.events.append(
            {"type": kind, "at": time.monotonic(), "speaker_id": uid}
        )

    async def feature_event(self, kind, uid=0, text="", data=None):
        if not self.feature_router:
            return {}
        from .personality import VoiceEvent

        try:
            return (
                await self.feature_router.handle_event(
                    VoiceEvent(kind, uid, text, data)
                )
                or {}
            )
        except Exception:
            self.metrics["feature_errors"] += 1
            return {}

    async def submit(
        self, uid, text, *, context="", reply=None, audio=None, feature_kind=""
    ):
        """Structured feature output uses the SAME generation/playback lifecycle."""
        if not self.active or uid not in self.participants or self.busy():
            return False
        self.current_user = uid
        self.generation += 1
        self.response_task = asyncio.create_task(
            self.answer(
                uid,
                text,
                self.generation,
                self.epochs[uid],
                feature_context=context,
                reply_override=reply,
                audio_override=audio,
                feature_kind=feature_kind,
            )
        )
        return True

    def consent(self, uid, enabled):
        if enabled:
            if (
                len(self.participants) >= self.limits.participants
                and uid not in self.participants
            ):
                raise ValueError("Participant limit reached")
            self.participants.add(uid)
            if hasattr(self.backend, "grant"):
                self.backend.grant(uid)
        else:
            self.participants.discard(uid)
            if self.pending_response_user == uid:
                self.clear_pending_response()
            if self.feature_router:
                self.feature_router.revoke(uid)
            self.user_interrupt.pop(uid, None)
            self.epochs[uid] += 1
            self.segmenter.users.pop(uid, None)
            if hasattr(self.backend, "revoke"):
                self.backend.revoke(uid)
            # Drop all queued PCM (bounded, safest for revoked/rejoined IDs).
            while not self.backend.frames.empty():
                with contextlib.suppress(queue.Empty):
                    self.backend.frames.get_nowait()
            retained = []
            while not self.jobs.empty():
                job = self.jobs.get_nowait()
                if job.user_id != uid:
                    retained.append(job)
            for job in retained:
                self.jobs.put_nowait(job)
            if uid == self.current_user:
                self.generation += 1
                if self.response_task:
                    self.response_task.cancel()
            if uid == self.focus:
                self.focus, self.focus_until = None, 0

    async def start(self):
        self.active, self.state = True, State.LISTENING
        self.workers = [
            asyncio.create_task(self.receive_loop()),
            asyncio.create_task(self.transcribe_loop()),
        ]

    async def failure(self, category):
        self.metrics[category] += 1
        if category not in self.reported_failures:
            self.reported_failures.add(category)
            with contextlib.suppress(Exception):
                await self.notify(category)

    def enqueue(self, utterance):
        if utterance.user_id not in self.participants:
            return
        utterance.epoch = self.epochs[utterance.user_id]
        self.metrics["vad_segments"] += 1
        self.metrics["utterance_ms"] = round(len(utterance.pcm) / 32)
        if self.jobs.full():
            self.jobs.get_nowait()
            self.metrics["dropped_stale_jobs"] += 1
        self.jobs.put_nowait(utterance)

    async def speech(self, uid, sustained, started):
        self.last_speech = time.monotonic()
        if uid not in self.participants or sustained < self.limits.barge_ms:
            return
        mode = self.user_interrupt.get(uid, self.interrupt_mode)
        if mode == "NATURAL" and uid == self.focus:
            await self.interrupt(uid, started)

    async def receive_loop(self):
        try:
            while self.active:
                if self.backend.unhealthy():
                    self.state = State.RECOVERING
                    await self.backend.stop()
                    await self.cancel_response()
                    self.participants.clear()
                    self.segmenter.users.clear()
                    while not self.jobs.empty():
                        self.jobs.get_nowait()
                    await self.failure("receive_unhealthy")
                    # Explicit leave/start required; never silently restart consent.
                    self.active = False
                    return
                for _ in range(self.limits.frame_queue):
                    try:
                        uid, pcm, stamp = self.backend.frames.get_nowait()
                    except queue.Empty:
                        break
                    if uid not in self.participants or time.monotonic() - stamp > 1:
                        continue
                    utterance, sustained = self.segmenter.feed(uid, pcm, stamp)
                    segment = self.segmenter.users.get(uid)
                    if sustained >= self.limits.start_ms:
                        if not self.response_task or self.response_task.done():
                            self.state = State.USER_SPEAKING
                        await self.speech(
                            uid, sustained, stamp - max(0, sustained - 20) / 1000
                        )
                    if utterance:
                        self.enqueue(utterance)
                for utterance in self.segmenter.expire(time.monotonic()):
                    self.enqueue(utterance)
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.state = State.RECOVERING
            await self.backend.stop()
            await self.cancel_response()
            await self.failure("receive_unhealthy")
            self.active = False

    def relevant(self, uid, text):
        # An explicit handover releases this bot's follow-up focus. Merely
        # discussing the other character is not a handover.
        partner = r"wanderer|hat[ -]?guy" if self.name == "scaramouche" else r"scaramouche|scara|balladeer"
        if re.match(r"^(?:(?:hey|okay|ok|hi|hello)\W+)?(?:" + partner + r")\b", text.strip(), re.I):
            if uid == self.focus:
                self.focus, self.focus_until = None, 0
            return False
        addressed = bool(re.search(r"\b" + re.escape(self.name) + r"\b", text.lower()))
        addressed = addressed or (
            self.name == "scaramouche" and bool(re.search(r"\bscara\b", text.lower()))
        )
        interrupted = self.interruption and self.interruption["user_id"] == uid
        focused = uid == self.focus and time.monotonic() < self.focus_until
        return bool(
            addressed
            or interrupted
            or self.mode == "ACTIVE_ROOM"
            or (self.mode == "CONVERSATION" and focused)
        )

    async def transcribe_loop(self):
        while self.active:
            utterance = await self.jobs.get()
            uid, epoch = utterance.user_id, utterance.epoch
            if (
                self.leave_requested
                or uid not in self.participants
                or epoch != self.epochs[uid]
                or time.monotonic() - utterance.ended > self.limits.stale_seconds
            ):
                self.metrics["dropped_stale_jobs"] += 1
                self.decision("discarded_before_stt", uid)
                utterance.pcm = b""
                continue
            started = time.monotonic()
            self.metrics["stt_queue_wait_ms"] = max(0, round((started - utterance.ended) * 1000))
            self.metrics["speech_to_stt_ms"] = max(0, round((started - utterance.started) * 1000))
            if not self.response_task or self.response_task.done():
                self.state = State.TRANSCRIBING
            try:
                text = await asyncio.wait_for(self.stt.transcribe(utterance.pcm), 12)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.decision("transcription_failed", uid)
                await self.failure("stt_unavailable")
                self.state = State.LISTENING
                continue
            finally:
                utterance.pcm = b""
            self.metrics["stt_latency_ms"] = round((time.monotonic() - started) * 1000)
            if (
                not self.active
                or uid not in self.participants
                or epoch != self.epochs[uid]
            ):
                continue
            if not text.strip():
                self.metrics["empty_transcripts"] += 1
                self.decision("discarded_empty_transcript", uid)
                continue
            self.metrics["stt_success"] += 1
            self.events.append(
                {"type": "stt_complete", "at": time.monotonic(), "speaker_id": uid}
            )
            # Acoustic echo from a participant's loudspeaker is still human-attributed.
            normalized = re.sub(r"\W+", " ", text.lower()).strip()
            if normalized and any(normalized == old for old in self.recent_outputs):
                self.metrics["echo_dropped"] += 1
                self.decision("discarded_echo", uid)
                continue
            if self.leave_requested:
                continue
            mode = self.user_interrupt.get(uid, self.interrupt_mode)
            if mode == "KEYWORD" and uid == self.focus:
                keyword = is_interrupt_keyword(text, self.name)
                if keyword:
                    self.decision("interrupt_keyword_detected", uid)
                    if self.busy():
                        self.metrics["interrupt_attempted"] += 1
                        await self.interrupt(uid, utterance.started)
                    else:
                        self.decision("interrupt_arrived_after_playback", uid)
                elif self.busy():
                    self.decision("interrupt_keyword_missed", uid)
            continuing = bool(
                re.search(r"(?:maybe|because|and|but|so|\.\.\.)\s*$", text, re.I)
            )
            continuing = continuing or uid in self.segmenter.users
            # Wait through overlapping humans/continuation pauses before replying.
            while self.active and self.segmenter.users:
                await asyncio.sleep(0.05)
                if time.monotonic() - utterance.ended > self.limits.stale_seconds:
                    break
            if time.monotonic() - utterance.ended > self.limits.stale_seconds:
                self.metrics["dropped_stale_jobs"] += 1
                self.decision("discarded_after_speech_wait", uid)
                continue
            if re.search(r"(?:maybe|because|and|but|so|\.\.\.)\s*$", text, re.I):
                await asyncio.sleep(0.45)
            # One bounded same-speaker continuation. Other speakers retain their
            # own jobs; their audio/text is never merged into this user's turn.
            if continuing and not self.jobs.empty():
                queued = []
                follow = None
                while not self.jobs.empty():
                    job = self.jobs.get_nowait()
                    if (
                        follow is None
                        and job.user_id == uid
                        and job.epoch == epoch
                        and job.started - utterance.ended <= 2
                    ):
                        follow = job
                    else:
                        queued.append(job)
                for job in queued:
                    self.jobs.put_nowait(job)
                if follow:
                    try:
                        more = await asyncio.wait_for(
                            self.stt.transcribe(follow.pcm), 12
                        )
                        text = (text + " " + more).strip()[:4000]
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await self.failure("stt_unavailable")
                    finally:
                        follow.pcm = b""
            # Route the FINAL same-speaker text, never a partial sentence whose
            # continuation may be urgent/sensitive. No feature repeats STT.
            if (
                not self.active
                or uid not in self.participants
                or epoch != self.epochs[uid]
            ):
                continue
            if self.on_spoken_leave and is_spoken_leave_command(text, self.name):
                self.leave_requested = True
                self.decision("spoken_leave_requested", uid)
                self.leave_task = asyncio.create_task(self.on_spoken_leave(uid))
                continue
            plan = await self.feature_event("utterance_completed", uid, text)
            if (
                not self.relevant(uid, text)
                and "reply" not in plan
                and not plan.get("force_reply")
            ):
                self.decision("discarded_not_addressed", uid)
                if not self.busy() and not self.segmenter.users:
                    self.state = State.LISTENING
                continue
            # Never wait for playback inside the STT worker: the next utterance
            # may be a stop command. Keep only the latest bounded pending reply.
            self.clear_pending_response()
            if self.busy():
                self.metrics["deferred_responses"] += 1
                self.pending_response_user = uid
                self.pending_response_task = asyncio.create_task(
                    self.dispatch_response(uid, text, epoch, utterance.ended, plan)
                )
            else:
                await self.dispatch_response(uid, text, epoch, utterance.ended, plan)

    async def dispatch_response(self, uid, text, epoch, ended, plan):
        try:
            while self.active and self.busy():
                # asyncio.wait does not cancel playback when this pending turn
                # is superseded/revoked. OFF mode therefore remains non-interrupting.
                remaining = self.limits.stale_seconds - (time.monotonic() - ended)
                if remaining <= 0:
                    self.decision("discarded_before_response", uid)
                    return
                await asyncio.wait({self.response_task}, timeout=remaining)
            if (
                not self.active
                or uid not in self.participants
                or epoch != self.epochs[uid]
                or time.monotonic() - ended > self.limits.stale_seconds
            ):
                self.decision("discarded_before_response", uid)
                return
            self.focus, self.focus_until = uid, time.monotonic() + 90
            self.decision("response_scheduled", uid)
            self.current_user = uid
            self.generation += 1
            self.response_task = asyncio.create_task(
                self.answer(
                    uid,
                    text,
                    self.generation,
                    epoch,
                    feature_context=plan.get("context", ""),
                    reply_override=plan.get("reply"),
                    feature_kind=plan.get("feature", ""),
                )
            )
        finally:
            if self.pending_response_task is asyncio.current_task():
                self.pending_response_task = self.pending_response_user = None

    def current(self, generation, uid, epoch):
        return (
            self.active
            and generation == self.generation
            and uid in self.participants
            and epoch == self.epochs[uid]
        )

    async def provider(self, kind, call):
        # Canceled SDK executor work may run on. Keep its slot occupied until it
        # finishes: at most one outstanding provider job/type, never a task storm.
        lock = self.provider_slots[kind]
        await asyncio.wait_for(lock.acquire(), self.limits.stale_seconds)

        async def run():
            try:
                return await call()
            finally:
                lock.release()

        task = asyncio.create_task(run())
        self.providers.add(task)

        def finished(t):
            self.providers.discard(t)
            if not t.cancelled():
                t.exception()  # retrieve errors from abandoned/stale jobs

        task.add_done_callback(finished)
        return await asyncio.wait_for(asyncio.shield(task), 90)

    async def answer(
        self,
        uid,
        text,
        generation,
        epoch,
        *,
        feature_context="",
        reply_override=None,
        audio_override=None,
        feature_kind="",
    ):
        start = time.monotonic()
        self.completed, self.total = 0, 0
        context = "Voice conversation: output spoken dialogue only; no narration or stage directions. No artificial sentence limit."
        context += " " + feature_context
        if self.interruption and self.interruption["user_id"] == uid:
            context += (
                " You were interrupted; only completed chunks were heard. Respond to the new utterance naturally. "
                "Normal interruptions are not grievances; genuine distress takes priority."
            )
            context += (
                f" Completed chunk fraction: {self.interruption['progress']:.2f}."
            )
            self.interruption = None
        try:
            if feature_kind and (
                not self.feature_router
                or not await self.feature_router.permit(uid, feature_kind)
            ):
                return
            if audio_override is not None:
                if self.current(generation, uid, epoch):
                    self.state = State.BOT_SPEAKING
                    await asyncio.wait_for(self.playback.play(audio_override), 2.5)
                return
            self.state = State.THINKING
            tick = time.monotonic()
            reply = (
                reply_override
                if reply_override is not None
                else await self.provider(
                    "llm", lambda: self.respond(uid, text, context)
                )
            )
            self.metrics["response_latency_ms"] = round(
                (time.monotonic() - tick) * 1000
            )
            if not self.current(generation, uid, epoch):
                self.metrics["dropped_stale_jobs"] += 1
                return
            pieces = chunks(reply[:12000])
            if not pieces:
                self.decision("empty_response", uid)
            self.total = len(pieces)
            for part in pieces:
                if not self.current(generation, uid, epoch):
                    return
                self.state = State.SYNTHESIZING
                tick = time.monotonic()
                audio = await self.provider("tts", lambda: self.synthesize(uid, part))
                self.metrics["tts_latency_ms"] = round((time.monotonic() - tick) * 1000)
                if not self.current(generation, uid, epoch):
                    self.metrics["dropped_stale_jobs"] += 1
                    return
                if feature_kind and not await self.feature_router.permit(
                    uid, feature_kind
                ):
                    return
                if not audio:
                    raise RuntimeError("tts_unavailable")
                self.state = State.BOT_SPEAKING
                if not self.completed:
                    self.metrics["first_audio_ms"] = round(
                        (time.monotonic() - start) * 1000
                    )
                if reply_override is not None:
                    delivered = await asyncio.wait_for(self.playback.play(audio), 12)
                else:
                    delivered = await self.playback.play(audio)
                audio = None
                if not delivered or not self.current(generation, uid, epoch):
                    self.decision("playback_not_delivered", uid)
                    return
                self.completed += 1
                if self.focus == uid:
                    self.focus_until = time.monotonic() + 90
                self.decision("playback_completed", uid)
                self.metrics["spoken_chunks"] += 1
                self.recent_outputs.append(re.sub(r"\W+", " ", part.lower()).strip())
                await self.remember(uid, part)
                await self.feature_event(
                    "bot_spoken",
                    uid,
                    part,
                    {"duo": feature_context.startswith("STRUCTURED VOICE DUO:")},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            if audio_override is not None or reply_override is not None:
                with contextlib.suppress(Exception):
                    await self.playback.stop()
            await self.failure("response_or_playback_unavailable")
        finally:
            if generation == self.generation and self.active:
                self.state = State.LISTENING

    async def interrupt(self, uid, speech_started):
        if (
            uid not in self.participants
            or not self.response_task
            or self.response_task.done()
        ):
            return
        confirmed = time.monotonic()
        self.clear_pending_response()
        self.state = State.INTERRUPTING
        self.generation += 1
        self.response_task.cancel()
        stopped_at = time.monotonic()
        try:
            await self.playback.stop()
        except Exception:
            self.state = State.RECOVERING
            await self.failure("playback_stop_failed")
            return
        actually_stopped = time.monotonic()
        self.interruption = {
            "type": "voice_interrupted",
            "user_id": uid,
            "progress": self.completed / max(1, self.total),
            "speech_detected_at": speech_started,
            "confirmed_at": confirmed,
            "stop_called_at": stopped_at,
            "playback_stopped_at": actually_stopped,
        }
        self.events.append(dict(self.interruption))
        await self.feature_event("bot_interrupted", uid, data=dict(self.interruption))
        self.metrics["cancelled_responses"] += 1
        self.metrics["detection_ms"] = round((confirmed - speech_started) * 1000)
        self.metrics["stop_call_ms"] = round((actually_stopped - stopped_at) * 1000)
        self.metrics["barge_in_ms"] = round((actually_stopped - speech_started) * 1000)
        self.state = State.LISTENING

    async def cancel_response(self):
        pending = self.clear_pending_response()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        self.generation += 1
        if self.response_task:
            self.response_task.cancel()
        with contextlib.suppress(Exception):
            await self.playback.stop()
        if self.response_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.response_task

    async def stop(self):
        self.active = False
        leave_task = self.leave_task
        if leave_task and leave_task is not asyncio.current_task():
            leave_task.cancel()
        await self.feature_event("session_stopped")
        self.state = State.CANCELLED
        self.participants.clear()
        with contextlib.suppress(Exception):
            await self.backend.stop()
        await self.cancel_response()
        if leave_task and leave_task is not asyncio.current_task():
            await asyncio.gather(leave_task, return_exceptions=True)
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        for task in self.providers:
            task.cancel()
        await asyncio.gather(*list(self.providers), return_exceptions=True)
        self.segmenter.users.clear()
        while not self.jobs.empty():
            self.jobs.get_nowait()
        self.interruption = None
        self.events.clear()
        self.recent_outputs.clear()
        self.state = State.IDLE

    def status(self):
        received = self.backend.metrics
        return {
            "state": self.state.value,
            "connected": bool(
                getattr(
                    getattr(self.backend, "vc", None), "is_connected", lambda: False
                )()
            ),
            "listening": self.active and self.backend.active,
            "receive_proven": bool(
                received["dave_frames"]
                and received["pcm_frames"]
                and self.metrics["stt_success"]
            ),
            "mode": self.mode,
            "interrupt": self.interrupt_mode,
            "participants": len(self.participants),
            "focused_users": int(self.focus in self.participants),
            "pending_audio": self.jobs.qsize(),
            "workers": [
                {
                    "name": name,
                    "running": not task.done(),
                    "failed": task.done()
                    and not task.cancelled()
                    and task.exception() is not None,
                }
                for name, task in zip(("receive", "transcribe"), self.workers)
            ],
            "metrics": dict(received) | dict(self.metrics) | dict(self.segmenter.metrics),
            "receiver": self.backend.health() if hasattr(self.backend, "health") else {},
        }
