import asyncio
import queue
import time
from collections import Counter
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from voice_conversation.receive import ReceiveBackend, eligible
from voice_conversation.session import Session, State, Playback, is_interrupt_keyword, is_spoken_leave_command
from voice_conversation.speech import Limits, Segmenter, Utterance, chunks, GroqSTT


def run(coro):
    return asyncio.run(coro)


def member(uid=1, bot=False, channel=10):
    return NS(id=uid, bot=bot, voice=NS(channel=NS(id=channel)))


def setup(**kwargs):
    backend = NS(
        frames=queue.Queue(128),
        metrics=Counter(),
        unhealthy=lambda: False,
        stop=AsyncMock(),
        active=True,
    )
    defaults = dict(
        channel_id=10,
        initiator=1,
        name="scaramouche",
        backend=backend,
        stt=NS(transcribe=AsyncMock(return_value="Scaramouche, hello")),
        playback=NS(play=AsyncMock(return_value=True), stop=AsyncMock()),
        respond=AsyncMock(return_value="A complete answer."),
        synthesize=AsyncMock(return_value=b"audio"),
        remember=AsyncMock(),
        notify=AsyncMock(),
        vad=NS(is_speech=lambda pcm, rate: any(pcm)),
    )
    defaults.update(kwargs)
    session = Session(**defaults)
    session.consent(1, True)
    session.active = True
    return session


@pytest.mark.parametrize("name,spoken,expected", [
    ("scaramouche", "Scaramouche leave voice channel", True),
    ("scaramouche", "scaramouche leave voice channel.", True),
    ("scaramouche", "Scaramouche, leave voice channel.", True),
    ("wanderer", "Wanderer leave voice channel", True),
    ("scaramouche", "scara leave voice channel", False),
    ("scaramouche", "hey scaramouche leave voice channel", False),
    ("scaramouche", "wanderer leave voice channel", False),
    ("wanderer", "wanderer leave vc", False),
    ("wanderer", "wanderer please leave voice channel", False),
])
def test_spoken_leave_phrase_is_exact(name, spoken, expected):
    assert is_spoken_leave_command(spoken, name) is expected


def test_exact_spoken_leave_consumes_transcript_without_normal_reply():
    async def check():
        called = asyncio.Event()

        async def leave(uid):
            assert uid == 1
            called.set()

        s = setup(on_spoken_leave=leave)
        s.stt.transcribe.return_value = "Scaramouche leave voice channel."
        worker = asyncio.create_task(s.transcribe_loop())
        s.enqueue(Utterance(1, b"raw", time.monotonic(), time.monotonic()))
        await asyncio.wait_for(called.wait(), 1)
        assert s.metrics["spoken_leave_requested"] == 1
        s.respond.assert_not_awaited()
        await s.stop()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    run(check())


@pytest.mark.parametrize(
    "uid,bot,channel,expected",
    [
        (1, False, 10, True),
        (1, True, 10, False),
        (9, False, 10, False),
        (1, False, 11, False),
        (2, False, 10, False),
    ],
)
def test_attribution(uid, bot, channel, expected):
    assert eligible(member(uid, bot, channel), 9, 10, {1}) is expected


def test_receive_bounds_privacy_health():
    async def check():
        b = ReceiveBackend(lambda: {1}, size=2)
        b.vc = NS(
            client=NS(user=NS(id=9)), channel=NS(id=10), is_listening=lambda: False
        )
        b.accept(member(), b"x" * 3840)
        assert b.frames.empty()  # disabled
        b.active = True
        b.accept(member(bot=True), b"x" * 3840)
        b.accept(member(), b"x")
        for _ in range(4):
            b.accept(member(), b"x" * 3840)
        assert b.frames.qsize() == 2 and b.metrics["dropped_frames"] == 2
        assert b.metrics["malformed_pcm"] == 1
        b.first_packet = b.last_valid = time.monotonic() - 20
        b.last_packet = b.last_valid
        assert not b.unhealthy()  # silence is not a receive failure
        b.last_packet = time.monotonic()
        assert b.unhealthy()
        await b.stop()
        assert b.frames.empty() and not b.active

    run(check())


def test_segmentation_silence_noise_overlap_end_and_max():
    vad = NS(is_speech=lambda pcm, rate: any(pcm))
    s = Segmenter(Limits(max_seconds=2), vad)
    noise, silent = b"\x01\x01" * 1920, bytes(3840)
    for i in range(50):
        assert s.feed(1, silent, i / 50) == (None, 0)
    for i in range(2):
        s.feed(1, noise, i / 50)
    for i in range(45):
        s.feed(1, silent, 0.1 + i / 50)
    assert not s.expire(3)
    for i in range(20):
        s.feed(1, noise, 4 + i / 50)
        s.feed(2, noise, 4 + i / 50)
    results = s.expire(6)
    assert {u.user_id for u in results} == {1, 2}
    assert all(len(u.pcm) == 20 * 640 for u in results)
    results = [s.feed(1, noise, 7 + i / 50)[0] for i in range(100)]
    assert sum(u is not None for u in results) == 1
    assert len(results[-1].pcm) <= 64000


def test_limits_and_sentence_chunks():
    with pytest.raises(ValueError):
        Limits(max_seconds=999)
    text = "This sentence is natural. " * 40
    pieces = chunks(text)
    assert " ".join(pieces) == text.strip()
    assert all(len(p) <= 320 for p in pieces)


def test_fragmented_speech_survives_short_vad_negative_gaps():
    s = Segmenter(Limits(), NS(is_speech=lambda pcm, rate: any(pcm)))
    voice, quiet = b"\x01\x01" * 1920, bytes(3840)
    runs = []
    for i in range(24):
        utterance, run_ms = s.feed(1, voice if i % 8 < 5 else quiet, i / 50)
        assert utterance is None
        runs.append(run_ms)
    utterances = s.expire(2)
    assert len(utterances) == 1
    assert utterances[0].user_id == 1
    assert len(utterances[0].pcm) == 24 * 640
    assert max(runs) == 100  # fragmented speech must not cause natural barge-in
    assert s.metrics["vad_speech_frames"] == 15


def test_brief_noise_still_drops_and_silence_does_not_join_separate_sounds():
    s = Segmenter(Limits(), NS(is_speech=lambda pcm, rate: any(pcm)))
    voice, quiet = b"\x01\x01" * 1920, bytes(3840)
    for base in (0, 2):
        for i in range(5):
            s.feed(1, voice, base + i / 50)
        for i in range(50):
            utterance, _ = s.feed(1, quiet, base + 0.1 + i / 50)
            assert utterance is None
    assert not s.expire(4)
    assert s.metrics["vad_short_segments_dropped"] == 2


@pytest.mark.parametrize(
    "mode,uid,duration,interrupt",
    [
        ("NATURAL", 1, 320, True),
        ("NATURAL", 1, 40, False),
        ("NATURAL", 2, 500, False),
        ("OFF", 1, 500, False),
        ("KEYWORD", 1, 500, False),
    ],
)
def test_natural_barge_in_gates(mode, uid, duration, interrupt):
    async def check():
        s = setup()
        s.interrupt_mode = mode
        s.response_task = asyncio.create_task(asyncio.sleep(30))
        await s.speech(uid, duration, time.monotonic() - 0.4)
        assert s.playback.stop.await_count == int(interrupt)
        assert bool(s.interruption) == interrupt
        await s.stop()

    run(check())


@pytest.mark.parametrize("stage", ["llm", "tts", "playback"])
def test_stale_work_and_partial_memory(stage):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            entered.set()
            await release.wait()
            return (
                "Unheard answer."
                if stage == "llm"
                else b"audio" if stage == "tts" else True
            )

        s = setup()
        if stage == "llm":
            s.respond = delayed
        elif stage == "tts":
            s.synthesize = delayed
        else:
            s.playback.play = delayed
        s.generation, s.current_user = 1, 1
        s.response_task = asyncio.create_task(s.answer(1, "hello", 1, 0))
        await entered.wait()
        await s.interrupt(1, time.monotonic())
        release.set()
        await asyncio.sleep(0.01)
        s.remember.assert_not_awaited()
        assert s.interruption["type"] == "voice_interrupted"
        await s.stop()

    run(check())


def test_completed_chunks_only_memory():
    async def check():
        s = setup(
            respond=AsyncMock(
                return_value=(
                    "A natural sentence with enough detail to form one whole spoken chunk. "
                    * 8
                )
            )
        )
        s.playback.play = AsyncMock(side_effect=[True, False])
        s.generation = 1
        await s.answer(1, "hello", 1, 0)
        assert s.remember.await_count == 1
        assert s.completed == 1 and s.total > 1
        await s.stop()

    run(check())


def test_provider_jobs_bounded_after_repeated_cancellation():
    async def check():
        s = setup()
        release = asyncio.Event()

        async def provider():
            await release.wait()

        for _ in range(10):
            task = asyncio.create_task(s.provider("llm", provider))
            await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert len(s.providers) == 1
        release.set()
        await asyncio.sleep(0.001)
        await s.stop()

    run(check())


def test_relevance_focus_and_optout():
    async def check():
        s = setup()
        assert not s.relevant(1, "hello there")
        assert s.relevant(1, "Scara, hello")
        s.mode, s.focus_until = "CONVERSATION", time.monotonic() + 10
        assert s.relevant(1, "yes") and not s.relevant(2, "yes")
        s.mode = "ACTIVE_ROOM"
        assert s.relevant(1, "hello")
        s.enqueue(Utterance(1, b"x", time.monotonic(), time.monotonic()))
        s.consent(1, False)
        assert s.jobs.empty() and s.focus is None and s.epochs[1] == 1
        await s.stop()

    run(check())


def test_queue_drops_old_and_no_unauthorized_work():
    async def check():
        s = setup()
        for i in range(10):
            s.enqueue(Utterance(1, b"x", i, i))
        s.enqueue(Utterance(2, b"x", 11, 11))
        assert s.jobs.qsize() == 2
        assert s.jobs.get_nowait().ended == 8
        assert s.metrics["dropped_stale_jobs"] == 8
        await s.stop()

    run(check())


@pytest.mark.parametrize(
    "outcome", ["success", "empty", "error", "timeout", "revoked", "stale"]
)
def test_stt_lifecycle(outcome):
    async def check():
        s = setup()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def transcribe(pcm):
            entered.set()
            await release.wait()
            if outcome == "error":
                raise RuntimeError("PRIVATE_PROVIDER_ERROR")
            if outcome == "timeout":
                raise asyncio.TimeoutError()
            return "" if outcome == "empty" else "Scaramouche, hello"

        s.stt.transcribe = transcribe
        task = asyncio.create_task(s.transcribe_loop())
        u = Utterance(
            1,
            b"raw",
            time.monotonic(),
            time.monotonic() - (20 if outcome == "stale" else 0),
        )
        s.enqueue(u)
        if outcome != "stale":
            await entered.wait()
        if outcome == "revoked":
            s.consent(1, False)
        release.set()
        if outcome == "success":
            async def responded():
                while not s.respond.await_count:
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(responded(), 1)
        else:
            await asyncio.sleep(0.02)
        assert s.respond.await_count == int(outcome == "success")
        assert "PRIVATE" not in str(s.metrics)
        # Mirror managed shutdown: close session eligibility before cancelling
        # this test-owned worker, which is not in Session.workers.
        await s.stop()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    run(check())


@pytest.mark.parametrize(
    "transcript,reply,outcome",
    [
        ("PRIVATE background sentence", "Answer.", "discarded_not_addressed"),
        ("", "Answer.", "discarded_empty_transcript"),
        ("Scaramouche, PRIVATE question", "", "empty_response"),
        ("Scaramouche, PRIVATE question", "PRIVATE answer.", "playback_completed"),
    ],
)
def test_voice_decisions_explain_silence_without_transcript_logging(
    transcript, reply, outcome
):
    async def check():
        s = setup()
        s.stt.transcribe.return_value = transcript
        s.respond.return_value = reply
        await s.start()
        s.enqueue(Utterance(1, b"PRIVATE audio", time.monotonic(), time.monotonic()))

        async def observed():
            while not s.metrics[outcome]:
                await asyncio.sleep(0.001)

        try:
            await asyncio.wait_for(observed(), 1)
            report = s.status()
            assert len(report["workers"]) == 2
            assert all(w["running"] and not w["failed"] for w in report["workers"])
            assert any(e["type"] == outcome and e["speaker_id"] == 1 for e in s.events)
            assert "PRIVATE" not in str(report) + str(list(s.events))
            assert s.remember.await_count == int(outcome == "playback_completed")
        finally:
            await s.stop()

    run(check())


def test_followup_after_first_playback_keeps_listening_and_routes_partner():
    async def check():
        s = setup()
        await s.start()
        try:
            for index, phrase in enumerate(
                ("Scaramouche, can you hear me?", "Do you like ice cream?", "What flavor?")
            ):
                s.stt.transcribe.return_value = phrase
                s.enqueue(Utterance(1, b"audio", time.monotonic(), time.monotonic()))

                async def delivered():
                    while s.metrics["playback_completed"] < index + 1:
                        await asyncio.sleep(0.001)

                await asyncio.wait_for(delivered(), 1)
            assert s.respond.await_count == 3
            assert s.remember.await_count == 3
            assert not s.relevant(2, "And you?")
            assert s.relevant(1, "Do you like Wanderer?")
            assert not s.relevant(1, "Wanderer, what do you think?")
            assert s.focus is None
            assert not s.relevant(1, "And you?")
            s.name = "wanderer"
            s.focus, s.focus_until = 1, time.monotonic() + 90
            assert not s.relevant(1, "Hey Scara, can you hear me?")
            s.name = "scaramouche"
            s.focus, s.focus_until = 1, time.monotonic() + 90
            s.mode = "DIRECT_ONLY"
            assert not s.relevant(1, "Do you like ice cream?")
            assert s.relevant(1, "Scaramouche, do you like ice cream?")
        finally:
            await s.stop()

    run(check())


def test_keyword_is_not_blocked_behind_ordinary_followup():
    async def check():
        s = setup()
        s.focus_until = time.monotonic() + 90
        s.stt.transcribe.side_effect = ["Do you like ice cream?", "stop"]
        playing = asyncio.create_task(asyncio.sleep(30))
        s.response_task = playing
        await s.start()
        try:
            s.enqueue(Utterance(1, b"first", time.monotonic(), time.monotonic()))
            async def observed(key, count):
                while s.metrics[key] < count:
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(observed("stt_success", 1), 1)
            s.enqueue(Utterance(1, b"stop", time.monotonic(), time.monotonic()))
            await asyncio.wait_for(observed("cancelled_responses", 1), 1)
            await asyncio.wait_for(observed("playback_completed", 1), 1)
            assert playing.cancelled()
            assert s.respond.await_count == 1
            assert s.respond.call_args.args[1] == "stop"
        finally:
            await s.stop()

    run(check())


@pytest.mark.parametrize("ending", ["complete", "revoke", "stop"])
def test_deferred_followup_preserves_playback_and_user_scope(ending):
    async def check():
        s = setup()
        s.focus_until = time.monotonic() + 90
        s.interrupt_mode = "OFF"
        s.stt.transcribe.side_effect = ["first question", "newest question"]
        release = asyncio.Event()
        playing = asyncio.create_task(release.wait())
        s.response_task = playing
        await s.start()
        try:
            async def pending(text):
                s.enqueue(Utterance(1, text, time.monotonic(), time.monotonic()))
                while s.metrics["deferred_responses"] < s.stt.transcribe.await_count or not s.stt.transcribe.await_count:
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(pending(b"one"), 1)
            s.enqueue(Utterance(1, b"two", time.monotonic(), time.monotonic()))
            async def replaced():
                while s.metrics["deferred_responses"] < 2:
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(replaced(), 1)
            assert not playing.done()
            assert s.metrics["dropped_pending_responses"] == 1
            if ending == "revoke":
                s.consent(1, False)
            elif ending == "stop":
                await s.stop()
            release.set()
            if ending == "complete":
                async def delivered():
                    while not s.metrics["playback_completed"]:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(delivered(), 1)
                assert s.respond.await_count == 1
                assert s.respond.call_args.args[1] == "newest question"
            else:
                await asyncio.sleep(0.02)
                s.respond.assert_not_awaited()
        finally:
            await s.stop()

    run(check())


def test_deferred_followup_expires_without_cancelling_playback():
    async def check():
        s = setup(limits=Limits(stale_seconds=2))
        playing = asyncio.create_task(asyncio.sleep(30))
        s.response_task = playing
        try:
            await asyncio.wait_for(
                s.dispatch_response(1, "private pending", 0, time.monotonic() - 1.98, {}), 1
            )
            assert not playing.done()
            assert s.metrics["discarded_before_response"] == 1
            s.respond.assert_not_awaited()
        finally:
            await s.stop()

    run(check())


def test_keyword_stops_and_next_turn_gets_context():
    async def check():
        s = setup()
        s.stt.transcribe.return_value = "wait, stop"
        s.response_task = asyncio.create_task(asyncio.sleep(30))
        task = asyncio.create_task(s.transcribe_loop())
        s.enqueue(Utterance(1, b"audio", time.monotonic(), time.monotonic()))
        async def responded():
            while not s.respond.await_count:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(responded(), 1)
        assert s.metrics["cancelled_responses"] == 1
        assert "interrupted" in s.respond.call_args.args[2]
        assert s.respond.call_args.args[1] == "wait, stop"
        # Mirror managed shutdown: close session eligibility before cancelling
        # this test-owned worker, which is not in Session.workers.
        await s.stop()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    run(check())


@pytest.mark.parametrize(
    "transcript",
    [
        "Scaramouche, stop.",
        "scaramnouche stop",
        "Scara Mouche, please stop",
        "um, Scaramush, wait",
        "hey Scara",
        "please hold on",
    ],
)
def test_keyword_recognizer_accepts_spoken_stt_variants(transcript):
    assert is_interrupt_keyword(transcript, "scaramouche")


@pytest.mark.parametrize(
    "transcript",
    [
        "I bought a new hat today",
        "Do you like stopping for ice cream?",
        "The conversation is quiet tonight",
    ],
)
def test_keyword_recognizer_rejects_unrelated_conversation(transcript):
    assert not is_interrupt_keyword(transcript, "scaramouche")


def test_keyword_diagnostics_distinguish_detection_from_late_arrival():
    async def check():
        s = setup()
        s.focus, s.focus_until = 1, time.monotonic() + 90
        s.stt.transcribe.return_value = "Scaramnouche, stop"
        task = asyncio.create_task(s.transcribe_loop())
        s.enqueue(Utterance(1, b"audio", time.monotonic(), time.monotonic()))
        await asyncio.sleep(0.02)
        assert s.metrics["interrupt_keyword_detected"] == 1
        assert s.metrics["interrupt_arrived_after_playback"] == 1
        assert s.metrics["interrupt_attempted"] == 0
        # Mirror managed shutdown: close session eligibility before cancelling
        # this test-owned worker, which is not in Session.workers.
        await s.stop()
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    run(check())


def test_failure_notifies_once_and_cleans_up():
    async def check():
        s = setup()
        s.backend.unhealthy = lambda: True
        await s.receive_loop()
        await s.failure("receive_unhealthy")
        assert not s.active
        assert s.notify.await_count == 1
        assert not s.participants
        await s.stop()

    run(check())


def test_playback_stop_does_not_stop_receive():
    async def check():
        vc = NS(stop_playing=Mock(), stop=Mock())
        p = Playback(vc)
        p.source = object()
        p.done = asyncio.get_running_loop().create_future()
        await p.stop()
        vc.stop_playing.assert_called_once()
        vc.stop.assert_not_called()
        assert not await p.done

    run(check())


def test_real_vad_silence_optional():
    pytest.importorskip("webrtcvad")
    s = Segmenter()
    assert s.feed(1, bytes(3840)) == (None, 0)


def test_groq_missing_key_and_bounds():
    async def check():
        with pytest.raises(RuntimeError, match="stt_not_configured"):
            await GroqSTT("").transcribe(b"x")
        assert await GroqSTT("test").transcribe(b"") == ""
        assert await GroqSTT("test").transcribe(b"x" * 640001) == ""

    run(check())


def test_dave_reader_fail_closed_and_rotation(monkeypatch):
    pytest.importorskip("discord.ext.voice_recv")
    from voice_conversation import receive
    from discord.ext.voice_recv import rtp

    monkeypatch.setattr(receive, "check_dependencies", lambda: None)
    b = ReceiveBackend(lambda: {1})
    b.active = True
    stats = NS(successes=0)

    def decrypt(uid, media, payload):
        stats.successes += 1
        return b"opus"

    dave = NS(
        ready=True,
        decrypt=Mock(side_effect=decrypt),
        get_decryption_stats=lambda uid: NS(successes=stats.successes),
    )
    vc = NS(
        client=NS(user=NS(id=9)),
        channel=NS(id=10),
        guild=NS(get_member=lambda uid: member(uid)),
        _get_id_from_ssrc=lambda ssrc: 1,
        _connection=NS(dave_session=dave, dave_protocol_version=1),
        secret_key=bytes(32),
    )
    b.vc = vc
    cls = b.client_class().reader_class
    reader = cls.__new__(cls)
    reader.voice_client, reader.error = vc, None
    reader.decryptor = NS(
        update_secret_key=Mock(), decrypt_rtp=Mock(return_value=b"ciphertext")
    )
    reader.packet_router = NS(feed_rtp=Mock())
    reader.speaking_timer = NS(notify=Mock())
    monkeypatch.setattr(rtp, "is_rtcp", lambda data: False)
    monkeypatch.setattr(rtp, "decode_rtp", lambda data: NS(ssrc=99))
    reader.callback(bytes(20))
    assert b.metrics["dave_frames"] == 1
    vc.secret_key = bytes([1]) * 32
    reader.callback(bytes(20))
    reader.decryptor.update_secret_key.assert_called_with(vc.secret_key)
    vc._connection.dave_protocol_version = 0
    reader.callback(bytes(20))
    assert reader.packet_router.feed_rtp.call_count == 2
    vc._connection.dave_protocol_version = 1
    dave.decrypt.side_effect = (
        lambda *args: b"plaintext"
    )  # passthrough not authenticated
    reader.callback(bytes(20))
    assert reader.packet_router.feed_rtp.call_count == 2
    dave.decrypt.side_effect = ValueError("secret")
    reader.callback(bytes(20))
    assert b.metrics["receive_errors"] == 2
    assert b.metrics["dave_unverified_drops"] == 1
    assert b.metrics["dave_decrypt_errors"] == 1
    reader.decryptor.decrypt_rtp.side_effect = ValueError("PRIVATE_KEY")
    reader.callback(bytes(20))
    assert b.metrics["transport_decrypt_errors"] == 1
    reader.decryptor.decrypt_rtp.side_effect = None
    dave.decrypt.side_effect = decrypt
    reader.packet_router.feed_rtp.side_effect = ValueError("PRIVATE_PACKET")
    reader.callback(bytes(20))
    assert b.metrics["packet_router_errors"] == 1
    monkeypatch.setattr(rtp, "decode_rtp", Mock(side_effect=ValueError("PRIVATE_RTP")))
    reader.callback(bytes(20))
    assert b.metrics["rtp_parse_errors"] == 1
    assert b.metrics["receive_errors"] == 5
    assert "PRIVATE" not in str(b.metrics)


@pytest.mark.parametrize("uid,category", [(None, "unknown_speaker_drops"), (2, "ineligible_speaker_drops")])
def test_receive_reports_pre_decryption_drops_without_private_data(monkeypatch, uid, category):
    pytest.importorskip("discord.ext.voice_recv")
    from voice_conversation import receive
    from discord.ext.voice_recv import rtp

    monkeypatch.setattr(receive, "check_dependencies", lambda: None)
    backend = ReceiveBackend(lambda: {1})
    backend.active = True
    vc = NS(
        client=NS(user=NS(id=9)), channel=NS(id=10),
        guild=NS(get_member=lambda user_id: member(user_id)),
        _get_id_from_ssrc=lambda ssrc: uid,
    )
    backend.vc = vc
    cls = backend.client_class().reader_class
    reader = cls.__new__(cls)
    reader.voice_client, reader.error = vc, None
    reader.decryptor = NS(decrypt_rtp=Mock())
    monkeypatch.setattr(rtp, "is_rtcp", lambda data: False)
    monkeypatch.setattr(rtp, "decode_rtp", lambda data: NS(ssrc=99))
    reader.callback(b"PRIVATE_PACKET_PAYLOAD")
    assert backend.metrics["udp_callbacks"] == 1
    assert backend.metrics["rtp_received"] == 1
    assert backend.metrics[category] == 1
    assert backend.metrics["packets"] == 0
    reader.decryptor.decrypt_rtp.assert_not_called()
    assert "PRIVATE" not in str(backend.metrics)


@pytest.mark.parametrize("fail_handshake", [False, True])
def test_receive_start_registers_silent_speaking_state(fail_handshake):
    pytest.importorskip("discord.ext.voice_recv")
    import discord

    async def check():
        backend = ReceiveBackend(lambda: {1})
        vc = NS(
            listen=Mock(), stop_listening=Mock(),
            is_listening=lambda: True,
            _reader=NS(packet_router=NS(destroy_all_decoders=Mock())),
        )

        async def register(state):
            # The receiver must be attached before Discord can send media.
            vc.listen.assert_called_once()
            assert state == discord.SpeakingState.none
            assert backend.active
            if fail_handshake:
                raise RuntimeError("handshake_failed")

        vc.ws = NS(speak=AsyncMock(side_effect=register))
        if fail_handshake:
            with pytest.raises(RuntimeError, match="handshake_failed"):
                await backend.start(vc)
            assert not backend.active
            vc.stop_listening.assert_called_once()
        else:
            await backend.start(vc)
            assert backend.active
            assert backend.metrics["receive_handshake_sent"] == 1
            vc.stop_listening.assert_not_called()
        vc.ws.speak.assert_awaited_once_with(discord.SpeakingState.none)

    run(check())


def test_receiver_health_distinguishes_mapping_and_listener_state():
    backend = ReceiveBackend(lambda: {1})
    backend.vc = NS(
        is_listening=lambda: True,
        _reader=NS(error=None),
        _ssrc_to_id={99: 1, 100: 2},
        _connection=NS(dave_session=NS(ready=True), secret_key="PRIVATE_KEY"),
    )
    health = backend.health()
    assert health == {
        "reader_listening": True, "reader_failed": False,
        "mapped_speakers": 2, "mapped_participants": 1, "dave_ready": True,
    }
    assert "PRIVATE" not in str(health)
    backend.vc._reader.error = RuntimeError("PRIVATE_FAILURE")
    assert backend.health()["reader_failed"]
    assert "PRIVATE" not in str(backend.health())
