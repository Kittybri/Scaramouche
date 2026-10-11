"""Presentation-only illustrated RPG: identity, unchanged text, graceful fallbacks."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from PIL import Image
import discord
from rpg_visuals import (BOSS_RANKS, portrait_filename, portrait_path,
                         render_scene, render_war_table, illustrated_send)


def test_all_eleven_portraits_have_distinct_slots_with_bot_six_identity():
    assert len(BOSS_RANKS) == len(set(BOSS_RANKS)) == 11
    for rank in BOSS_RANKS:
        boss={"rank":rank,"name":"Original story name"}
        if rank!=6:
            assert portrait_filename(boss,"Scaramouche")==portrait_filename(boss,"Wanderer")
        else:
            assert portrait_filename(boss,"Scaramouche")=="06-scaramouche.jpg"
            assert portrait_filename(boss,"Wanderer")=="06-wanderer.jpg"


@pytest.mark.parametrize("world",["horror","takeover"])
@pytest.mark.parametrize("kind",["world","encounter","dice","victory","defeat"])
def test_visuals_are_local_images_and_do_not_change_story(world,kind,tmp_path):
    boss={"rank":6,"name":"The Balladeer's Shadow","pts":20,"theme":"Original theme retained"}
    artifact=render_scene(kind=kind,world=world,boss=boss,bot_name="Scaramouche",
                          round_num=10,points=14,total=77,defeated=(11,10),
                          origin="teyvat",element="electro",roll=13,art_dir=tmp_path)
    img=Image.open(artifact)
    assert img.size==(1100,660)
    assert img.format=="JPEG"
    assert boss["theme"]=="Original theme retained"
    assert boss["name"]=="The Balladeer's Shadow"


def test_war_table_reads_existing_completed_ranks_only(tmp_path):
    data=render_war_table(world="takeover",defeated=(11,10),total=44,art_dir=tmp_path)
    assert Image.open(data).size==(1100,660)


def test_illustrated_send_preserves_original_embed_and_view_when_art_disabled(monkeypatch):
    async def run():
        monkeypatch.setenv("RPG_ART_ENABLED","off")
        channel=MagicMock()
        channel.send=AsyncMock(return_value="sent")
        embed=discord.Embed(title="Original untouched title",description="Original scenario")
        view=discord.ui.View(timeout=5)
        result=await illustrated_send(channel,embed,world="horror",view=view)
        assert result=="sent"
        kwargs=channel.send.await_args.kwargs
        assert kwargs["embed"] is embed and kwargs["view"] is view
        assert kwargs["embed"].description=="Original scenario"
        assert "file" not in kwargs
    asyncio.run(run())


def test_illustrated_send_attaches_art_without_overwriting_embed_text(monkeypatch):
    async def run():
        monkeypatch.setenv("RPG_ART_ENABLED","1")
        channel=MagicMock()
        channel.send=AsyncMock(return_value="sent")
        embed=discord.Embed(title="Untouched Gauntlet",description="Three existing choices")
        await illustrated_send(channel,embed,boss={"rank":11,"name":"Tartaglia","pts":12})
        args=channel.send.await_args.kwargs
        assert args["embed"].title=="Untouched Gauntlet"
        assert args["embed"].description=="Three existing choices"
        assert args["embed"].image.url=="attachment://harbinger-battlefield.jpg"
        assert args["file"].filename=="harbinger-battlefield.jpg"
        args["file"].close()
    asyncio.run(run())


def test_corrupted_and_takeover_portraits_are_isolated(tmp_path):
    boss = {"rank": 6, "name": "The Balladeer's Shadow"}
    root = tmp_path / "portraits"
    root.mkdir()
    legacy = root / "06-scaramouche.jpg"
    Image.new("RGB", (80, 80), "blue").save(legacy)
    # A non-corrupted legacy image must NEVER be reused as horror artwork.
    horror = portrait_path(boss, "Scaramouche", tmp_path, world="horror")
    takeover = portrait_path(boss, "Scaramouche", tmp_path, world="takeover")
    assert horror == root / "horror" / "06-scaramouche.jpg"
    assert not horror.is_file()
    assert takeover == legacy
    horror.parent.mkdir()
    Image.new("RGB", (80, 80), "red").save(horror)
    takeover.parent.mkdir()
    Image.new("RGB", (80, 80), "green").save(takeover.parent / takeover.name)
    assert portrait_path(boss, "Scaramouche", tmp_path, world="horror") == horror
    assert portrait_path(boss, "Scaramouche", tmp_path, world="takeover") == takeover.parent / takeover.name
    assert portrait_path(boss, "Wanderer", tmp_path, world="horror").name == "06-wanderer.jpg"


def test_rank_six_and_other_bosses_use_all_world_variants(tmp_path):
    for rank in BOSS_RANKS:
        boss = {"rank": rank}
        for identity in ("Scaramouche", "Wanderer"):
            horror = portrait_path(boss, identity, tmp_path, world="horror")
            takeover = portrait_path(boss, identity, tmp_path, world="takeover")
            assert horror.parent.name == "horror"
            assert takeover.parent.name == "takeover"
            assert horror.name == takeover.name
            assert horror != takeover


def test_new_world_backdrop_is_detected_after_first_render(tmp_path):
    # Installing the approved asset must not require a process restart.
    from rpg_visuals import _backdrop
    before = _backdrop("horror", (100, 100), tmp_path).getpixel((50, 50))
    folder = tmp_path / "worlds"
    folder.mkdir()
    Image.new("RGB", (100, 100), (160, 40, 30)).save(folder / "horror.jpg")
    after = _backdrop("horror", (100, 100), tmp_path).getpixel((50, 50))
    assert after != before


def test_opt_in_is_required_to_send_artwork(monkeypatch):
    async def run():
        monkeypatch.delenv("RPG_ART_ENABLED", raising=False)
        channel = MagicMock()
        channel.send = AsyncMock(return_value="original")
        embed = discord.Embed(title="Untouched", description="Original game")
        assert await illustrated_send(channel, embed) == "original"
        assert channel.send.await_args.kwargs["embed"] is embed
        assert "file" not in channel.send.await_args.kwargs
    asyncio.run(run())


def test_file_creation_failure_never_mutates_original_embed(monkeypatch):
    async def run():
        import rpg_visuals
        monkeypatch.setenv("RPG_ART_ENABLED", "1")
        def fail(*args, **kwargs):
            raise ValueError("Synthetic file failure")
        monkeypatch.setattr(rpg_visuals.discord, "File", fail)
        channel = MagicMock()
        channel.send = AsyncMock(return_value="original")
        embed = discord.Embed(title="Original", description="Unchanged scene")
        assert await illustrated_send(channel, embed) == "original"
        assert not embed.image.url
        assert "file" not in channel.send.await_args.kwargs
    asyncio.run(run())
