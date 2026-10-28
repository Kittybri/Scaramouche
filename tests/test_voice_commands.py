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

