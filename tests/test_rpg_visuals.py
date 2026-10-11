"""Presentation-only illustrated RPG: identity, unchanged text, graceful fallbacks."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from PIL import Image
import discord
from rpg_visuals import (BOSS_RANKS, portrait_filename, render_scene,
                         render_war_table, illustrated_send)


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
