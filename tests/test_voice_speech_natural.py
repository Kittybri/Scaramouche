"""More natural speech in the voice channel (owner's request, November 2026; both bots share this file).

No pause between the pieces of a reply, a short opening so he starts sooner, interruption reactions made ahead of
time, mood shown with Fish S2 cues instead of rewritten punctuation, every clip at one loudness, and a reply length
drawn by chance: even odds at a neutral mood, curter when he is irritated with the person, longer when pleased.
"""

import asyncio
import queue
import re
import time
from collections import Counter
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import ormsgpack
import pytest

import voice_conversation.session as session_module
import voice_handler
from voice_conversation.session import FREE_LENGTH, SHORT_LENGTH, Session, State, short_reply_odds
from voice_conversation.speech import Limits, chunks, spoken_pieces

BOT = "wanderer" if hasattr(session_module, "names_wander_alias_in_opening") else "scaramouche"
VAD = NS(is_speech=lambda pcm, rate: any(pcm))
STORY = (
    "Hmph. You again? What could you possibly want from me this time, fool? I suppose I can spare a moment. "
    "Speak, before I change my mind about tolerating your presence here at all. I will not repeat myself, so "
    "listen closely to every word. Then go, and do not waste my time again."
)


def player(play):
    return NS(play=play, stop=AsyncMock(), vc=NS(is_playing=lambda: False), source=None)


def make_session(**kwargs):
    backend = NS(frames=queue.Queue(128), metrics=Counter(), unhealthy=lambda: False, stop=AsyncMock(), active=True)
    defaults = dict(
        channel_id=10, initiator=1, name=BOT, backend=backend,
        stt=NS(transcribe=AsyncMock(return_value="hello")), playback=player(AsyncMock(return_value=True)),
        respond=AsyncMock(return_value="A reply."), synthesize=AsyncMock(return_value=b"audio"),
        remember=AsyncMock(), notify=AsyncMock(), limits=Limits(), vad=VAD,
    )
    defaults.update(kwargs)
    s = Session(**defaults)
    s.participants = {1}
    return s


# ── 1/2. a short opening, and no pause between pieces ────────────────────────
def test_the_opening_is_voiced_on_its_own():
    pieces = spoken_pieces(STORY)
    assert pieces[0] == "Hmph. You again? What could you possibly want from me this time, fool?"
    assert " ".join(pieces) == STORY                     # nothing lost or reordered
    assert len(chunks(STORY)[0]) >= 100 > len(pieces[0])  # he used to wait for a first chunk of 100+ characters
    assert all(len(piece) >= 100 for piece in pieces[1:-1])
    assert spoken_pieces("Fine.") == ["Fine."] and spoken_pieces("") == []
    assert spoken_pieces("Hmph. As if.") == ["Hmph. As if."]  # a short reply stays one piece
    one_long_sentence = "word " * 100
    assert spoken_pieces(one_long_sentence) == chunks(one_long_sentence)


def test_the_next_piece_is_made_while_this_one_plays():
    async def check():
        events = []

        async def synthesize(uid, text):
            events.append(("made", text))
            await asyncio.sleep(0.15)
            return text.encode()

        async def play(audio):
            events.append(("start", time.monotonic()))
            await asyncio.sleep(0.25)
            events.append(("end", time.monotonic()))
            return True

        s = make_session(synthesize=synthesize, playback=player(play), respond=AsyncMock(return_value=STORY))
        s.active = True
        await asyncio.wait_for(s.answer(1, "Tell me a story.", s.generation, s.epochs[1]), 10)
        pieces = spoken_pieces(STORY)
        assert [text for kind, text in events if kind == "made"] == pieces
        assert events[0] == ("made", pieces[0])            # the short opening is made first
        starts = [t for kind, t in events if kind == "start"]
        ends = [t for kind, t in events if kind == "end"]
        assert len(starts) == len(pieces) >= 3
        gaps = [start - end for end, start in zip(ends, starts[1:])]
        assert max(gaps) < 0.08  # he used to fall silent for the whole synthesis (0.15 s here) between pieces
        assert s.completed == len(pieces)

    asyncio.run(check())


def test_a_reply_cut_short_drops_the_piece_being_made():
    async def check():
        made, played = [], []

        async def synthesize(uid, text):
            made.append(text)
            await asyncio.sleep(0.1)
            return b"x"

        async def play(audio):
            played.append(audio)
            s.generation += 1  # interrupted while the first piece plays
            await asyncio.sleep(0.05)
            return True

        s = make_session(synthesize=synthesize, playback=player(play), respond=AsyncMock(return_value=STORY))
        s.active = True
        await asyncio.wait_for(s.answer(1, "Tell me a story.", s.generation, s.epochs[1]), 5)
        await asyncio.sleep(0.15)
        assert len(played) == 1 and len(made) <= 2   # nothing after the cut is voiced or made
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    asyncio.run(check())


# ── 3. the interruption reaction is ready before he is cut off ───────────────
def _react(s):
    if hasattr(s, "_speak_interruption_acknowledgement"):
        line = session_module._INTERRUPTION_LINES[0]
        return s._speak_interruption_acknowledgement(1, s.generation, s.epochs[1], time.monotonic(), line)
    return s.speak_interruption_ack(1, s.generation, s.epochs[1], time.monotonic())


def test_reactions_are_made_ahead_and_played_at_once(monkeypatch):
    async def check():
        monkeypatch.setattr(session_module, "_REACTION_WARMUP_S", 0)
        synthesize = AsyncMock(side_effect=lambda uid, text: b"made:" + text.encode())
        s = make_session(synthesize=synthesize)
        s.active, s.state = True, State.LISTENING
        await asyncio.wait_for(s.prepare_reactions(), 2)
        assert set(s.reaction_audio) == set(session_module._INTERRUPTION_LINES)
        synthesize.reset_mock()
        played = []
        s.playback = player(AsyncMock(side_effect=lambda audio: played.append(audio) or True))
        assert await _react(s)
        assert played and played[0].startswith(b"made:") and synthesize.await_count == 0

    asyncio.run(check())


def test_reactions_wait_until_nothing_else_is_being_voiced(monkeypatch):
    async def check():
        monkeypatch.setattr(session_module, "_REACTION_WARMUP_S", 0)
        synthesize = AsyncMock(return_value=b"audio")
        s = make_session(synthesize=synthesize)
        s.active, s.state = True, State.LISTENING
        reply = asyncio.get_running_loop().create_future()
        s.response_task = reply                               # he is answering someone
        warming = asyncio.ensure_future(s.prepare_reactions())
        await asyncio.sleep(0.7)
        assert synthesize.await_count == 0
        reply.set_result(None)
        await asyncio.wait_for(warming, 3)
        assert synthesize.await_count == len(session_module._INTERRUPTION_LINES)

    asyncio.run(check())


def test_starting_a_session_prepares_the_reactions(monkeypatch):
    async def check():
        monkeypatch.setattr(session_module, "_REACTION_WARMUP_S", 0)
        s = make_session(synthesize=AsyncMock(return_value=b"audio"))
        await s.start()
        try:
            for _ in range(60):
                if len(s.reaction_audio) == len(session_module._INTERRUPTION_LINES):
                    break
                await asyncio.sleep(0.05)
            assert len(s.reaction_audio) == len(session_module._INTERRUPTION_LINES)
        finally:
            await s.stop()

    asyncio.run(check())


def test_without_a_prepared_clip_the_reaction_is_made_on_the_spot():
    async def check():
        synthesize = AsyncMock(return_value=b"fresh")
        s = make_session(synthesize=synthesize)
        s.active = True
        played = []
        s.playback = player(AsyncMock(side_effect=lambda audio: played.append(audio) or True))
        assert await _react(s)
        assert played == [b"fresh"] and synthesize.await_count == 1

    asyncio.run(check())


# ── 4. mood as a Fish cue, not rewritten words ───────────────────────────────
def test_mood_is_a_cue_now_not_rewritten_words(monkeypatch):
    monkeypatch.delenv("VOICE_EMOTION_TAGS", raising=False)
    text = "You and I both know it, fool. But never again."
    for style in ("cold", "cutting", "heated"):
        styled = voice_handler._style_tts_text(text, style)
        assert styled.count("[") == 2, styled  # one cue per sentence: an S2 cue lasts to the end of its sentence
        assert re.sub(r"\[[^\]]*\]\s*", "", styled) == text  # every word kept ("and", "but", the commas)
    assert voice_handler._style_tts_text(text, "guarded") == text
    # only he adds cues: brackets in the reply itself (from the model or a user) are removed
    assert voice_handler._style_tts_text("[whispers] Come closer. [laughs]", "guarded") == "Come closer."
    monkeypatch.setenv("VOICE_EMOTION_TAGS", "off")
    assert voice_handler._style_tts_text(text, "cold") == text
    assert voice_handler._delivery("heated") == ("", 1.0)


def test_a_long_voice_note_keeps_every_word(monkeypatch):
    monkeypatch.delenv("VOICE_EMOTION_TAGS", raising=False)
    text = " ".join(f"Sentence number {i} is here." for i in range(48))  # ~1300 characters, 48 sentences
    assert len(text) <= 1500
    # with a cue per sentence it would pass Fish's 1500 characters and lose its end: it is voiced plainly instead
    assert voice_handler._style_tts_text(text, "cold") == text
    assert voice_handler._style_tts_text("Leave. Now.", "cold").count("[") == 2


def test_the_mood_reaches_fish_as_a_cue_and_a_speed(monkeypatch):
    monkeypatch.delenv("VOICE_EMOTION_TAGS", raising=False)
    calls = []
    monkeypatch.setattr(voice_handler, "_fish_tts_blocking",
                        lambda text, key, chunk, **kw: calls.append((text, kw)) or b"mp3")
    assert asyncio.run(voice_handler.get_audio_mooded("Leave. Now.", "key", -8, "cutting")) == b"mp3"
    assert calls[0][0].startswith("[") and calls[0][1]["speed"] != 1.0
    assert asyncio.run(voice_handler.get_audio_mooded("Hello.", "key", 0, "guarded")) == b"mp3"
    assert calls[1] == ("Hello.", {**calls[1][1], "speed": 1.0})


class _Client:
    made, sent, fail = [], [], []

    def __init__(self, **kwargs):
        _Client.made.append(kwargs)

    def post(self, url, content=None, headers=None):
        if _Client.fail:
            raise _Client.fail.pop(0)
        _Client.sent.append(ormsgpack.unpackb(content))
        return NS(status_code=200, content=b"mp3", text="")

    def close(self):
        pass


@pytest.fixture
def fish(monkeypatch):
    _Client.made, _Client.sent, _Client.fail = [], [], []
    monkeypatch.setattr(voice_handler.httpx, "Client", _Client)
    monkeypatch.setattr(voice_handler._local, "client", None, raising=False)
    return _Client


def test_fish_requests_share_one_kept_connection_and_carry_the_speed(fish):
    assert voice_handler._fish_tts_blocking("One.", "key", 220, speed=1.05) == b"mp3"
    assert voice_handler._fish_tts_blocking("Two.", "key", 220) == b"mp3"
    assert len(fish.made) == 1  # one connection (and TLS handshake), not one per piece
    assert fish.sent[0]["prosody"] == {"speed": 1.05, "volume": 0} and "prosody" not in fish.sent[1]


def test_a_dropped_kept_connection_is_retried_once_but_a_timeout_is_not(fish):
    httpx = voice_handler.httpx
    fish.fail = [httpx.RemoteProtocolError("Server disconnected without sending a response.")]
    assert voice_handler._fish_tts_blocking("Hi there.", "key") == b"mp3"
    assert len(fish.made) == 2 and len(fish.sent) == 1
    fish.fail = [httpx.ReadTimeout("slow")]
    assert voice_handler._fish_tts_blocking("Hi again.", "key") is None
    assert len(fish.sent) == 1  # not sent twice


# ── 5. every clip at one loudness ────────────────────────────────────────────
def test_every_clip_is_levelled_to_one_loudness(monkeypatch):
    filters = " ... loudnorm          A->A       EBU R128 loudness normalization\n"
    monkeypatch.setattr(session_module.subprocess, "run", lambda *a, **k: NS(stdout=filters))
    monkeypatch.delenv("VOICE_LOUDNESS_LUFS", raising=False)
    try:
        session_module.level_options.cache_clear()
        assert session_module.level_options() == "-af loudnorm=I=-18:TP=-2:LRA=11"
        session_module.level_options.cache_clear()
        monkeypatch.setenv("VOICE_LOUDNESS_LUFS", "-16")
        assert session_module.level_options() == "-af loudnorm=I=-16:TP=-2:LRA=11"
        session_module.level_options.cache_clear()
        monkeypatch.setenv("VOICE_LOUDNESS_LUFS", "off")
        assert session_module.level_options() == ""
        session_module.level_options.cache_clear()
        monkeypatch.delenv("VOICE_LOUDNESS_LUFS")
        monkeypatch.setattr(session_module.subprocess, "run", lambda *a, **k: NS(stdout="no such filter"))
        assert session_module.level_options() == ""  # an ffmpeg without loudnorm plays clips as before
    finally:
        session_module.level_options.cache_clear()


def test_playback_hands_the_levelling_to_ffmpeg(monkeypatch):
    import discord

    captured = {}

    class Source:
        def __init__(self, source, **kwargs):
            captured.update(kwargs)

        def cleanup(self):
            pass

    class VC:
        def is_playing(self):
            return False

        def play(self, source, after):
            after(None)

        def stop(self):
            pass

    monkeypatch.setattr(session_module, "level_options", lambda: "-af loudnorm=I=-18:TP=-2:LRA=11")
    monkeypatch.setattr(discord, "FFmpegPCMAudio", Source)

    async def check():
        assert await session_module.Playback(VC()).play(b"mp3") is True

    asyncio.run(check())
    assert captured == {"pipe": True, "options": "-af loudnorm=I=-18:TP=-2:LRA=11"}


# ── 6. reply length: chance, weighted by his mood ────────────────────────────
def test_the_odds_of_a_short_reply_follow_his_mood():
    assert short_reply_odds(0) == 0.5                      # a coin toss at a neutral mood
    assert short_reply_odds(-10) == pytest.approx(0.8)     # curt when irritated with you
    assert short_reply_odds(10) == pytest.approx(0.2)      # talkative when pleased
    assert short_reply_odds(-5) == pytest.approx(0.65) and short_reply_odds(-50) == pytest.approx(0.8)
    assert short_reply_odds(None) == short_reply_odds("odd") == 0.5


def _contexts(monkeypatch, mood, draws, **answer_kwargs):
    contexts = []

    async def respond(uid, text, context):
        contexts.append(context)
        return "Fine."

    values = iter(draws)
    monkeypatch.setattr(session_module, "random", NS(random=lambda: next(values), choice=lambda seq: seq[0]))
    mood_of = mood if callable(mood) else AsyncMock(return_value=mood)

    async def check():
        s = make_session(respond=respond, mood_of=mood_of)
        s.active = True
        for _ in range(max(1, len(draws))):
            await s.answer(1, "hello", s.generation, s.epochs[1], **answer_kwargs)
        return s

    session = asyncio.run(check())
    return contexts, session


def test_each_reply_draws_its_length(monkeypatch):
    contexts, s = _contexts(monkeypatch, 0, [0.4, 0.6])
    assert SHORT_LENGTH in contexts[0] and FREE_LENGTH not in contexts[0]
    assert FREE_LENGTH in contexts[1] and SHORT_LENGTH not in contexts[1]
    assert s.metrics["short_replies"] == s.metrics["free_length_replies"] == 1
    assert SHORT_LENGTH in _contexts(monkeypatch, -10, [0.7])[0][0]  # odds 0.8 when irritated
    assert FREE_LENGTH in _contexts(monkeypatch, 10, [0.3])[0][0]    # odds 0.2 when pleased


def test_games_duets_and_scripted_lines_keep_their_length(monkeypatch):
    mood_of = AsyncMock(return_value=-10)
    contexts, _ = _contexts(monkeypatch, mood_of, [], feature_context="STRUCTURED VOICE DUO: argue.")
    assert FREE_LENGTH in contexts[0] and SHORT_LENGTH not in contexts[0]
    mood_of.assert_not_awaited()


def test_an_unknown_mood_counts_as_neutral(monkeypatch):
    contexts, _ = _contexts(monkeypatch, AsyncMock(side_effect=RuntimeError("db")), [0.45])
    assert SHORT_LENGTH in contexts[0]  # 0.45 < 0.5


def test_without_a_mood_source_replies_keep_the_old_free_length():
    async def check():
        contexts = []
        s = make_session(respond=AsyncMock(side_effect=lambda uid, text, context: contexts.append(context) or "Ok."))
        s.active = True
        await s.answer(1, "hello", s.generation, s.epochs[1])
        return contexts

    assert FREE_LENGTH in asyncio.run(check())[0]


def test_joining_gives_the_session_his_mood_toward_each_person(monkeypatch):
    from test_voice_integration import setup
    from voice_conversation.integration import ReceiveBackend

    async def check():
        service, ctx, channel = setup(monkeypatch)
        service.mem.get_user = AsyncMock(return_value={"mood": -7})
        channel.members = [ctx.author]
        channel.connect = AsyncMock(return_value=NS(channel=channel, disconnect=AsyncMock(), listen=lambda *a: None,
                                                    is_listening=lambda: False))
        monkeypatch.setattr("voice_conversation.integration.shutil.which", lambda n: "/test/ffmpeg")
        monkeypatch.setattr(ReceiveBackend, "client_class", lambda self: object)

        async def start_backend(backend, client):
            backend.vc, backend.active = client, True

        async def start_session(session):
            session.active = True

        monkeypatch.setattr(ReceiveBackend, "start", start_backend)
        monkeypatch.setattr(Session, "start", start_session)
        monkeypatch.setattr(session_module, "Segmenter", lambda *a: NS(users={}, metrics={}))
        try:
            await service.command(ctx, "start")
            assert await service.sessions[5].mood_of(1) == -7
        finally:
            service.sessions.clear()
            await service.bot.close()

    asyncio.run(check())
