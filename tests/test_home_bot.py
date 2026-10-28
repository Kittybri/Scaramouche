"""Bot-side regressions shared by both character repositories."""

import asyncio
import inspect
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from discord.ext import commands
import pytest

from home.bot_integration import HomeBot, quiet
from home.menu import HomeMenu, HomeSettings, ConfirmAction, ActionModal
from command_help import public_catalog
from home.protocol import command, Rejected


def run(coro):
    return asyncio.run(coro)


def test_additive_home_preference_migration(tmp_path, monkeypatch):
    import memory

    monkeypatch.setattr(memory, "_data_dir", str(tmp_path))
    monkeypatch.setattr(memory, "SHARED_DB_PATH", str(tmp_path / "shared.db"))
    kwargs = (
        {
            "db_path": str(tmp_path / "test.db"),
            "shared_db_path": str(tmp_path / "shared.db"),
        }
        if "db_path" in inspect.signature(memory.Memory).parameters
        else {}
    )
    mem = memory.Memory("test", **kwargs)
    # A real pre-home preferences table: old values must survive repeated migrations.
    with sqlite3.connect(mem.db_path) as db:
        db.execute(
            "CREATE TABLE user_preferences(user_id INTEGER PRIMARY KEY,voice_enabled INTEGER DEFAULT 1,utility_mode INTEGER DEFAULT 1,duo_autoplay INTEGER DEFAULT 1,rp_depth TEXT DEFAULT 'medium')"
        )
        db.execute("INSERT INTO user_preferences VALUES(1,0,1,0,'deep')")

    async def check():
        await mem.init()
        await mem.init()
        prefs = await mem.get_user_preferences(1)
        assert prefs["voice_enabled"] is False and prefs["rp_depth"] == "deep"
        assert (
            not prefs["home_presence_enabled"]
            and not prefs["home_actions_enabled"]
            and not prefs["home_alarms_enabled"]
        )
        assert prefs["voice_output_target"] == ""
        await mem.set_user_preference(1, "voice_output_target", "bedroom")
        await mem.set_user_preference(1, "home_actions_enabled", 1)
        assert (await mem.get_user_preferences(1))["voice_output_target"] == "bedroom"
        assert not (await mem.get_user_preferences(2))["home_actions_enabled"]

    run(check())


def test_commands_register_and_owner_diagnostics_stay_private():
    async def check():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        home = HomeBot("scaramouche", bot, NS(), {}, AsyncMock(), owner_id=1)
        home.install()
        expected = {
            "location",
            "actions",
            "alarms",
            "output",
            "do",
            "confirm",
            "speak",
            "status",
            "devices",
            "agent",
            "permissions",
            "audit",
            "disable",
            "enable",
            "test",
        }
        assert {c.name for c in bot.get_command("home").commands} == expected
        home.client.call = AsyncMock(return_value={"ok": True})
        ctx = NS(author=NS(id=2), guild=None, reply=AsyncMock())
        await bot.get_command("home status").callback(ctx)
        home.client.call.assert_not_awaited()
        ctx.author.id = 1
        ctx.guild = NS(id=9)
        await bot.get_command("home status").callback(ctx)
        home.client.call.assert_not_awaited()
        ctx.guild = None
        for op in (
            "status",
            "devices",
            "agent",
            "permissions",
            "audit",
            "disable",
            "enable",
        ):
            await bot.get_command("home " + op).callback(ctx)
            assert home.client.call.call_args.args == (op, 1)
        await bot.close()

    run(check())


def test_home_is_the_only_public_help_entry_and_subcommands_still_work():
    async def check():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        mem = NS(get_user_preferences=AsyncMock(return_value={}))
        home = HomeBot("scaramouche", bot, mem, {}, AsyncMock(), owner_id=1)
        home.install()
        labels = [field.name for page in public_catalog(bot) for field in page.fields]
        assert "!home" in labels
        assert all(not label.startswith("!home ") for label in labels)
        for name in ("actions", "alarms", "confirm", "do", "location", "output", "speak"):
            assert bot.get_command("home " + name) is not None
        author = NS(id=2, send=AsyncMock())
        ctx = NS(author=author, guild=NS(id=9), reply=AsyncMock())
        await bot.get_command("home").callback(ctx)
        assert author.send.await_count == 1
        panel = author.send.call_args.kwargs
        assert isinstance(panel["view"], HomeMenu)
        assert panel["view"].user_id == 2 and panel["view"].guild_id == 0
        assert ctx.reply.call_args.args[0] == "Home controls sent privately."
        await bot.close()

    run(check())


def test_home_panel_is_user_bound_and_confirmation_remains_explicit():
    async def check():
        mem = NS(get_user_preferences=AsyncMock(return_value={}))
        home = HomeBot("scaramouche", NS(), mem, {}, AsyncMock())
        home.client.call = AsyncMock(return_value={"ok": True, "result": "completed"})
        wrong_response = NS(send_message=AsyncMock())
        menu = HomeMenu(home, 1, 9)
        assert not await menu.interaction_check(NS(user=NS(id=2), response=wrong_response))
        assert "someone else" in wrong_response.send_message.call_args.args[0]
        home.client.call.assert_not_awaited()
        settings = HomeSettings(home, 1, 9)
        delete_button = next(item for item in settings.children if item.label == "Delete location data")
        response = NS(edit_message=AsyncMock(), defer=AsyncMock())
        await delete_button.callback(NS(user=NS(id=1), response=response))
        assert response.edit_message.call_args.kwargs["view"].__class__.__name__ == "ConfirmLocationDelete"
        home.client.call.assert_not_awaited()
        confirm = ConfirmAction(home, 1, 9, "a" * 32)
        confirm_button = next(item for item in confirm.children if item.label == "Confirm exact action")
        await confirm_button.callback(NS(user=NS(id=1), response=response, edit_original_response=AsyncMock()))
        assert home.client.call.call_args.args == ("confirm", 1)
        assert home.client.call.call_args.kwargs["guild_id"] == 9
        assert home.client.call.call_args.kwargs["request_id"] == "a" * 32
        modal = ActionModal(home, 1, 9)
        home.action = AsyncMock(return_value={"confirmation_required": "b" * 32})
        await modal.on_submit(NS(user=NS(id=2), response=wrong_response))
        home.action.assert_not_awaited()
        modal.device._value = "lamp"
        modal.action._value = "power"
        modal.parameters._value = '{"on": true}'
        ok_response = NS(send_message=AsyncMock(), defer=AsyncMock())
        ok_interaction = NS(user=NS(id=1), response=ok_response, edit_original_response=AsyncMock())
        await modal.on_submit(ok_interaction)
        assert home.action.call_args.args == (1, 9, "lamp", "power", {"on": True})
        assert ok_response.defer.await_count == 1
        assert isinstance(ok_interaction.edit_original_response.call_args.kwargs["view"], ConfirmAction)
        assert "lamp" in ok_interaction.edit_original_response.call_args.kwargs["content"]

    run(check())


def test_current_tts_is_reused_and_quiet_or_sensitive_audio_is_blocked():
    async def check():
        tts = AsyncMock(return_value=b"ID3" + b"0" * 40)
        home = HomeBot("wanderer", NS(), NS(), {"enabled": True}, tts)
        home.client.call = AsyncMock(
            side_effect=[
                {"ok": True, "asset": "audio"},
                {"ok": True, "result": "completed"},
            ]
        )
        user = {"quiet_hours_start": 0, "quiet_hours_end": 0, "mood": 3}
        result = await home.speak(1, "speaker", "Take a break.", user)
        assert result["ok"]
        assert tts.call_args.args == ("Take a break.", 3)
        assert tts.call_args.kwargs["user"] is user
        assert home.client.call.call_args.kwargs["command"]["action"] == "play"
        tts.reset_mock()
        assert not (await home.speak(1, "speaker", "password: private", user))["ok"]
        assert not (
            await home.speak(1, "speaker", "hello", dict(user, voice_enabled=False))
        )["ok"]
        tts.assert_not_awaited()
        home.client.config["enabled"] = False
        assert not (await home.speak(1, "speaker", "hello", user))["ok"]
        await home.tick()

    run(check())


def test_protocol_cannot_accept_raw_device_commands():
    for action, p in (
        ("exec", {}),
        ("print_note", {"text": "private conversation"}),
        ("play", {"url": "https://bad", "volume": 1}),
    ):
        with pytest.raises(Rejected):
            command("d", action, p, 1, 0, "wanderer")


def test_memory_failure_does_not_hide_completed_action():
    async def check():
        home = HomeBot(
            "scaramouche",
            NS(),
            NS(),
            {},
            AsyncMock(),
            self_store=NS(record_event=AsyncMock(side_effect=RuntimeError())),
        )
        home.client.call = AsyncMock(return_value={"ok": True, "result": "completed"})
        assert (await home.action(1, 0, "lamp", "power", {"on": True}))[
            "result"
        ] == "completed"
        assert home.client.call.await_count == 1

    run(check())


def test_scheduler_quiet_gate_does_not_generate_or_send(monkeypatch):
    async def check():
        user = {
            "home_actions_enabled": True,
            "home_presence_enabled": True,
            "proactive": True,
            "allow_dms": True,
            "mood": -9,
        }
        mem = NS(
            get_user=AsyncMock(return_value=user), consume_shared_cooldown=AsyncMock()
        )
        home = HomeBot(
            "wanderer",
            NS(fetch_user=AsyncMock()),
            mem,
            {
                "enabled": True,
                "users": {
                    "1": {
                        "rules": [
                            {
                                "signal": "irritated",
                                "device": "lamp",
                                "action": "power",
                                "parameters": {"on": False},
                            }
                        ]
                    }
                },
            },
            AsyncMock(),
        )
        home.client.call = AsyncMock(
            return_value={
                "ok": True,
                "events": [{"kind": "location.entered", "zone": "HOME"}],
            }
        )
        monkeypatch.setattr("home.bot_integration.quiet", lambda _: True)
        await home.tick()
        home.bot.fetch_user.assert_not_awaited()
        mem.consume_shared_cooldown.assert_not_awaited()
        assert home.client.call.await_count == 1

    run(check())
