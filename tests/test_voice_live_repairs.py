"""Regressions for the November 2026 live-voice repairs (both bots share this file).

Owner's report: Scaramouche answers silence ("Thank you."), talks endlessly and ignores "stop" and "leave the
voice chat"; Wanderer never seems to hear and does not leave. Undoing any one of the repairs makes a test here
fail; the rest pin what the repairs must not change (negated or misdirected leave requests stay ignored).
"""

import asyncio
import queue
import time
from collections import Counter
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

import voice_conversation.session as session_module
from voice_conversation import names
from voice_conversation.session import Session, State
from voice_conversation.speech import GroqSTT, Limits, Segmenter, Utterance, heard_text, is_phantom

BOT = "wanderer" if hasattr(session_module, "names_wander_alias_in_opening") else "scaramouche"
SPEECH, QUIET = b"\x01\x01" * 1920, bytes(3840)
VAD = NS(is_speech=lambda pcm, rate: any(pcm))


def noise(i):
    """Background noise: the detector fires on 2 frames in every 9 (breathing, a fan, keys)."""
    return i % 9 in (0, 4)


def make_session(name=BOT, **kwargs):
    backend = NS(frames=queue.Queue(128), metrics=Counter(), unhealthy=lambda: False, stop=AsyncMock(), active=True)
    defaults = dict(
        channel_id=10, initiator=1, name=name, backend=backend,
        stt=NS(transcribe=AsyncMock(return_value="hello")),
        playback=NS(play=AsyncMock(return_value=True), stop=AsyncMock(), vc=NS(is_playing=lambda: False), source=None),
        respond=AsyncMock(return_value="A reply."), synthesize=AsyncMock(return_value=b"audio"),
        remember=AsyncMock(), notify=AsyncMock(), limits=Limits(), vad=VAD,
    )
    defaults.update(kwargs)
    s = Session(**defaults)
    s.participants = {1}
    return s


# ── names: who is being spoken to ────────────────────────────────────────────
def test_lines_for_the_other_bot_are_left_to_it_even_when_focused():
    async def check():  # a Session needs a running loop on Python 3.9
        s = make_session("scaramouche")
        for line in ("Can you hear me, Wanderer?", "Wonderer, can you hear me?", "Wanda, are you there?"):
            s.focus, s.focus_until = 1, time.monotonic() + 90
            assert not s.relevant(1, line), line
        s.focus, s.focus_until = 1, time.monotonic() + 90
        assert s.relevant(1, "Do you like Wanderer?")  # talking about him is not a handover

    asyncio.run(check())


def test_misheard_names_still_reach_the_right_bot():
    async def check():
        wanderer = make_session("wanderer")
        wanderer.focus = None
        for line in ("Wonderer, can you hear me?", "Can you hear me, Wanderer?", "Wander, are you there?"):
            assert wanderer.relevant(1, line), line
        assert not wanderer.relevant(1, "I wonder what he thinks.")
        scaramouche = make_session("scaramouche")
        scaramouche.focus = None
        for line in ("Scaramoosh, can you hear me?", "Scara Mouche, hello.", "Can you hear me, Scaramouch?"):
            assert scaramouche.relevant(1, line), line

    asyncio.run(check())


def test_spellings_nobody_listed_are_still_close_enough():
    assert names.addressed("Scaramuch, are you there?", "scaramouche")
    assert names.addressed("Are you there, Wanderar?", "wanderer")
    assert not names.mentions("I wondered about that.", "wanderer")
    assert not names.mentions("A scarecrow in the field.", "scaramouche")


def test_wander_and_the_next_word_do_not_spell_the_name():
    for line in ("Let's wander over there.", "Wander over here.", "We could wander around."):
        assert not names.mentions(line, "wanderer"), line


@pytest.mark.parametrize("bot,line,expected", [
    ("scaramouche", "Scaramouche, leave the voice chat.", True),
    ("scaramouche", "Leave the call, Scaramouche.", True),
    ("wanderer", "Wanderer, get out of the VC.", True),
    ("scaramouche", "Scaramouche, don't leave.", False),
    ("scaramouche", "Scaramouche, why would you leave the call?", False),
    ("wanderer", "Scaramouche, leave the voice chat.", False),
    ("scaramouche", "Leave the voice chat, Wanderer.", False),
    ("scaramouche", "Scaramouche and Wanderer, leave the call.", False),
    ("wanderer", "Scaramouche and Wanderer, leave the call.", False),
])
def test_natural_spoken_leave(bot, line, expected):
    assert names.leave_intent(line, bot) is expected


# ── segmenting: noise is not speech, and does not hold a sentence open ───────
def feed(seg, frames, start=0.0, uid=1):
    out = []
    for i, frame in enumerate(frames):
        utterance, _ = seg.feed(uid, frame, start + i * 0.02)
        if utterance:
            out.append(utterance)
    return out


def test_background_noise_alone_never_becomes_an_utterance():
    seg = Segmenter(Limits(), VAD)
    assert feed(seg, [SPEECH if noise(i) else QUIET for i in range(1500)]) == []  # 30 s of noise
    assert seg.metrics["vad_short_segments_dropped"] + seg.metrics["vad_noise_segments_dropped"] > 10


def test_denser_noise_is_not_speech_either():
    """3 frames in 10: enough detector hits in total to pass the minimum, but never a dense stretch like words."""
    seg = Segmenter(Limits(), VAD)
    assert feed(seg, [SPEECH if i % 10 in (0, 3, 6) else QUIET for i in range(1500)]) == []
    assert seg.metrics["vad_noise_segments_dropped"] > 10


def test_a_sentence_ends_soon_after_speech_even_with_noise_behind_it():
    seg = Segmenter(Limits(), VAD)
    frames = [SPEECH] * 50 + [SPEECH if noise(i) else QUIET for i in range(500)]  # 1 s of words, then noise
    out = feed(seg, frames)
    assert len(out) == 1
    assert out[0].ended - 1.0 < 1.2  # it used to stay open to the 12 s cap


def test_a_stop_inside_noise_is_cut_out_quickly():
    seg = Segmenter(Limits(), VAD)
    frames = [SPEECH if noise(i) else QUIET for i in range(150)] + [SPEECH] * 25 \
        + [SPEECH if noise(i) else QUIET for i in range(500)]
    out = feed(seg, frames)
    assert len(out) == 1
    stop_ended = (150 + 25) * 0.02
    assert out[0].ended - stop_ended < 1.2


def test_only_real_speech_counts_as_someone_speaking():
    seg = Segmenter(Limits(), VAD)
    feed(seg, [SPEECH, QUIET, QUIET, QUIET], uid=1)   # a noise frame opened a candidate
    feed(seg, [SPEECH] * 12, uid=2)                   # real words
    assert 1 in seg.users and seg.speaking() == {2}


# ── Whisper: confidence and its stock phantom phrases ────────────────────────
@pytest.mark.parametrize("text", ["Thank you.", "Thanks for watching!", "Thank you. Thank you.", "you", "[BLANK_AUDIO]", "♪♪"])
def test_whispers_silence_phrases_are_phantoms(text):
    assert is_phantom(text)


@pytest.mark.parametrize("text", ["Stop.", "Okay.", "Thank you, Scaramouche.", "Can you hear me, Wanderer?", "Hola, ¿qué tal?"])
def test_real_short_speech_is_kept(text):
    assert not is_phantom(text)


def test_segments_whisper_rates_as_silence_are_dropped():
    payload = {"text": "Thank you. Stop.", "segments": [
        {"text": " Thank you.", "no_speech_prob": 0.9, "avg_logprob": -1.3, "compression_ratio": 1.0},
        {"text": " Stop.", "no_speech_prob": 0.05, "avg_logprob": -0.2, "compression_ratio": 1.0},
    ]}
    assert heard_text(payload) == "Stop."
    loop = {"segments": [{"text": " the the the the the the", "no_speech_prob": 0.1, "avg_logprob": -0.3,
                          "compression_ratio": 3.1}]}
    assert heard_text(loop) == ""
    assert heard_text({"text": "hello"}) == "hello"
    # Whisper is often confident about its silence phrases, so the phrase itself is dropped too
    sure = {"segments": [{"text": " Thank you.", "no_speech_prob": 0.02, "avg_logprob": -0.1, "compression_ratio": 0.8}]}
    assert heard_text(sure) == ""
    assert heard_text({"text": " Thanks for watching!"}) == ""


def test_a_phantom_from_any_speech_to_text_gets_no_reply():
    async def check():
        s = make_session(stt=NS(transcribe=AsyncMock(return_value="Thank you.")))
        s.active = True
        s.focus, s.focus_until = 1, time.monotonic() + 90
        worker = asyncio.create_task(s.transcribe_loop())
        now = time.monotonic()
        s.enqueue(Utterance(1, b"pcm", now - 0.5, now))
        await asyncio.sleep(0.3)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert s.metrics["phantom_transcripts"] == 1
        s.respond.assert_not_awaited()

    asyncio.run(check())


def test_a_noise_frame_does_not_hold_back_the_reply():
    async def check():
        s = make_session()
        s.active = True
        s.focus, s.focus_until = 1, time.monotonic() + 90
        s.segmenter.feed(1, SPEECH, time.monotonic())  # one noise frame opens a candidate, which stays open
        assert 1 in s.segmenter.users and not s.segmenter.speaking()
        worker = asyncio.create_task(s.transcribe_loop())
        now = time.monotonic()
        s.enqueue(Utterance(1, b"pcm", now - 0.5, now))
        for _ in range(60):
            if s.respond.await_count:
                break
            await asyncio.sleep(0.025)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if s.response_task:
            s.response_task.cancel()
            await asyncio.gather(s.response_task, return_exceptions=True)
        assert s.respond.await_count == 1  # it used to wait for the candidate, up to the 15 s stale limit

    asyncio.run(check())


def test_whisper_is_asked_for_confidence(monkeypatch):
    fields = {}

    class Form:
        def add_field(self, name, value, **kwargs):
            fields[name] = value

    class CM:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            return False

    body = b'{"text": "Stop.", "segments": [{"text": "Stop.", "no_speech_prob": 0.01, "avg_logprob": -0.1}]}'
    response = NS(status=200, content=NS(read=AsyncMock(return_value=body)))
    monkeypatch.setattr("aiohttp.FormData", Form)
    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: CM(NS(post=Mock(return_value=CM(response)))))
    assert asyncio.run(GroqSTT("key").transcribe(bytes(640))) == "Stop."
    assert fields["response_format"] == "verbose_json" and fields["temperature"] == "0"


# ── the "interrupt me?" reaction when cut off between sentences ──────────────
def test_cut_off_between_sentences_still_counts_as_mid_reply():
    async def check():
        s = make_session()
        s.active = True
        s.response_task = asyncio.create_task(asyncio.sleep(10))
        s.state, s.completed, s.total = State.SYNTHESIZING, 1, 3   # voicing sentence 2 of 3
        await s.interrupt(1, time.monotonic())
        key = "playback_was_active" if BOT == "wanderer" else "was_speaking"
        assert s.interruption[key] is True
        s.response_task = asyncio.create_task(asyncio.sleep(10))
        s.state, s.completed, s.total = State.THINKING, 0, 0       # nothing said yet: no reaction
        await s.interrupt(1, time.monotonic())
        assert s.interruption[key] is False

    asyncio.run(check())


# ── a session that cannot hear leaves instead of sitting there deaf ──────────
def test_a_dead_receiver_makes_the_bot_leave():
    async def check():
        failed = AsyncMock()
        s = make_session(on_failed=failed)
        s.backend.unhealthy = lambda: True
        s.active = True
        await asyncio.wait_for(s.receive_loop(), 2)
        await asyncio.wait_for(s.leave_task, 2)
        failed.assert_awaited_once_with("receive_unhealthy")

    asyncio.run(check())


def test_speech_to_text_failing_for_a_minute_makes_the_bot_leave():
    async def check():
        failed = AsyncMock()
        s = make_session(on_failed=failed, stt=NS(transcribe=AsyncMock(side_effect=RuntimeError("stt_http_failure"))))
        s.active = True
        worker = asyncio.create_task(s.transcribe_loop())

        async def fail_once():
            now = time.monotonic()
            s.enqueue(Utterance(1, b"pcm", now, now))
            await asyncio.sleep(0.05)

        for _ in range(6):
            await fail_once()
        assert s.leave_task is None  # a burst of errors (a rate limit) is no reason to leave
        s.stt_failing_since -= 61    # ...failing for over a minute is
        await fail_once()
        await asyncio.wait_for(s.leave_task, 2)
        failed.assert_awaited_once_with("stt_unavailable")
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(check())


def test_one_success_resets_the_failure_count():
    async def check():
        results = [RuntimeError("stt_http_failure")] * 4 + ["hello"] + [RuntimeError("stt_http_failure")] * 4
        s = make_session(on_failed=AsyncMock(), stt=NS(transcribe=AsyncMock(side_effect=results)))
        s.active = True
        worker = asyncio.create_task(s.transcribe_loop())
        for i in range(9):
            if i == 4:
                s.stt_failing_since -= 61  # the first failures were long ago, but the success next starts a new count
            now = time.monotonic()
            s.enqueue(Utterance(1, b"pcm", now, now))
            await asyncio.sleep(0.05)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        assert s.stt_failures == 4 and s.leave_task is None

    asyncio.run(check())


def test_the_integration_disconnects_and_says_why(monkeypatch):
    from voice_conversation.integration import VoiceConversation

    async def check():
        service = VoiceConversation(NS(add_listener=Mock(), close=AsyncMock()), NS(), BOT, AsyncMock(), AsyncMock(), "key")
        channel = NS(send=AsyncMock())
        session = NS(backend=NS(vc=NS(channel=channel)))
        service.sessions[5] = session
        service.leave = AsyncMock()
        await service.failed_leave(5, session, "receive_unhealthy")
        service.leave.assert_awaited_once_with(5, announce=False)
        assert "!join" in channel.send.await_args.args[0]

    asyncio.run(check())


def test_joining_hands_failures_to_the_integration(monkeypatch):
    from test_voice_integration import setup
    from voice_conversation.integration import ReceiveBackend

    async def check():
        service, ctx, channel = setup(monkeypatch)
        channel.members = [ctx.author]
        vc = NS(channel=channel, disconnect=AsyncMock(), listen=Mock(), is_listening=lambda: False)
        channel.connect = AsyncMock(return_value=vc)
        monkeypatch.setattr("voice_conversation.integration.shutil.which", lambda n: "/test/ffmpeg")
        monkeypatch.setattr(ReceiveBackend, "client_class", lambda self: object)

        async def start_backend(backend, client):
            backend.vc, backend.active = client, True

        async def start_session(session):
            session.active = True

        monkeypatch.setattr(ReceiveBackend, "start", start_backend)
        monkeypatch.setattr(Session, "start", start_session)
        monkeypatch.setattr(session_module, "Segmenter", lambda *a: NS(users={}, metrics={}))  # no webrtcvad needed
        try:
            await service.command(ctx, "start")
            session = service.sessions[5]
            service.failed_leave = AsyncMock()
            await session.on_failed("stt_unavailable")
            service.failed_leave.assert_awaited_once_with(5, session, "stt_unavailable")
        finally:
            service.sessions.clear()
            await service.bot.close()

    asyncio.run(check())


def test_a_greeting_is_not_held_back_by_noise(monkeypatch):
    from test_voice_integration import setup

    async def check():
        service, ctx, channel = setup(monkeypatch)
        monkeypatch.setattr("voice_conversation.integration.random.random", lambda: 0.75)
        service.bot._connection.user = NS(id=9)
        ctx.author.guild = ctx.guild
        noisy = NS(users={2: object()}, speaking=lambda: set())  # someone's fan opened a candidate, nobody talks
        session = NS(
            active=True, channel_id=10, participants=set(), limits=NS(participants=4),
            backend=NS(vc=NS(channel=channel)), busy=lambda: False, segmenter=noisy, jobs=asyncio.Queue(),
            submit=AsyncMock(return_value=True), decision=Mock(),
        )
        session.consent = Mock(side_effect=lambda uid, enabled: session.participants.add(uid))
        service.sessions[5] = session
        try:
            await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
            await asyncio.wait_for(asyncio.gather(*list(service.greeting_tasks.values())), 3)
            session.submit.assert_awaited_once()
        finally:
            service.sessions.clear()
            await service.bot.close()

    asyncio.run(check())


# ── end to end in real time: noise after a reply, and "stop" said over noise ──
class _Player:
    def __init__(self):
        self.vc, self.source, self._task = NS(is_playing=lambda: self._task is not None and not self._task.done()), None, None

    async def play(self, audio):
        self.source = object()
        self._task = asyncio.ensure_future(asyncio.sleep(len(audio) / 1000))
        try:
            await self._task
            return True
        except asyncio.CancelledError:
            if self._task.cancelled():
                return False
            raise
        finally:
            self.source = None

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()


def _live_session(script, replies):
    heard = []

    async def transcribe(pcm):
        await asyncio.sleep(0.2)
        text = script.pop(0) if script else "Thank you."  # what Whisper says for noise
        heard.append(text)
        return text

    async def respond(uid, text, context):
        replies.append(text)
        return "Listen closely. " * 8

    s = make_session(stt=NS(transcribe=transcribe), playback=_Player(), respond=respond,
                     synthesize=AsyncMock(side_effect=lambda uid, text: b"x" * 1500))
    return s, heard


async def _talk(s, frames):
    for frame in frames:
        s.backend.frames.put_nowait((1, frame, time.monotonic()))
        await asyncio.sleep(0.02)


def test_noise_after_a_reply_gets_no_more_replies():
    async def check():
        replies = []
        s, heard = _live_session([f"{BOT.capitalize()}, hello."], replies)
        await s.start()
        try:
            await _talk(s, [SPEECH] * 40 + [QUIET] * 50)
            await asyncio.sleep(1.5)
            await _talk(s, [SPEECH if noise(i) else QUIET for i in range(300)])  # 6 s of noise
            await asyncio.sleep(0.5)
            assert replies == [f"{BOT.capitalize()}, hello."]
        finally:
            await s.stop()

    asyncio.run(check())


def test_stop_said_over_background_noise_interrupts_quickly():
    async def check():
        replies = []
        s, heard = _live_session([f"{BOT.capitalize()}, tell me a long story.", "Stop."], replies)
        await s.start()
        try:
            await _talk(s, [SPEECH] * 40 + [QUIET] * 50)
            while s.state != State.BOT_SPEAKING:
                await asyncio.sleep(0.02)
            await _talk(s, [SPEECH if noise(i) else QUIET for i in range(50)])
            started = time.monotonic()
            await _talk(s, [SPEECH] * 25)
            noise_task = asyncio.ensure_future(_talk(s, [SPEECH if noise(i) else QUIET for i in range(300)]))
            while not s.metrics["cancelled_responses"] and time.monotonic() - started < 6:
                await asyncio.sleep(0.02)
            noise_task.cancel()
            assert s.metrics["cancelled_responses"] == 1
            assert time.monotonic() - started < 3  # it used to be heard only after he had finished
        finally:
            await s.stop()

    asyncio.run(check())
