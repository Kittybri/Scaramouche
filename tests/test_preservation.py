"""Runtime and negative preservation tests; no network calls or real stores."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import discord
from discord import app_commands
from discord.ext import commands
import pytest
from preservation_contract import inventory, validate, removal_errors, load, CLASSES
from restored_status import ProviderStatus
from command_help import public_catalog
from help_delivery import send_help

def run(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper

def test_actual_runtime_registration_help_features_and_module_loading():
    result = subprocess.run([sys.executable, "tools/validate_preservation.py"],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr

@run
async def test_preservation_negative_cases():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    @bot.command(name="card", aliases=["cards"])
    async def card(ctx): pass
    @bot.group(name="game")
    async def game(ctx): pass
    @game.command(name="start")
    async def start(ctx): pass
    @bot.tree.command(name="card")
    async def slash(interaction: discord.Interaction): pass
    module = SimpleNamespace(bot=bot, engine=SimpleNamespace(run=lambda: None),
                             PRIVACY_DELETION=SimpleNamespace(stages={"cards": lambda uid: None}))
    manifest = inventory(bot)
    manifest["help_entries"] = ["!card", "/card"]
    features = {"features":{"cards":{"status":"PRESENT","prefix":["card"],"wiring":["engine.run"],
                                    "callable":["engine.run"],"privacy_stages":["cards"]}}}
    assert not validate(module, "!card /card", manifest, features)
    changed = copy.deepcopy(manifest)
    changed["listeners"] = {"on_ready": ["missing.callback"]}
    assert "missing listener: on_ready" in validate(module, "!card /card", changed, features)
    saved = bot.remove_command("cards")
    assert any("alias" in e for e in validate(module, "!card /card", manifest, features))
    bot.all_commands["cards"] = saved
    saved = bot.remove_command("card")
    assert any("missing prefix" in e for e in validate(module, "!card /card", manifest, features))
    bot.add_command(saved)
    saved = bot.remove_command("game")
    assert any("game" in e for e in validate(module, "!card /card", manifest, features))
    bot.add_command(saved)
    saved = bot.tree.remove_command("card")
    assert any("missing slash" in e for e in validate(module, "!card /card", manifest, features))
    bot.tree.add_command(saved)
    assert any("missing help" in e for e in validate(module, "!cardinal /card", manifest, features))
    module.engine.run = None
    assert any("unwired" in e for e in validate(module, "!card /card", manifest, features))
    module.engine.run = lambda: None
    del module.PRIVACY_DELETION.stages["cards"]
    assert any("privacy stage" in e for e in validate(module, "!card /card", manifest, features))
    module.PRIVACY_DELETION.stages["cards"] = lambda uid: None
    changed = copy.deepcopy(manifest)
    changed["prefix"]["card"]["module"] = "missing.module"
    assert any("module" in e for e in validate(module, "!card /card", changed, features))
    changed = copy.deepcopy(manifest)
    changed["cogs"] = ["UnloadedCog"]
    assert "missing cogs" in validate(module, "!card /card", changed, features)
    changed = copy.deepcopy(manifest)
    changed["extensions"] = ["missing.extension"]
    assert "missing extensions" in validate(module, "!card /card", changed, features)
    bot.get_command("card").enabled = False
    assert any("disabled" in e for e in validate(module, "!card /card", manifest, features))
    await bot.close()

def test_intentional_removal_requires_explicit_complete_record():
    before = {"prefix":{"card":{"aliases":["cards"]}}, "slash":{"card":{}}, "help_entries":["!card"],
              "features":{"tarot":{"status":"PRESENT"}}}
    after = {"prefix":{}, "slash":{}, "help_entries":[], "features":{}}
    missing = removal_errors(before, after, [])
    assert set(missing) == {"prefix:card","alias:card:cards","slash:card","help:!card","feature:tarot"}
    assert removal_errors(before, after, [{"removed_surfaces":missing}]) == missing
    record = {k:"explicit test authorization/evidence" for k in
              ("authorization","reason","replacement","privacy_impact","validation")}
    record["removed_surfaces"] = missing
    assert not removal_errors(before, after, [record])

def test_historical_audit_and_expected_surface_are_complete():
    audit = load("historical_audit.json")
    contract = load("commands.json")
    legacy = json.loads(Path("tests/legacy_command_surface.json").read_text())
    prefix = {r["old"] for r in audit["records"] if r["surface"] in {"prefix","alias"}}
    assert set(legacy["prefix"]) <= prefix
    assert set(legacy["slash"]) <= {r["old"] for r in audit["records"] if r["surface"] == "slash"}
    assert len(audit["dynamic_modules"]) == len(audit["cog_modules"])
    assert all(r["classification"] in CLASSES and r["evidence"] for r in audit["records"])
    unresolved = {(r["surface"],r["old"]) for r in audit["records"]
                  if r["classification"] in {"ACCIDENTALLY_MISSING","UNSAFE_TO_RESTORE_AS_IS"}}
    assert unresolved == {(r["surface"],r["name"]) for r in contract["unresolved_surfaces"]}
    assert set(contract["owner_commands"]).isdisjoint(contract["public_commands"])
    assert set(contract["prefix"]) == set(contract["owner_commands"] + contract["public_commands"] + contract["permission_scoped_commands"])
    # No silent erasure of the historical source hashes or intentional rename.
    assert len(audit["sources"]) >= 12
    assert next(r for r in audit["records"] if r["old"] == "nsfw")["classification"] == "RENAMED"

def test_resolving_a_missing_alias_requires_an_actual_registered_alias():
    before = {"unresolved_surfaces": [{"surface":"alias","name":"quest1"}]}
    assert removal_errors(before, {}, []) == ["unresolved:alias:quest1"]
    after = {"prefix":{"rpg1":{"aliases":["quest1"]}}}
    assert removal_errors(before, after, []) == []
    after["prefix"]["rpg1"]["aliases"] = []
    assert removal_errors(before, after, []) == ["unresolved:alias:quest1"]

@pytest.mark.parametrize("name", ["Scaramouche", "Wanderer"])
@run
async def test_status_is_read_only_distinct_and_private(name):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    client = SimpleNamespace(_clients=[object()], is_exhausted=lambda:False)
    c = ProviderStatus(bot,name,client,1).install()
    ctx = SimpleNamespace(author=SimpleNamespace(id=2,send=AsyncMock()),reply=AsyncMock())
    await bot.get_command("status").callback(ctx)
    text = ctx.reply.call_args.args[0]
    assert ("Obviously" in text) == (name == "Scaramouche")
    assert "configured" not in text
    assert bot.get_command("aistatus") is bot.get_command("status")
    ctx.author.id = 1
    await c.command(ctx)
    assert "no live API probe" in ctx.author.send.call_args.args[0]
    assert ctx.reply.await_count == 1
    client.is_exhausted = lambda:True
    assert await c.state() == "cooldown"
    client._clients = []
    assert await c.state() == "not_configured"
    await bot.close()

@run
async def test_scara_restored_aliases_keep_current_permission_checks():
    if load("commands.json")["bot"] != "scaramouche":
        # These Scaramouche-only aliases are not added to Wanderer.
        return
    import ast
    tree = ast.parse(Path("bot.py").read_text())
    selected = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr == "command"
                        and any(k.arg == "name" and isinstance(k.value, ast.Constant)
                                and k.value.value in {"mute", "unmute", "taskhealth"}
                                for k in d.keywords)
                        for d in n.decorator_list)]
    assert len(selected) == 3
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    memory = SimpleNamespace(mute_user=AsyncMock(), unmute_user=AsyncMock())
    reply = AsyncMock()
    namespace = {"bot":bot,"commands":commands,"discord":discord,"mem":memory,
                 "safe_reply":reply,"_owner_only":lambda ctx:False,
                 "qai":AsyncMock(),"log_error":lambda *args:pytest.fail("Unexpected callback failure")}
    exec(compile(ast.Module(body=selected,type_ignores=[]),"bot.py","exec"),namespace)
    ctx = SimpleNamespace(author=SimpleNamespace(id=1,guild_permissions=SimpleNamespace(manage_messages=False)))
    target = SimpleNamespace(id=2)
    for alias in ("botban","banfrombot"):
        assert bot.get_command(alias) is bot.get_command("mute")
        await bot.get_command(alias).callback(ctx,target,10)
    for alias in ("botunban","unbanfrombot"):
        assert bot.get_command(alias) is bot.get_command("unmute")
        await bot.get_command(alias).callback(ctx,target)
    await bot.get_command("bothealth").callback(ctx)
    memory.mute_user.assert_not_awaited()
    memory.unmute_user.assert_not_awaited()
    namespace["qai"].assert_not_awaited()
    assert reply.await_count == 5
    await bot.close()

@run
async def test_owner_dm_failure_never_leaks_status():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    c = ProviderStatus(bot,"Scaramouche",SimpleNamespace(_clients=[]),1)
    error = discord.Forbidden(SimpleNamespace(status=403,reason="Forbidden"),"closed")
    ctx = SimpleNamespace(author=SimpleNamespace(id=1,send=AsyncMock(side_effect=error)),reply=AsyncMock())
    await c.command(ctx)
    assert "not_configured" not in ctx.reply.call_args.args[0]
    await bot.close()

@run
async def test_help_catalog_is_paginated_and_does_not_expose_owner_commands():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    async def callback(ctx): pass
    @app_commands.command(name="sample", description="Registered slash command")
    async def sample(interaction: discord.Interaction): pass
    bot.tree.add_command(sample)
    for i in range(45):
        bot.add_command(commands.Command(callback,name=f"public{i}",aliases=[f"p{i}"]))
    bot.add_command(commands.Command(callback,name="build"))
    pages = public_catalog(bot)
    ctx = SimpleNamespace(send=AsyncMock())
    await send_help(ctx,pages)
    embeds = [e for c in ctx.send.call_args_list for e in c.kwargs.get("embeds",[])]
    all_text = json.dumps([e.to_dict() for e in embeds])
    assert "!build" not in all_text and "!public44" in all_text and "!p44" in all_text
    assert bot.tree.get_command("sample") is sample
    assert not any(e.title == "Slash command index" for e in embeds)
    assert "/sample" not in all_text
    assert all(len(e.fields) <= 25 and len(e) <= 6000 for e in embeds)
    assert all(sum(len(e) for e in c.kwargs["embeds"]) <= 6000 for c in ctx.send.call_args_list)
    await bot.close()
