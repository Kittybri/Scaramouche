"""Slash-first discovery preserves the legacy handlers and user-scoped settings."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from command_help import public_catalog
from persistent_world import PersistentWorld


def test_world_preferences_are_user_scoped_and_validate_values(tmp_path):
    async def check():
        values = {}

        async def get(uid):
            return {"grudge_enabled": bool(values.get((uid, "grudge_enabled"), True)),
                    "lullaby_enabled": bool(values.get((uid, "lullaby_enabled"), False)),
                    "lullaby_start_hour": values.get((uid, "lullaby_start_hour"), 23)}

        async def set_pref(uid, key, value):
            values[(uid, key)] = value

        mem = SimpleNamespace(shared_db_path=str(tmp_path / "shared.db"),
                              get_user_preferences=get, set_user_preference=set_pref)
        world = PersistentWorld("scaramouche", mem, {})
        assert "Lullaby: True" in await world.preferences(1, "lullaby", "on")
        assert "Lullaby: False" in await world.preferences(2)
        assert "Start hour: 20" in await world.preferences(1, "lullaby_hour", "20")
        assert "Use /world prefs" in await world.preferences(1, "lullaby_hour", "19")
        assert values[(1, "lullaby_start_hour")] == 20

    asyncio.run(check())


def test_legacy_commands_remain_callable_but_leave_public_help():
    async def check():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        for name in ("weatherlocation", "world", "worldadd", "worldprefs"):
            async def handler(ctx):
                pass
            bot.command(name=name)(handler)
        assert all(bot.get_command(name) for name in
                   ("weatherlocation", "world", "worldadd", "worldprefs"))
        labels = [field.name for page in public_catalog(bot) for field in page.fields]
        assert not any(label in labels for label in
                       ("!weatherlocation", "!world", "!worldadd", "!worldprefs"))
        await bot.close()

    asyncio.run(check())


def test_weather_location_shared_handler_status_set_and_clear():
    source = ast.parse(Path("bot.py").read_text())
    node = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name == "weather_location_text")
    locations = {}

    async def get_user(uid):
        return {"weather_location": locations.get(uid, "")}

    async def set_location(uid, value):
        locations[uid] = value

    namespace = {"mem": SimpleNamespace(get_user=get_user,
                                          set_weather_location=set_location),
                 "_resolve_weather_location": AsyncMock(return_value=True)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "bot.py", "exec"), namespace)
    handler = namespace["weather_location_text"]

    async def check():
        assert "not configured" in await handler(1)
        assert "notice" in await handler(1, "San Jose, CA")
        assert locations[1] == "San Jose, CA"
        assert "not configured" in await handler(2)
        assert "disabled" in await handler(1, "off")
        assert locations[1] == ""

    asyncio.run(check())
