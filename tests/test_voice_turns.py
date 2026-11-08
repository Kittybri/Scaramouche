"""Turn-taking fixes from the owner's live transcript (November 2026; both bots share this file).

Whisper "heard" noise as Russian and Icelandic, he answered every "Okay." with a speech, his memory label
"[voice spoken]" crept into what he said, only a few exact words could stop him, and his goodbye could be cut off.
"""

import asyncio
import queue
import time
from collections import Counter
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

import voice_conversation.session as session_module
from voice_conversation.session import Session, State, is_backchannel, is_interrupt_keyword, talks_over
from voice_conversation.speech import GroqSTT, Limits, Utterance

BOT = "wanderer" if hasattr(session_module, "names_wander_alias_in_opening") else "scaramouche"
VAD = NS(is_speech=lambda pcm, rate: any(pcm))


def make_session(**kwargs):
    backend = NS(frames=queue.Queue(128), metrics=Counter(), unhealthy=lambda: False, stop=AsyncMock(), active=True)
    defaults = dict(
        channel_id=10, initiator=1, name=BOT, backend=backend,
        stt=NS(transcribe=AsyncMock(return_value="hello")),
        playback=NS(play=AsyncMock(return_value=True), stop=AsyncMock(), vc=NS(is_playing=lambda: True), source=None),
        respond=AsyncMock(return_value="A reply."), synthesize=AsyncMock(return_value=b"audio"),
        remember=AsyncMock(), notify=AsyncMock(), limits=Limits(), vad=VAD,
    )
    defaults.update(kwargs)
    s = Session(**defaults)
    s.participants = {1, 2}
    return s


def test_acknowledgements_and_real_words():
    for text in ("Okay.", "Yes.", "Yeah, yeah.", "Mm-hmm.", "Uh huh.", "Right."):
        assert is_backchannel(text) and not talks_over(text), text
    for text in ("Wait, what about Nahida?", "Actually, tell me about Inazuma.", "Okay but why?", "No."):
        assert talks_over(text) and not is_backchannel(text), text
    assert not talks_over("") and not is_backchannel("")


def test_more_ways_to_say_stop():
    for text in ("Skip.", "Shh.", "Shush, please.", "Be quiet.", "Scaramouche, be quiet!", "Quiet!", "Stop.", "Shut up."):
        assert is_interrupt_keyword(text, BOT), text
    assert not is_interrupt_keyword("The conversation is quiet tonight", BOT)
    assert not is_interrupt_keyword("I like it when the room is quiet.", BOT)


def _hear(s, text, uid=1):
    async def run():
        s.stt = NS(transcribe=AsyncMock(return_value=text))
        worker = asyncio.create_task(s.transcribe_loop())
        now = time.monotonic()
        s.enqueue(Utterance(uid, b"pcm", now - 0.5, now))
        await asyncio.sleep(0.2)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    return run()


def _speaking(s, *, state=State.BOT_SPEAKING, completed=1, total=3):
    s.active = True
    s.focus, s.focus_until = 1, time.monotonic() + 90
    s.response_task = asyncio.ensure_future(asyncio.sleep(10))
    s.state, s.completed, s.total = state, completed, total


@pytest.mark.parametrize("text,stops", [
    ("Actually, tell me about Inazuma instead.", True),   # real words over him stop him
    ("Okay.", False),                                    # a listener's acknowledgement does not
    ("Mm-hmm.", False),
])
def test_talking_over_him_stops_him(text, stops):
    async def check():
        s = make_session()
        _speaking(s)
        await _hear(s, text)
        assert (s.metrics["interrupt_attempted"] == 1) is stops
        assert ("interrupt_talked_over" in [e["type"] for e in s.events]) is stops

    asyncio.run(check())


def test_only_while_he_speaks_and_only_the_person_he_answers():
    async def check():
        s = make_session()
        _speaking(s, state=State.THINKING, completed=0, total=0)   # still thinking: a follow-up, not a cut-in
        await _hear(s, "And make it about the Shogun.")
        assert s.metrics["interrupt_attempted"] == 0
        s = make_session()
        _speaking(s)
        await _hear(s, "Anyway, what were we doing?", uid=2)            # someone else in the room
        assert s.metrics["interrupt_attempted"] == 0

    asyncio.run(check())


def test_a_bare_okay_gets_no_speech_unless_he_asked():
    async def check():
        s = make_session()
        s.active = True
        s.focus, s.focus_until = 1, time.monotonic() + 90
        s.last_spoken = "I suppose you think that is clever."
        await _hear(s, "Okay.")
        await asyncio.sleep(0.1)
        assert "discarded_backchannel" in [e["type"] for e in s.events]
        s.respond.assert_not_awaited()
        s.last_spoken = "Do you want to hear it or not?"
        await _hear(s, "Yes.")
        await asyncio.sleep(0.3)
        assert s.respond.await_count == 1       # answering his question is a turn

    asyncio.run(check())


def test_after_he_asks_something_a_yes_is_answered():
    async def check():
        s = make_session(respond=AsyncMock(return_value="Do you want to hear it or not?"))
        s.active = True
        s.focus, s.focus_until = 1, time.monotonic() + 90
        await s.answer(1, "I have a question.", s.generation, s.epochs[1])   # he asks, in his own words
        assert s.last_spoken == "Do you want to hear it or not?"
        await _hear(s, "Yes.")
        await asyncio.sleep(0.3)
        assert s.respond.await_count == 2

    asyncio.run(check())


def test_his_memory_label_is_never_spoken():
    async def check():
        said = []
        s = make_session(synthesize=AsyncMock(side_effect=lambda uid, text: said.append(text) or b"a"),
                         respond=AsyncMock(return_value="[voice spoken] [voice spoken], you have some nerve. Fine."))
        s.active = True
        await s.answer(1, "hello", s.generation, s.epochs[1])
        assert said and all("voice spoken" not in text for text in said)
        assert " ".join(said) == "you have some nerve. Fine."

    asyncio.run(check())


def test_nothing_cuts_off_the_goodbye():
    async def check():
        s = make_session()
        _speaking(s)
        s.leave_requested = True
        await s.interrupt(1, time.monotonic())
        assert not s.response_task.done() and s.metrics["cancelled_responses"] == 0
        s.response_task.cancel()

    asyncio.run(check())


def test_the_goodbye_finishes_before_he_disconnects(monkeypatch):
    from voice_conversation.integration import VoiceConversation

    order = []

    async def check():
        service = VoiceConversation(NS(add_listener=Mock(), close=AsyncMock()), NS(), BOT, AsyncMock(), AsyncMock(), "k")
        played = asyncio.get_running_loop().create_future()

        async def submit(uid, text, reply=None):
            async def say():
                await asyncio.sleep(0.2)
                order.append("said goodbye")
            session.response_task = asyncio.ensure_future(say())
            return True

        session = NS(active=True, participants={1}, cancel_response=AsyncMock(), submit=submit, response_task=None)
        service.sessions[5] = session
        service.leave = AsyncMock(side_effect=lambda *a, **k: order.append(("left", time.monotonic())))
        started = time.monotonic()
        await service.spoken_leave(5, session, 1)
        played.cancel()
        assert order[0] == "said goodbye" and order[1][1] - started >= 0.65  # 0.2 s line + 0.5 s pause

    asyncio.run(check())


def _whisper_fields(monkeypatch, env=None):
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
    if env is None:
        monkeypatch.delenv("VOICE_STT_LANGUAGE", raising=False)
    else:
        monkeypatch.setenv("VOICE_STT_LANGUAGE", env)
    asyncio.run(GroqSTT("key").transcribe(bytes(640)))
    return fields


def test_whisper_is_told_the_language(monkeypatch):
    assert _whisper_fields(monkeypatch)["language"] == "en"        # noise was "heard" as Russian and Icelandic
    assert _whisper_fields(monkeypatch, "ja")["language"] == "ja"
    assert "language" not in _whisper_fields(monkeypatch, "auto")


def test_dropped_packets_are_counted_by_kind(monkeypatch):
    pytest.importorskip("discord.ext.voice_recv")
    from discord.ext.voice_recv import rtp
    from voice_conversation import receive
    from voice_conversation.receive import ReceiveBackend

    monkeypatch.setattr(receive, "check_dependencies", lambda: None)
    b = ReceiveBackend(lambda: {1})
    b.active = True
    stats = NS(successes=0, passthroughs=0)
    outcome = {}

    def decrypt(uid, media, payload):
        kind = outcome["kind"]
        if kind == "voice":
            stats.passthroughs += 1
            return b"opus-voice-frame"
        return b"\xf8\xff\xfe"           # Discord's Opus silence frame

    dave = NS(ready=True, decrypt=decrypt,
              get_decryption_stats=lambda uid: NS(successes=stats.successes, passthroughs=stats.passthroughs))
    vc = NS(client=NS(user=NS(id=9)), channel=NS(id=10),
            guild=NS(get_member=lambda uid: NS(id=uid, bot=False, voice=NS(channel=NS(id=10)))),
            _get_id_from_ssrc=lambda ssrc: 1, _connection=NS(dave_session=dave, dave_protocol_version=1),
            secret_key=bytes(32))
    b.vc = vc
    monkeypatch.setattr(b, "allows", lambda member: True, raising=False)
    cls = b.client_class().reader_class
    reader = cls.__new__(cls)
    reader.voice_client, reader.error = vc, None
    reader.decryptor = NS(update_secret_key=Mock(), decrypt_rtp=Mock(return_value=b"ciphertext"))
    reader.packet_router = NS(feed_rtp=Mock())
    reader.speaking_timer = NS(notify=Mock())
    monkeypatch.setattr(rtp, "is_rtcp", lambda data: False)
    monkeypatch.setattr(rtp, "decode_rtp", lambda data: NS(ssrc=99))
    for kind in ("silence", "silence", "voice"):
        outcome["kind"] = kind
        reader.callback(bytes(20))
    assert b.metrics["dave_unverified_drops"] == 3            # still dropped: unverified audio is not trusted
    assert b.metrics["dave_unverified_silence"] == 2 and b.metrics["dave_unverified_passthrough_voice"] == 1
    reader.packet_router.feed_rtp.assert_not_called()
