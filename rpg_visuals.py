"""Optional illustrated presentation for the existing Harbinger Gauntlet.

Purely visual: NEVER selects choices, changes scores, narrates scenes, or writes
player state. Portraits must come from a deliberately curated art directory;
unknown portraits remain absent rather than drawing the wrong character.
"""
from __future__ import annotations

import asyncio
import io
from functools import lru_cache
import logging
import math
import os
import random
from pathlib import Path

import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps

LOG = logging.getLogger(__name__)
ART_DIR = Path(__file__).resolve().parent / "assets" / "rpg"
NAMES = ("Tartaglia", "Capitano", "Pantalone", "La Signora", "Sandrone",
         "The Balladeer", "Pulcinella", "Arlecchino", "Columbina",
         "Il Dottore", "Pierro")
SIGNATURE = {
    11: ("ABYSSAL HYDRO", (66, 151, 228)),
    10: ("LIVING BLACK MIST", (113, 131, 169)),
    9: ("CURSED GOLD", (211, 164, 65)),
    8: ("CRYOFLAME ASH", (219, 95, 131)),
    7: ("FLESH AND GEARS", (173, 146, 123)),
    6: ("CRACKED PUPPET / SMOKE", (164, 95, 220)),
    5: ("HAUNTED DECREES", (161, 131, 202)),
    4: ("BLACK FLAMES", (235, 76, 93)),
    3: ("STOLEN DREAM THREADS", (174, 156, 225)),
    2: ("FRACTURED RESEARCH", (82, 189, 206)),
    1: ("FROZEN REALITY", (161, 198, 223)),
}
BOSS_RANKS = (11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)


def _font(size, bold=False):
    for filename in (("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
                      else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                     "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(filename, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text(draw, xy, value, size=25, color=(242, 225, 196), bold=False):
    draw.text(xy, str(value), fill=color, font=_font(size, bold))


def _world(world):
    return "horror" if world == "horror" else "takeover"


def portrait_filename(boss, bot_name):
    """Rank 6 intentionally has distinct canonical identities in the two RPGs."""
    rank = int((boss or {}).get("rank", 0))
    if rank not in BOSS_RANKS:
        return None
    name = "wanderer" if str(bot_name).lower().startswith("wander") else "scaramouche"
    return "06-" + name + ".jpg" if rank == 6 else "%02d.jpg" % rank


def portrait_path(boss, bot_name, art_dir=None):
    filename = portrait_filename(boss, bot_name)
    return ((Path(art_dir) if art_dir is not None else ART_DIR) / "portraits" / filename) if filename else None


@lru_cache(maxsize=8)
def _backdrop(world, size=(1100, 660), art_dir=None):
    world = _world(world)
    w, h = size
    path = (Path(art_dir) if art_dir is not None else ART_DIR) / "worlds" / (world + ".jpg")
    if path.is_file():
        with Image.open(path) as source:
            image = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    else:
        # Built-in painted atmosphere; missing external art never stops the RPG.
        image = Image.new("RGB", size)
        pixels = image.load()
        for y in range(h):
            t = y / float(h)
            for x in range(w):
                haze = math.sin(x / 92.0) * 7 + math.cos((y + x / 6) / 85) * 8
                if world == "horror":
                    pixels[x, y] = (int(30 + 31 * t + haze) % 256,
                                    int(16 + 8 * t + haze / 3) % 256,
                                    int(47 + 15 * (1-t) + haze) % 256)
                else:
                    pixels[x, y] = (int(20 + 28 * t + haze) % 256,
                                    int(35 + 25 * (1-t) + haze) % 256,
                                    int(55 + 12 * (1-t) + haze) % 256)
    d = ImageDraw.Draw(image, "RGBA")
    rng = random.Random(42 if world == "horror" else 29)
    for i in range(45):
        x = rng.randrange(w)
        d.line((x, rng.randrange(100, h), x+rng.randrange(-70, 70), h),
               fill=(12, 10, 22, 85), width=rng.randrange(2, 10))
    for x in range(-30, w, 125):
        d.polygon([(x, h-135), (x+60, h-310), (x+110, h-135)],
                  fill=(4, 8, 18, 105))
    return image.convert("RGB")


def _frame(draw, w, h, accent):
    draw.rounded_rectangle((9, 9, w-10, h-10), radius=20,
                           outline=(208, 177, 113), width=5)
    draw.rounded_rectangle((20, 20, w-21, h-21), radius=17,
                           outline=accent, width=2)
    for x in (32, w-32):
        for y in (32, h-32):
            draw.ellipse((x-9, y-9, x+9, y+9), fill=(219, 190, 112))


def _paste_portrait(image, boss, bot_name, target, art_dir):
    path = portrait_path(boss, bot_name, art_dir)
    if not path or not path.is_file():
        return False
    try:
        with Image.open(path) as source:
            thumb = ImageOps.fit(source.convert("RGB"),
                                 (target[2]-target[0], target[3]-target[1]),
                                 method=Image.Resampling.LANCZOS)
        image.paste(thumb, target[:2])
        return True
    except (OSError, ValueError):
        return False


def _corruption(draw, rank, area):
    """Character-specific *visual* curse, keyed to already established lore."""
    x0, y0, x1, y1 = area
    rng = random.Random(rank * 29)
    accent = SIGNATURE.get(rank, ("", (180, 100, 180)))[1]
    if rank == 6:
        # The Balladeer is a broken, haunted puppet: cracks + smoke threads.
        for i in range(11):
            x = rng.randrange(x0+40, x1-30)
            y = rng.randrange(y0+30, y1-80)
            draw.line([(x,y), (x+rng.randrange(-25,25), y+35),
                       (x+rng.randrange(-45,45), y+75)],
                      fill=(212, 199, 236, 190), width=2)
        for i in range(17):
            x = rng.randrange(x0, x1)
            y = rng.randrange(y0, y1)
            draw.arc((x-40,y-70,x+110,y+45), 100, 320,
                     fill=(164, 110, 210, 140), width=3)
    elif rank in (11, 10, 1):
        for i in range(14):
            x = rng.randrange(x0, x1)
            y = rng.randrange(y0, y1)
            draw.arc((x-38,y-45,x+85,y+65), 65, 255,
                     fill=(*accent, 165), width=3)
    elif rank in (7, 9, 2):
        for i in range(14):
            x = rng.randrange(x0+20, x1-20)
            y = rng.randrange(y0+25, y1-25)
            draw.ellipse((x-6,y-6,x+6,y+6), outline=(*accent,170), width=3)
            draw.line((x-18,y,x+18,y), fill=(*accent,120), width=2)
    else:
        for i in range(16):
            x = rng.randrange(x0+30, x1-25)
            y = rng.randrange(y0+30, y1-30)
            draw.line((x,y,x+rng.randrange(-45,45),y-rng.randrange(20,80)),
                      fill=(*accent,150), width=3)


def _pips(draw, x, y, defeated):
    beaten = set(defeated or ())
    for i, rank in enumerate(BOSS_RANKS):
        px = x + i * 52
        active = rank in beaten
        draw.ellipse((px,y,px+37,y+37),
                     fill=(107, 66, 48) if active else (24, 25, 39),
                     outline=(244, 197, 107) if active else (106, 104, 124),
                     width=2)
        _text(draw, (px+10 if rank<10 else px+5, y+9), rank, 15,
              (246, 223, 173) if active else (175, 170, 189), bold=True)
        if active:
            draw.line((px+7,y+19,px+14,y+26,px+29,y+11),
                      fill=(119, 229, 151), width=4)


def render_scene(*, kind="encounter", world="horror", boss=None, bot_name="Wanderer",
                 round_num=0, points=0, total=0, defeated=(), origin="", element="",
                 roll=0, art_dir=None):
    """Render an image from existing game state, with no game-state mutation."""
    kind = kind if kind in {"world", "encounter", "dice", "victory", "defeat", "board"} else "encounter"
    rank = int((boss or {}).get("rank", 0))
    tint = SIGNATURE.get(rank, ("WORLD", (171, 118, 195)))[1]
    w, h = (1100, 660)
    image = _backdrop(world, (w, h), art_dir).copy()
    d = ImageDraw.Draw(image, "RGBA")
    d.rounded_rectangle((36, 88, 465, 532), radius=16, fill=(9, 15, 30, 220),
                        outline=(*tint, 230), width=3)
    d.rounded_rectangle((494, 88, 1060, 532), radius=18, fill=(7, 12, 29, 220),
                        outline=(199, 174, 115, 210), width=3)
    if boss:
        has_art = _paste_portrait(image, boss, bot_name, (47, 98, 455, 520), art_dir)
        d = ImageDraw.Draw(image, "RGBA")
        if world == "horror" and has_art:
            _corruption(d, rank, (47, 98, 455, 520))
        if not has_art:
            # Show thematic artwork only: never substitute an unrelated character.
            for i in range(35):
                alpha = int(90 * (1.0-i/35))
                d.arc((65+i, 110+i, 438-i, 507-i), 100, 320,
                      fill=(*tint, alpha), width=4)
            _text(d,(93,263),"SHADOWED ADVERSARY",20,(209,190,190))
    _text(d,(54,36),"HARBINGER CHRONICLES",34,(248,221,151),True)
    _text(d,(505,112),("CORRUPTED HORROR" if world=="horror" else "HARBINGER TAKEOVER"),
          20,(*tint,),True)
    if boss:
        _text(d,(505,154),("HARBINGER #%s" % rank),23,(217,202,180))
        name = str(boss.get("name", "Unknown"))[:27]
        _text(d,(505,190),name,31,(246,227,204),True)
        if world=="horror":
            _text(d,(505,242),SIGNATURE[rank][0],19,(*tint,))
        _text(d,(505,303),"ROUND  %s / 10" % (round_num or "-"),20,(221,217,211))
        _text(d,(505,345),"SKILL  %s / %s" % (int(points),int(boss.get("pts",0))),21,(243,227,191))
        denom=max(1,int(boss.get("pts",1)))
        proportion=min(1,max(0,int(points)/denom))
        d.rounded_rectangle((505,390,1009,410),radius=8,fill=(30,32,49))
        d.rounded_rectangle((505,390,int(505+504*proportion),410),
                            radius=8,fill=(*tint,))
    else:
        _text(d,(505,176),"CHOOSE YOUR WORLD",34,(245,221,186),True)
        _text(d,(505,243),"Two worlds. Eleven Harbingers.",24,(222,216,210))
        _text(d,(505,298),"Your story and choices stay unchanged.",17,(190,187,194))
    if origin:
        _text(d,(505,443),("ORIGIN  "+str(origin))[:45],18,(192,201,222))
    if element:
        _text(d,(505,470),("VISION  "+str(element))[:45],18,(192,201,222))
    if kind in ("victory","defeat"):
        txt = "VICTORY" if kind=="victory" else "DEFEAT"
        _text(d,(506,477),txt,38,(230,192,89) if kind=="victory" else (232,92,112),True)
    if kind=="dice":
        _text(d,(506,465),"ROLL  "+str(roll),34,(231,203,139),True)
    _text(d,(48,556),"HAR BINGER WAR TABLE   |   CONQUERED SEALS".replace("HAR B","HARB"),18,(207,190,163),True)
    _pips(d, 290, 577, defeated)
    _text(d,(49,607),"TOTAL POINTS   %d" % int(total),18,(214,202,182),True)
    _frame(d,w,h,tint)
    buf=io.BytesIO()
    image.save(buf,format="JPEG",quality=85,optimize=True)
    buf.seek(0)
    return buf


def render_war_table(*, world="horror", defeated=(), total=0, bot_name="Wanderer", art_dir=None):
    image=_backdrop(world, (1100,660), art_dir).copy()
    d=ImageDraw.Draw(image,"RGBA")
    d.rounded_rectangle((36,82,1064,592),radius=20,fill=(15,15,23,212),
                        outline=(201,168,105,230),width=4)
    _text(d,(60,30),"THE HARBINGER WAR TABLE",35,(244,214,149),True)
    defeated=set(defeated or ())
    for i,rank in enumerate(BOSS_RANKS):
        col=i%6; row=i//6
        left=70+col*166+(0 if row==0 else 75)
        top=117+row*221
        if rank==6:
            name="The Wanderer's Echo" if str(bot_name).lower().startswith("wander") else "The Balladeer's Shadow"
        else:
            name=NAMES[i]
        boss={"rank":rank,"name":name}
        exists=_paste_portrait(image,boss,bot_name,(left,top,left+136,top+141),art_dir)
        d=ImageDraw.Draw(image,"RGBA")
        if exists and world=="horror":
            _corruption(d,rank,(left,top,left+136,top+141))
        if not exists:
            d.rectangle((left,top,left+136,top+141),fill=(25,31,45,160))
            _text(d,(left+42,top+46),str(rank),42,(143,132,157),True)
        if rank not in defeated:
            d.rectangle((left,top,left+136,top+141),fill=(7,9,20,147))
        d.rounded_rectangle((left-2,top-2,left+138,top+143),radius=7,
                            outline=(236,186,98) if rank in defeated else (119,104,105),width=3)
        _text(d,(left+4,top+152),("#%s %s" % (rank,name))[:18],14,
              (247,220,166) if rank in defeated else (190,188,192),True)
        if rank in defeated:
            _text(d,(left+44,top+47),"CLEAR",21,(235,194,84),True)
    _text(d,(68,555),"CONQUERED %d / 11       TOTAL POINTS %d" % (len(defeated),int(total)),
          21,(232,210,155),True)
    _frame(d,1100,660,(131,101,74))
    buf=io.BytesIO()
    image.save(buf,format="JPEG",quality=86,optimize=True)
    buf.seek(0)
    return buf


async def illustrated_send(channel, embed, *, kind="encounter", world="horror",
                           boss=None, bot_name="Wanderer", round_num=0, points=0,
                           total=0, defeated=(), origin="", element="", roll=0,
                           view=None, allowed_mentions=None, board=False):
    """Send the ORIGINAL embed/buttons; if rendering fails, send text unchanged."""
    kwargs={"embed":embed}
    if view is not None:
        kwargs["view"]=view
    if allowed_mentions is not None:
        kwargs["allowed_mentions"]=allowed_mentions
    if os.getenv("RPG_ART_ENABLED","1").strip().lower() not in {"0","false","off","no"}:
        try:
            art = await asyncio.to_thread(
                render_war_table if board else render_scene,
                **({"world":world,"defeated":defeated,"total":total,"bot_name":bot_name}
                   if board else
                   {"kind":kind,"world":world,"boss":boss,"bot_name":bot_name,
                    "round_num":round_num,"points":points,"total":total,
                    "defeated":defeated,"origin":origin,"element":element,"roll":roll})
            )
            filename="harbinger-war-table.jpg" if board else "harbinger-battlefield.jpg"
            embed.set_image(url="attachment://"+filename)
            kwargs["file"]=discord.File(art,filename=filename)
        except Exception as exc:
            LOG.warning("RPG artwork unavailable; sending original text (%s)",type(exc).__name__)
    return await channel.send(**kwargs)
