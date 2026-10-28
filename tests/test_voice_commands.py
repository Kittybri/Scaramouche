"""Voice command restoration uses the existing guarded session dispatcher."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import discord
import pytest
from discord.ext import commands
from voice_commands import VoiceCommands, VOICE_HELP
from voice_conversation.integration import VoiceConversation


def setup(name="scaramouche"):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), application_id=123)
    service = VoiceConversation(bot, NS(), name, AsyncMock(), AsyncMock(), "fake", 1)
    controller = VoiceCommands(service).install()
    return bot, service, controller


@pytest.mark.parametrize("name", ["scaramouche", "wanderer"])
@pytest.mark.parametrize("command,action", [("join", "join"), ("listen", "join"), ("leave", "leave")])
def test_prefix_routes_to_existing_dispatcher(name, command, action):
    async def run():
        bot, service, controller = setup(name)
        service.handle = AsyncMock()
        ctx = NS(guild=NS(id=5))
        await bot.get_command(command).callback(ctx, target=name)
        service.handle.assert_awaited_once_with(ctx, [action])
        service.handle.reset_mock()
        other = "wanderer" if name == "scaramouche" else "scaramouche"
        await bot.get_command(command).callback(ctx, target=other)
        service.handle.assert_not_awaited()
        await bot.close()
    asyncio.run(run())


def test_bad_target_and_dm_cannot_start():
    async def run():
        bot, service, controller = setup()
        service.handle = AsyncMock()
        ctx = NS(guild=None, reply=AsyncMock())
        await bot.get_command("join").callback(ctx, target="typo")
        await bot.get_command("join").callback(ctx)
        service.handle.assert_not_awaited()
        assert "server voice channel" in ctx.reply.call_args.args[0]
        await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["join", "leave"])
def test_slash_defers_and_reuses_original_user_and_guards(action):
    async def run():
        bot, service, controller = setup()
        # Real handle rejects users outside the allowed VC and unauthorized stops.
        interaction = NS(user=NS(id=2, guild_permissions=NS(manage_guild=False), voice=None),
            guild=NS(id=5), channel=NS(id=8),
            response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
        command = bot.tree.get_command(action)
        assert command.guild_only
        await command.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        result = interaction.followup.send.call_args
        assert result.kwargs["ephemeral"] is True
        assert not result.kwargs["allowed_mentions"].everyone
        assert "mention_author" not in result.kwargs
        assert ("Join a VC" if action == "join" else "session initiator") in result.args[0]
        assert not service.sessions
        await bot.close()
    asyncio.run(run())


def test_registration_upserts_only_owned_names_once():
    async def run():
        bot, service, controller = setup()
        bot.http.upsert_global_command = AsyncMock()
        bot.tree.sync = AsyncMock(side_effect=AssertionError("No bulk overwrite"))
        await controller.ready()
        await controller.ready()
        assert [call.args[1]["name"] for call in bot.http.upsert_global_command.call_args_list] == ["join", "leave"]
        bot.tree.sync.assert_not_awaited()
        assert bot.get_command("listen") is bot.get_command("join")
        assert all(name in VOICE_HELP for name in ("!join", "!listen", "!leave", "/join", "/leave", "!voice start"))
        await bot.close()
    asyncio.run(run())


def test_wanderer_uses_existing_complete_tree_sync_without_racing_it():
    async def run():
        bot, service, controller = setup("wanderer")
        bot.http.upsert_global_command = AsyncMock()
        await controller.ready()
        bot.http.upsert_global_command.assert_not_awaited()
        assert bot.tree.get_command("join") and bot.tree.get_command("leave")
        await bot.close()
    asyncio.run(run())



@pytest.mark.parametrize("name,utterance", [
    ("scaramouche", "Hey Scaramouche, voice voice channel"),
    ("scaramouche", "Scara join voice cahat"),
    ("wanderer", "Wanderer, can you join the voice channel?"),
])
def test_natural_request_joins_only_named_bot(name, utterance, monkeypatch):
    from voice_commands import join_intent
    assert join_intent(utterance, name)
    other = "wanderer" if name == "scaramouche" else "scaramouche"
    assert not join_intent(utterance, other)

    async def run():
        bot, service, controller = setup(name)
        channel = NS(id=10)
        service.allowed = frozenset({10})
        author = NS(id=2, bot=False, voice=NS(channel=channel))
        guild = NS(id=5)
        message = NS(content=utterance, author=author, guild=guild, attachments=[], reply=AsyncMock())
        ctx = NS(author=author, guild=guild, reply=AsyncMock())
        async def start(_ctx, action):
            assert action == "join"
            service.sessions[5] = NS(active=True)
        service.command = AsyncMock(side_effect=start)
        assert await controller.maybe_join(message, ctx)
        service.command.assert_awaited_once_with(ctx, "join")
        assert name == "scaramouche" or "Alright" in message.reply.call_args.args[0]
        assert message.reply.await_count == 1
        assert not message.reply.call_args.kwargs["allowed_mentions"].everyone
        await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("utterance", [
    "Scara, how do I join voice chat?",
    "Scaramouche, don't join voice chat.",
    "I want to join Scaramouche in voice.",
    "Wanderer, join voice chat.",
    "Scaramouche and Wanderer, join voice chat.",
    "Scara, what is a voice channel?",
    "Scara, does voice chat work?",
    "Scara, do not join voice chat.",
    "Scara, don’t join voice chat.",
    "!join scaramouche",
])
def test_conversational_route_rejects_non_commands_and_partner(utterance):
    from voice_commands import join_intent
    assert not join_intent(utterance, "scaramouche")


def test_voice_note_uses_short_transcript_and_existing_guard(monkeypatch):
    async def run():
        bot, service, controller = setup("wanderer")
        service.mem.get_user_preferences = AsyncMock(return_value={"voice_enabled": True})
        service.allowed = frozenset({10})
        audio = NS(is_voice_message=lambda: True, duration=3, size=4000, read=AsyncMock(return_value=b"OggSfake"))
        author = NS(id=2, bot=False, voice=NS(channel=NS(id=10)))
        message = NS(content="", author=author, guild=NS(id=5), attachments=[audio], reply=AsyncMock())
        ctx = NS(author=author, guild=message.guild, reply=AsyncMock())
        controller._transcribe_note = AsyncMock(return_value="Wanderer, join voice chat")
        service.command = AsyncMock(side_effect=lambda _ctx, _action: service.sessions.update({5: NS(active=True)}))
        assert await controller.maybe_join(message, ctx)
        controller._transcribe_note.assert_awaited_once_with(audio)
        service.command.assert_awaited_once_with(ctx, "join")
        assert "Alright" in message.reply.call_args.args[0]

        service.command.reset_mock()
        service.sessions.clear()
        message.author.voice.channel.id = 99
        assert not await controller.maybe_join(message, ctx)
        service.command.assert_not_awaited()
        assert controller._transcribe_note.await_count == 1
        await bot.close()
    asyncio.run(run())


def test_voice_note_rejects_long_or_large_audio_before_download():
    async def run():
        bot, service, controller = setup()
        for duration, size in [(31, 1000), (3, 2_000_001)]:
            audio = NS(is_voice_message=lambda: True, duration=duration, size=size, read=AsyncMock())
            assert not await controller._transcribe_note(audio)
            audio.read.assert_not_awaited()
        await bot.close()
    asyncio.run(run())


def test_voice_note_opted_out_is_not_transcribed():
    async def run():
        bot, service, controller = setup("wanderer")
        service.allowed = frozenset({10})
        service.mem.get_user_preferences = AsyncMock(return_value={"voice_enabled": False})
        author = NS(id=2, bot=False, voice=NS(channel=NS(id=10)))
        audio = NS(is_voice_message=lambda: True)
        message = NS(content="", author=author, guild=NS(id=5), attachments=[audio])
        controller._transcribe_note = AsyncMock()
        assert not await controller.maybe_join(message, NS())
        controller._transcribe_note.assert_not_awaited()
        await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("as_note", [False, True])
def test_other_bot_join_request_is_consumed_without_reply(as_note):
    async def run():
        bot, service, controller = setup("scaramouche")
        service.allowed = frozenset({10})
        service.mem.get_user_preferences = AsyncMock(return_value={"voice_enabled": True})
        author = NS(id=2, bot=False, voice=NS(channel=NS(id=10)))
        audio = NS(is_voice_message=lambda: True)
        message = NS(content="" if as_note else "Wanderer, join voice chat", author=author,
                     guild=NS(id=5), attachments=[audio] if as_note else [], reply=AsyncMock())
        controller._transcribe_note = AsyncMock(return_value="Wanderer, join voice chat")
        service.command = AsyncMock()
        assert await controller.maybe_join(message, NS())
        service.command.assert_not_awaited()
        message.reply.assert_not_awaited()
        assert controller._transcribe_note.await_count == int(as_note)
        await bot.close()
    asyncio.run(run())


def test_voice_note_transcription_uses_bounded_groq_request(monkeypatch):
    async def run():
        import voice_commands
        bot, service, controller = setup()
        service.stt_key = "test-key-not-real"
        audio = NS(is_voice_message=lambda: True, duration=3, size=12, read=AsyncMock(return_value=b"OggSfake"))
        response = NS(status=200, content=NS(read=AsyncMock(return_value=b'{"text":"Scara join voice chat"}')))
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)
        class ResponseContext:
            async def __aenter__(self): return response
            async def __aexit__(self, *_args): return None
        class ClientContext:
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            def post(self, url, *, headers, data):
                assert url.endswith("/audio/transcriptions")
                assert headers["Authorization"] == "Bearer test-key-not-real"
                assert data.is_multipart
                return ResponseContext()
        monkeypatch.setattr(voice_commands.aiohttp, "ClientSession", lambda **_kwargs: ClientContext())
        assert await controller._transcribe_note(audio) == "Scara join voice chat"
        audio.read.assert_awaited_once_with()
        response.content.read.assert_awaited_once_with(8193)
        await bot.close()
    asyncio.run(run())


@pytest.mark.parametrize("transcript", [
    "Wanderer, join voice chat. password: hunter22",
    "Wanderer, join voice chat. I want to hurt myself",
])
def test_voice_note_sensitive_transcript_never_joins(transcript):
    async def run():
        bot, service, controller = setup("wanderer")
        service.mem.get_user_preferences = AsyncMock(return_value={"voice_enabled": True})
        service.allowed = frozenset({10})
        author = NS(id=2, bot=False, voice=NS(channel=NS(id=10)))
        audio = NS(is_voice_message=lambda: True)
        message = NS(content="", author=author, guild=NS(id=5), attachments=[audio], reply=AsyncMock())
        controller._transcribe_note = AsyncMock(return_value=transcript)
        service.command = AsyncMock()
        assert not await controller.maybe_join(message, NS())
        service.command.assert_not_awaited()
        await bot.close()
    asyncio.run(run())


def test_natural_join_failure_does_not_claim_presence():
    async def run():
        bot, service, controller = setup("scaramouche")
        service.allowed = frozenset({10})
        author = NS(id=2, bot=False, voice=NS(channel=NS(id=99)), guild_permissions=NS(manage_guild=False))
        message = NS(content="Scara join voice chat", author=author, guild=NS(id=5), attachments=[], reply=AsyncMock())
        ctx = NS(author=author, guild=message.guild, reply=AsyncMock())
        assert await controller.maybe_join(message, ctx)
        assert "Join a VC" in ctx.reply.call_args.args[0]
        message.reply.assert_not_awaited()
        assert not service.sessions
        await bot.close()
    asyncio.run(run())

