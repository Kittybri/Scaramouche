"""Regression coverage for recovered artwork/UI, registration and scoped persistence."""
import asyncio
import ast
import json
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
import discord
from discord.ext import commands
import pytest
import tarot_system as ts
from tarot_commands import TarotController, TAROT_HELP


def run(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


@pytest.fixture
def deck(tmp_path, monkeypatch):
    from PIL import Image
    art = tmp_path / "art"
    art.mkdir()
    for card in ts.TAROT_DECK:
        Image.new("RGB", (40, 60), "teal").save(art / (card.stem + ".jpg"))
    monkeypatch.setenv("TAROT_DECK_DIR", str(art))
    return art


def controller(tmp_path, name="Scaramouche"):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), application_id=123)
    client = SimpleNamespace(call_with_retry=lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="A new direction."))]))
    c = TarotController(bot, name, client, "fake-model", tmp_path, AsyncMock(return_value=False),
                        lambda text: "password=" in text,
                        store=ts.TarotStore(tmp_path/"tarot.db")).install()
    return c


@pytest.mark.parametrize("name", ["Scaramouche", "Wanderer"])
@run
async def test_registration_keeps_prefix_and_removes_only_requested_slash_commands(tmp_path, name):
    c = controller(tmp_path, name)
    @c.bot.command(name="unrelated")
    async def unrelated(ctx): pass
    assert c.bot.get_command("unrelated")
    assert c.bot.tree.get_command("tarot") is None
    assert c.bot.tree.get_command("dailycard") is None
    for n in ("tarothistory", "tarotsettings"):
        assert c.bot.tree.get_command(n)
    assert c.bot.get_command("scaratarot" if name == "Scaramouche" else "tarot")
    assert c.bot.get_command("scarat" if name == "Scaramouche" else "cards")
    if name == "Scaramouche":
        assert c.bot.get_command("scaradailycard") is c.bot.get_command("scaradaily")
    else:
        assert c.bot.get_command("dailycard")
    c.bot.http.get_global_commands = AsyncMock(return_value=[
        {"name": "tarot", "id": "101", "type": 1},
        {"name": "dailycard", "id": "102", "type": 1},
        {"name": "tarothistory", "id": "103", "type": 1},
        {"name": "unrelated", "id": "104", "type": 1},
        {"name": "tarot", "id": "105", "type": 2},
    ])
    c.bot.http.delete_global_command = AsyncMock()
    c.bot.http.upsert_global_command = AsyncMock()
    c.bot.tree.sync = AsyncMock(side_effect=AssertionError("bulk replacement prohibited"))
    await c.sync_commands()
    await c.sync_commands()
    c.bot.http.get_global_commands.assert_awaited_once_with(123)
    assert c.bot.http.delete_global_command.await_args_list == [
        call(123, 101), call(123, 102)
    ]
    assert c.bot.http.upsert_global_command.await_count == 2
    c.bot.tree.sync.assert_not_awaited()
    await c.bot.close()


@pytest.mark.parametrize("spread,count", [("celtic_cross",10),("three_card",3),("yes_no",5)])
def test_78_cards_unique_draws_and_rendering(deck, spread, count):
    from PIL import Image
    assert len(ts.TAROT_DECK) == len(ts.CARD_BY_STEM) == 78
    draws = ts.draw_spread(spread, rng=random.Random(1))
    assert len(draws) == len({d.card.stem for d in draws}) == count
    assert Image.open(ts.render_spread_image(spread, draws)).width > 0
    assert Image.open(ts.render_single_card(draws[0])).size == (512,768)


def test_celtic_cross_retains_all_positions_without_message_overflow():
    draws = ts.draw_spread("celtic_cross", rng=random.Random(2))
    reading = "\n".join(f"[[{n}]] A complete interpretation." for n in range(1,11))
    pages = ts.celtic_cross_pages("Scaramouche", "x"*500, draws, reading, ts.TarotPreferences())
    assert len(pages) == 3
    assert all(len(p) <= 1950 for p in pages)
    for n in range(1,11):
        assert f"**{n}. " in "\n".join(pages)


@run
async def test_store_restart_user_isolation_and_revoked_views_cannot_restore_data(tmp_path):
    root = ts.TarotStore(tmp_path/"tarot.db")
    a = await root.session(1, "Scaramouche")
    b = await root.session(2, "Wanderer")
    a_other_bot = await root.session(1, "Wanderer")
    draws = ts.draw_spread("three_card")
    await a.save_reading(1, "Scaramouche", "synthetic-A", "three_card", draws, "A")
    await b.save_reading(2, "Wanderer", "synthetic-B", "three_card", draws, "B")
    reopened = ts.TarotStore(root.path)
    assert (await reopened.get_history(1))[0]["question"] == "synthetic-A"
    await reopened.forget(1)
    assert await root.get_history(1) == []
    assert (await root.get_history(2))[0]["question"] == "synthetic-B"
    for stale in (a, a_other_bot):
        with pytest.raises(PermissionError):
            await stale.save_reading(1, "Scaramouche", "resurrected", "three_card", draws, "A")
    with root._connect() as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM tarot_sessions WHERE user_id=1").fetchone()[0] == 0


@run
async def test_daily_is_stable_and_forget_topic_escapes_wildcards(tmp_path):
    root = ts.TarotStore(tmp_path/"tarot.db")
    prefs = ts.TarotPreferences()
    one = await ts.get_daily_card(root, 1, "Scaramouche", prefs)
    two = await ts.get_daily_card(root, 1, "Scaramouche", prefs)
    assert one == two
    draws = ts.draw_spread("three_card")
    for q in ("100% marker", "different"):
        await root.save_reading(1, "Scaramouche", q, "three_card", draws, "ok")
    await root.forget(1, "%")
    assert [r["question"] for r in await root.get_history(1)] == ["different"]


@run
async def test_wrong_user_and_deleted_session_rejected(tmp_path):
    c = controller(tmp_path)
    store = await c.session(1)
    view = ts.TarotView(1, "Scaramouche", AsyncMock(), "", ts.TarotPreferences(), store)
    assert len(view.children) == 3
    intruder = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
    assert not await view.interaction_check(intruder)
    await c.store.forget(1)
    owner = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=AsyncMock()))
    assert not await view.interaction_check(owner)
    await c.bot.close()


@pytest.mark.parametrize("action", ["reading","daily","history","settings"])
@run
async def test_slash_private_delivery_and_prefix_controls(tmp_path, deck, action):
    c = controller(tmp_path)
    interaction = SimpleNamespace(user=SimpleNamespace(id=1),
        response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())
    await c.slash(interaction, action, "Synthetic question")
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    payload = interaction.edit_original_response.call_args.kwargs
    assert payload.get("content")
    assert not payload["allowed_mentions"].everyone
    if action == "reading":
        assert isinstance(payload["view"], ts.TarotView)
        assert payload["view"].preferences.visibility == "private"
    elif action == "daily":
        assert len(payload["attachments"]) == 1
    await c.bot.close()


@run
async def test_private_prefix_dm_failure_does_not_leak_question(tmp_path, deck):
    c = controller(tmp_path)
    await c.store.set_preference(1, "visibility", "private")
    author = SimpleNamespace(id=1, send=AsyncMock(side_effect=discord.Forbidden(SimpleNamespace(status=403,reason="Forbidden"), "blocked")))
    ctx = SimpleNamespace(author=author, guild=object(), send=AsyncMock())
    await c.prefix(ctx, "reading", "private synthetic question")
    assert "private synthetic question" not in str(ctx.send.call_args_list)
    assert "/tarotsettings" in ctx.send.call_args.args[0]
    assert "/dailycard" not in ctx.send.call_args.args[0]
    await c.bot.close()


@pytest.mark.parametrize("question", ["password=fake-secret", "I have chest pain"])
@run
async def test_secret_and_high_stakes_never_reach_provider(tmp_path, question):
    c = controller(tmp_path)
    c.client.call_with_retry = lambda **kwargs: pytest.fail("must not call provider")
    with pytest.raises(ValueError):
        await c.payload(1, "reading", question)
    await c.bot.close()


@run
async def test_modal_followup_guard_and_owner_binding(tmp_path):
    c = controller(tmp_path)
    store = await c.session(1)
    ai = AsyncMock()
    view = ts.TarotResultView(1, "Scaramouche", ai, "question", ts.TarotPreferences(),
                             store, "three_card", ts.draw_spread("three_card"), "reading")
    interaction = SimpleNamespace(user=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
    await view.answer_followup(interaction, "password=fake-secret")
    ai.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    stranger = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
    assert not await view.interaction_check(stranger)
    await c.bot.close()


@run
async def test_deletion_during_generation_prevents_result_and_save(tmp_path):
    c = controller(tmp_path)
    store = await c.session(1)
    def call(**kwargs):
        import sqlite3
        with sqlite3.connect(c.store.path) as db:
            db.execute("DELETE FROM tarot_sessions WHERE user_id=1")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="stale"))])
    c.client.call_with_retry = call
    with pytest.raises(PermissionError):
        await c.generate(store, "synthetic", 100)
    assert not await c.store.get_history(1)
    await c.bot.close()


def test_actual_bot_wiring_help_privacy_and_no_bulk_sync_regression():
    source = Path("bot.py").read_text()
    assert "TAROT = TarotController(" in source
    assert '"tarot": TAROT_STORE.forget' in source
    assert '"tarot_final": TAROT_STORE.forget' in source
    assert "await TAROT_STORE.forget(ctx.author.id, topic)" in source
    tree = ast.parse(source)
    help_fn = next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=="help_cmd")
    assert "TAROT_HELP" in ast.unparse(help_fn)
    assert all("/"+name in TAROT_HELP for name in ["tarothistory", "tarotsettings"])
    assert "!scaradailycard" in TAROT_HELP
    assert "!dailycard" in TAROT_HELP
    assert "`/tarot`" not in TAROT_HELP
    assert "`/dailycard`" not in TAROT_HELP


def test_import_time_construction_does_not_require_running_event_loop(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Async primitives must be created lazily on the running loop")
    monkeypatch.setattr(asyncio, "Lock", forbidden)
    monkeypatch.setattr(asyncio, "Semaphore", forbidden)
    store = ts.TarotStore(tmp_path / "import.db")
    TarotController(None, "Scaramouche", None, "fake-model", tmp_path, None, None, store=store)


@pytest.mark.parametrize("bot_name", ["Scaramouche", "Wanderer"])
@run
async def test_prefix_reading_and_daily_are_normal_revisitable_messages(tmp_path, bot_name):
    c = controller(tmp_path, bot_name)
    channel_message = SimpleNamespace(id=12345)
    channel_send = AsyncMock(return_value=channel_message)
    ctx = SimpleNamespace(
        guild=object(), author=SimpleNamespace(id=1, send=AsyncMock()),
        send=channel_send,
    )
    c.payload = AsyncMock(return_value=({"content": "Public tarot table", "view": SimpleNamespace()}, False))
    await c.prefix(ctx, "reading", "Will I get a cat?")
    assert channel_send.await_args.kwargs["content"] == "Public tarot table"
    assert "ephemeral" not in channel_send.await_args.kwargs
    ctx.author.send.assert_not_awaited()
    assert c.payload.await_args.args[1] == "reading"
    channel_send.reset_mock()
    c.payload = AsyncMock(return_value=({"content": "Daily tarot card", "file": "synthetic"}, False))
    await c.prefix(ctx, "daily")
    assert channel_send.await_args.kwargs["content"] == "Daily tarot card"
    assert "ephemeral" not in channel_send.await_args.kwargs
    ctx.author.send.assert_not_awaited()
    await c.bot.close()


@run
async def test_private_prefix_uses_actual_dm_not_ephemeral_guild_message(tmp_path):
    c = controller(tmp_path, "Scaramouche")
    author = SimpleNamespace(id=1, send=AsyncMock(return_value=SimpleNamespace(id=22)))
    ctx = SimpleNamespace(guild=object(), author=author, send=AsyncMock())
    c.payload = AsyncMock(return_value=({"content": "Private reading"}, True))
    await c.prefix(ctx, "reading", "private question")
    assert author.send.await_args.kwargs["content"] == "Private reading"
    assert "ephemeral" not in author.send.await_args.kwargs
    assert ctx.send.await_count == 1
    assert "privately" in ctx.send.await_args.args[0]
    await c.bot.close()
