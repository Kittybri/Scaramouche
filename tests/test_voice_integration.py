import asyncio
import io
import json
import math
import os
import struct
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest
import discord
from discord.ext import commands

from voice_conversation.integration import VoiceConversation
from voice_conversation.speech import GroqSTT
from voice_conversation.smoke import evaluate


def setup(monkeypatch, allowed_env=None):
    if allowed_env is None:
        monkeypatch.delenv("VOICE_ALLOWED_CHANNEL_IDS", raising=False)
    else:
        monkeypatch.setenv("VOICE_ALLOWED_CHANNEL_IDS", allowed_env)
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    mem = NS(
        get_user_preferences=AsyncMock(return_value={"voice_enabled": True}),
        get_user=AsyncMock(return_value={}),
        upsert_user=AsyncMock(),
        add_message=AsyncMock(),
    )
    service = VoiceConversation(
        bot,
        mem,
        "scaramouche",
        AsyncMock(return_value="Dialogue."),
        AsyncMock(return_value=b"audio"),
        "test",
        1,
    )
    channel = NS(
        id=10,
        send=AsyncMock(),
        permissions_for=lambda m: NS(connect=True, speak=True, view_channel=True),
    )
    author = NS(
        id=1,
        bot=False,
        voice=NS(channel=channel),
        guild_permissions=NS(manage_guild=False),
        send=AsyncMock(),
        display_name="Test",
        mention="<@1>",
    )
    guild = NS(id=5, voice_client=None, me=NS(), get_member=lambda uid: author)
    ctx = NS(author=author, guild=guild, reply=AsyncMock())
    return service, ctx, channel


def test_spoken_farewell_finishes_before_disconnect(monkeypatch):
    async def check():
        service, _ctx, _channel = setup(monkeypatch)
        playback_done = asyncio.Event()
        started = asyncio.Event()

        async def finish_playback():
            started.set()
            await playback_done.wait()

        session = NS(
            active=True,
            participants={1},
            cancel_response=AsyncMock(),
            submit=AsyncMock(return_value=True),
            response_task=None,
        )

        async def submit(uid, text, **kwargs):
            assert uid == 1 and text == "" and kwargs["reply"]
            session.response_task = asyncio.create_task(finish_playback())
            return True

        session.submit.side_effect = submit
        service.sessions[5] = session
        service.leave = AsyncMock()
        task = asyncio.create_task(service.spoken_leave(5, session, 1))
        await asyncio.wait_for(started.wait(), 1)
        service.leave.assert_not_awaited()
        playback_done.set()
        await asyncio.wait_for(task, 1)
        service.leave.assert_awaited_once_with(5, announce=False)
        await service.bot.close()

    asyncio.run(check())


def test_legacy_dispatch_and_join_needs_unambiguous_voice_channel(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        for text in ("hello", "on", "off", None):
            assert not await service.command(ctx, text)
        assert not await service.command(ctx, "status")
        ctx.author.voice.channel = None
        assert await service.command(ctx, "start")
        channel.send.assert_not_awaited()
        assert not service.sessions
        assert "unambiguous voice channel" in ctx.reply.call_args.args[0]
        ctx.author.voice.channel = channel
        monkeypatch.setattr("voice_conversation.integration.shutil.which", lambda _n: "/test/ffmpeg")
        monkeypatch.setattr("voice_conversation.integration.ReceiveBackend.client_class",
                            Mock(side_effect=RuntimeError("backend unavailable")))
        assert await service.command(ctx, "start")
        assert "Live voice could not start" in ctx.reply.call_args.args[0]
        await service.bot.close()

    asyncio.run(check())


def test_voice_listens_automatically_by_default(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        service.bot._connection.user = NS(id=9)
        ctx.author.guild = ctx.guild
        session = NS(channel_id=10)
        service.sessions[5] = session
        service.enroll = AsyncMock(return_value=True)
        service.queue_greeting = Mock()
        await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
        service.enroll.assert_awaited_once_with(session, ctx.author, announce=True)
        service.queue_greeting.assert_called_once_with(session, ctx.author)
        service.sessions.clear()
        await service.bot.close()
    asyncio.run(check())


def test_targeted_start_and_stop_only_reach_named_bot(monkeypatch):
    async def check():
        service, ctx, _channel = setup(monkeypatch)
        service.handle = AsyncMock()

        assert await service.command(ctx, "start wanderer")
        service.handle.assert_not_awaited()

        assert await service.command(ctx, "start scaramouche")
        service.handle.assert_awaited_once_with(ctx, ["start"])

        service.handle.reset_mock()
        assert await service.command(ctx, "stop balladeer")
        service.handle.assert_awaited_once_with(ctx, ["stop"])

        service.handle.reset_mock()
        assert await service.command(ctx, "start")
        service.handle.assert_awaited_once_with(ctx, ["start"])
        await service.bot.close()

    asyncio.run(check())


def test_start_ignores_voice_note_preference_and_sanitizes_backend_failure(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        service.mem.get_user_preferences.return_value = {"voice_enabled": False}
        monkeypatch.setattr(
            "voice_conversation.integration.shutil.which", lambda n: "/test/ffmpeg"
        )
        monkeypatch.setattr(
            "voice_conversation.integration.ReceiveBackend.client_class",
            Mock(side_effect=RuntimeError("secret provider error")),
        )
        await service.command(ctx, "start")
        assert "secret" not in str(ctx.reply.call_args_list)
        service.mem.get_user_preferences.assert_not_awaited()
        channel.send.assert_not_awaited()
        await service.bot.close()

    asyncio.run(check())


def test_start_callbacks_and_automatic_membership_shutdown(monkeypatch):
    async def check():
        # A stale host allowlist must not block an explicit join in this VC.
        service, ctx, channel = setup(monkeypatch, allowed_env="999")
        service.mem.get_user_preferences.return_value = {"voice_enabled": False}
        service.queue_greeting = Mock()
        other = NS(id=2, bot=False, voice=NS(channel=channel), display_name="Other", mention="<@2>")
        channel.members = [ctx.author, other, NS(id=9, bot=True)]
        ctx.guild.get_member = lambda uid: ctx.author if uid == 1 else other
        vc = NS(
            channel=channel,
            disconnect=AsyncMock(),
            listen=Mock(),
            is_listening=lambda: False,
        )
        channel.connect = AsyncMock(return_value=vc)
        monkeypatch.setattr(
            "voice_conversation.integration.shutil.which", lambda n: "/test/ffmpeg"
        )
        monkeypatch.setattr(
            "voice_conversation.integration.ReceiveBackend.client_class",
            lambda s: object,
        )

        async def start(backend, client):
            backend.vc, backend.active = client, True

        monkeypatch.setattr(
            "voice_conversation.integration.ReceiveBackend.start", start
        )
        async def start_session(session):
            session.active = True
        monkeypatch.setattr("voice_conversation.integration.Session.start", start_session)
        monkeypatch.setattr(
            "voice_conversation.session.Segmenter", lambda *a: NS(users={}, metrics={})
        )
        await service.command(ctx, "start")
        channel.send.assert_not_awaited()
        session = service.sessions[5]
        session.active = True
        assert session.participants == {1, 2}
        service.mem.get_user_preferences.assert_not_awaited()
        await session.respond(2, "Scaramouche, hello", "Voice context")
        assert service.respond.call_args.args[0] == 2
        assert service.respond.call_args.args[4] == "Other"
        reply = await session.respond(
            1, "Scaramouche, I can't breathe", "Voice context"
        )
        assert reply == "Dialogue."
        extra = service.respond.call_args.kwargs["extra_context"]
        assert "PROTECTIVE_OVERRIDE" in extra
        await session.synthesize(1, "Dialogue.")
        assert service.tts.call_args.kwargs["voice_key"] == 1
        await session.remember(1, "Spoken part")
        service.mem.add_message.assert_awaited_with(
            1, 10, "assistant", "[voice spoken] Spoken part"
        )
        await service.command(ctx, "mode conversation")
        assert session.mode == "CONVERSATION"
        await service.command(ctx, "interrupt me natural")
        assert session.user_interrupt[1] == "NATURAL"
        assert not await service.command(ctx, "listen on")
        assert session.participants == {1, 2}
        service.queue_greeting.assert_not_called()
        service.install()
        await service.bot.close()
        assert not service.sessions
        vc.disconnect.assert_awaited_once()

    asyncio.run(check())


def test_join_empty_matching_vc_enrolls_humans_only_when_they_arrive(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        service.bot._connection.user = NS(id=9)
        channel.name = "General"
        channel.members = []
        ctx.channel = NS(name="general")
        ctx.guild.voice_channels = [channel, NS(
            name="Other", permissions_for=lambda _m: NS(connect=True, speak=True, view_channel=True)
        )]
        ctx.author.voice.channel = None
        vc = NS(channel=channel, disconnect=AsyncMock(), listen=Mock(), is_listening=lambda: False)
        channel.connect = AsyncMock(return_value=vc)
        monkeypatch.setattr("voice_conversation.integration.shutil.which", lambda _n: "/test/ffmpeg")
        monkeypatch.setattr("voice_conversation.integration.ReceiveBackend.client_class", lambda _s: object)
        async def start_backend(backend, client):
            backend.vc, backend.active = client, True
        monkeypatch.setattr("voice_conversation.integration.ReceiveBackend.start", start_backend)
        async def start_session(session):
            session.active = True
        monkeypatch.setattr("voice_conversation.integration.Session.start", start_session)
        monkeypatch.setattr("voice_conversation.session.Segmenter", lambda *a: NS(users={}, metrics={}))
        service.queue_greeting = Mock()

        await service.command(ctx, "join")
        channel.connect.assert_awaited_once()
        session = service.sessions[5]
        assert session.participants == set()
        ctx.author.guild = ctx.guild
        ctx.author.voice.channel = channel
        await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
        assert session.participants == {1}
        assert session.focus == 1 and session.focus_until > 0
        service.queue_greeting.assert_called_once_with(session, ctx.author)
        await service.bot.close()
    asyncio.run(check())


def test_arrival_can_be_silent_but_still_counts_as_seen(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        ctx.author.guild = ctx.guild
        monkeypatch.setattr("voice_conversation.integration.random.random", lambda: 0.25)
        session = NS(active=True, participants={1})
        service.queue_greeting(session, ctx.author)
        assert service.arrivals_seen[5][1] == 1
        assert not service.greeting_tasks
        await service.bot.close()
    asyncio.run(check())


def test_automatic_arrival_enrollment_greeting_and_leave(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        monkeypatch.setattr("voice_conversation.integration.random.random", lambda: 0.75)
        service.bot._connection.user = NS(id=9)
        ctx.author.guild = ctx.guild
        session = NS(
            active=True, channel_id=10, participants=set(),
            limits=NS(participants=4), backend=NS(vc=NS(channel=channel)),
            busy=lambda: False, segmenter=NS(users={}), jobs=asyncio.Queue(),
            submit=AsyncMock(return_value=True), decision=Mock(),
        )
        def consent(uid, enabled):
            (session.participants.add if enabled else session.participants.discard)(uid)
        session.consent = Mock(side_effect=consent)
        service.sessions[5] = session
        try:
            await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
            await asyncio.gather(*list(service.greeting_tasks.values()))
            assert session.participants == {1}
            channel.send.assert_not_awaited()
            session.submit.assert_awaited_once()
            first_reply = session.submit.call_args.kwargs["reply"]
            assert "Test" in first_reply
            # Mute/deafen changes are not new arrivals.
            await service.voice_state(ctx.author, NS(channel=channel), NS(channel=channel))
            channel.send.assert_not_awaited()
            ctx.author.voice.channel = None
            await service.voice_state(ctx.author, NS(channel=channel), NS(channel=None))
            assert not session.participants
            # A real rejoin gets a distinct return greeting with the name.
            ctx.author.voice.channel = channel
            await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
            await asyncio.gather(*list(service.greeting_tasks.values()))
            assert session.participants == {1}
            assert session.submit.await_count == 2
            return_reply = session.submit.call_args.kwargs["reply"]
            assert "Test" in return_reply and return_reply != first_reply
            assert any(word in return_reply.lower() for word in ("back", "again", "returned"))
            session.participants.clear()
            ctx.author.bot = True
            await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
            assert not session.participants
            ctx.author.bot = False
            service.mem.get_user_preferences.return_value = {"voice_enabled": False}
            await service.voice_state(ctx.author, NS(channel=None), NS(channel=channel))
            await asyncio.gather(*list(service.greeting_tasks.values()))
            assert session.participants == {1}
            channel.send.assert_not_awaited()
            service.mem.get_user_preferences.assert_not_awaited()
        finally:
            service.sessions.clear()
            await service.bot.close()
    asyncio.run(check())


def test_partner_join_is_announced_without_enrollment_or_human_audio_loss(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        service.bot._connection.user = NS(id=9)
        service.partner_id = 8
        partner = NS(id=8, bot=True, guild=ctx.guild, voice=NS(channel=channel))
        session = NS(
            active=True, channel_id=10, initiator=1, participants={1},
            busy=lambda: False, segmenter=NS(users={}), jobs=asyncio.Queue(),
            submit=AsyncMock(return_value=True), consent=Mock(), decision=Mock(),
        )
        service.sessions[5] = session
        await service.voice_state(partner, NS(channel=None), NS(channel=channel))
        await asyncio.gather(*list(service.greeting_tasks.values()))
        session.submit.assert_awaited_once()
        assert "Wanderer" in session.submit.call_args.kwargs["reply"]
        session.consent.assert_not_called()
        partner.voice.channel = None
        await service.voice_state(partner, NS(channel=channel), NS(channel=None))
        session.consent.assert_not_called()
        assert session.participants == {1}
        service.sessions.clear()
        await service.bot.close()

    asyncio.run(check())


def test_auto_enrollment_needs_no_discord_notice_and_leave_fails_closed(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        session = NS(active=True, channel_id=10, participants=set(),
                     limits=NS(participants=4), backend=NS(vc=NS(channel=channel)), consent=Mock())
        channel.send.side_effect = RuntimeError("notice unavailable")
        assert await service.enroll(session, ctx.author, announce=True)
        session.consent.assert_called_once_with(1, True)
        channel.send.assert_not_awaited()
        session.consent.reset_mock()
        ctx.author.voice.channel = None
        assert not await service.enroll(session, ctx.author, announce=True)
        session.consent.assert_not_called()
        ctx.author.voice.channel = channel
        session.active = False
        assert not await service.enroll(session, ctx.author, announce=True)
        session.consent.assert_not_called()
        await service.bot.close()
    asyncio.run(check())


def test_pending_arrival_cannot_speak_after_session_stop(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        monkeypatch.setattr("voice_conversation.integration.random.random", lambda: 0.75)
        ctx.author.guild = ctx.guild
        session = NS(
            active=True, participants={1}, busy=lambda: True,
            segmenter=NS(users={}), jobs=asyncio.Queue(),
            submit=AsyncMock(), decision=Mock(),
            backend=NS(vc=NS(channel=channel, disconnect=AsyncMock())),
            stop=AsyncMock(),
        )
        service.sessions[5] = session
        service.queue_greeting(session, ctx.author)
        await asyncio.sleep(0)
        await service.leave(5)
        session.submit.assert_not_awaited()
        assert not service.greeting_tasks and not service.arrivals_seen
        await service.bot.close()
    asyncio.run(check())


def test_permissions_and_diagnostics(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        session = NS(
            initiator=1,
            active=True,
            channel_id=10,
            participants={1},
            consent=Mock(),
            user_interrupt={},
            status=lambda: {"state": "LISTENING", "metrics": {}},
            events=[],
        )
        service.sessions[5] = session
        ctx.author.id = 2
        ctx.author.voice.channel = None
        await service.command(ctx, "stop")
        assert service.sessions
        await service.command(ctx, "diagnostics")
        ctx.author.send.assert_not_awaited()
        assert not await service.command(ctx, "listen off")
        session.consent.assert_not_called()
        ctx.author.voice.channel = channel
        service.leave = AsyncMock()
        await service.command(ctx, "stop")
        service.leave.assert_awaited_once_with(5)
        ctx.author.id = 1
        await service.command(ctx, "diagnostics")
        file = ctx.author.send.call_args.kwargs["file"]
        assert json.load(file.fp)["state"] == "LISTENING"
        ctx.author.send.reset_mock()
        assert await service.command(ctx, "diagontics")
        ctx.author.send.assert_awaited_once()
        service.sessions.clear()
        await service.bot.close()

    asyncio.run(check())


def test_legacy_voice_off_ends_active_session_instead_of_opt_out(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        ctx.author.id = 2
        service.sessions[5] = NS(channel_id=10, initiator=1)
        service.leave = AsyncMock()
        assert await service.command(ctx, "off")
        service.leave.assert_awaited_once_with(5)
        service.mem.get_user_preferences.assert_not_awaited()
        assert "Live voice ended" in ctx.reply.call_args.args[0]
        service.sessions.clear()
        await service.bot.close()
    asyncio.run(check())


def test_disconnect_and_channel_delete_cleanup(monkeypatch):
    async def check():
        service, ctx, channel = setup(monkeypatch)
        service.bot._connection.user = NS(id=9)
        session = NS(channel_id=10, consent=Mock())
        service.sessions[5] = session
        ctx.author.guild = ctx.guild
        await service.voice_state(ctx.author, NS(channel=channel), NS(channel=None))
        session.consent.assert_called_with(1, False)
        service.leave = AsyncMock()
        await service.channel_deleted(NS(id=10, guild=ctx.guild))
        service.leave.assert_awaited_with(5)
        ctx.author.id = 9
        await service.voice_state(ctx.author, NS(channel=channel), NS(channel=None))
        assert service.leave.await_count == 2
        service.sessions.clear()
        await service.bot.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b'{"text":"hello"}', "hello"),
        (200, b'{"text":""}', ""),
        (429, b"private details", None),
        (200, b"not json", None),
        (200, b"x" * 17000, None),
    ],
)
def test_stt_http_path(monkeypatch, status, body, expected):
    class CM:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            pass

    response = NS(status=status, content=NS(read=AsyncMock(return_value=body)))
    client = NS(post=Mock(return_value=CM(response)))
    monkeypatch.setattr("aiohttp.ClientSession", lambda **kwargs: CM(client))

    async def check():
        if expected is None:
            with pytest.raises((ValueError, RuntimeError)):
                await GroqSTT("test").transcribe(bytes(640))
        else:
            assert await GroqSTT("test").transcribe(bytes(640)) == expected

    asyncio.run(check())
    assert client.post.call_count == 1


def test_smoke_requires_codec_and_human_evidence():
    snapshot = {
        "listening": True,
        "metrics": {
            key: 1
            for key in (
                "packets",
                "dave_frames",
                "pcm_frames",
                "vad_segments",
                "stt_success",
                "spoken_chunks",
                "cancelled_responses",
            )
        },
    }
    snapshot["metrics"]["rms_peak"] = 1000
    assert evaluate(snapshot)
    assert not evaluate(snapshot, True, True)
    del snapshot["metrics"]["dave_frames"]
    assert evaluate(snapshot, True, True)


def test_real_opus_roundtrip_optional():
    library = os.getenv("VOICE_OPUS_LIBRARY")
    if not library:
        pytest.skip("Set VOICE_OPUS_LIBRARY to test real Opus encode/decode")
    discord.opus.load_opus(library)
    pcm = b"".join(
        struct.pack(
            "<hh",
            int(8000 * math.sin(i * 2 * math.pi * 440 / 48000)),
            int(8000 * math.sin(i * 2 * math.pi * 440 / 48000)),
        )
        for i in range(960)
    )
    packet = discord.opus.Encoder().encode(pcm, 960)
    decoded = discord.opus.Decoder().decode(packet)
    assert len(decoded) == 3840
    import audioop

    assert 20 < audioop.rms(decoded, 2) < 32767
