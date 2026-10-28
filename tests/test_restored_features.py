import asyncio
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import discord
from discord.ext import commands
import pytest
from birthday_commands import BirthdayController, parse_birthday
from memory import Memory
from world_archive import WorldArchive
from restored_slash import RestoredSlash
from restored_admin import install as install_admin

def run(fn):
    import functools
    @functools.wraps(fn)
    def wrapper(*args,**kwargs): return asyncio.run(fn(*args,**kwargs))
    return wrapper

async def fixture(tmp):
    m=Memory('scaramouche',str(tmp/'local.db'),str(tmp/'shared.db'))
    await m.init()
    user=NS(id=1,display_name='Synthetic A',send=AsyncMock())
    channel=NS(id=10,send=AsyncMock())
    guild=NS(id=20,me=NS(id=99),get_member=lambda uid:user if uid==1 else None)
    channel.guild=guild
    channel.permissions_for=lambda member:NS(view_channel=True,send_messages=True)
    ctx=NS(author=user,channel=channel,guild=guild,send=AsyncMock())
    async def setup(ctx):
        await m.upsert_user(ctx.author.id,'synthetic',ctx.author.display_name)
        return await m.get_user(ctx.author.id)
    b=BirthdayController(NS(get_channel=lambda cid:channel if cid==10 else None),m,AsyncMock(return_value=False),lambda t:False,setup)
    return m,b,ctx

@pytest.mark.parametrize('text,expected',[('02-29',(2,29,None)),('February 29',(2,29,None)),('2000-02-29',(2,29,2000)),('March 14',(3,14,None))])
def test_birthday_formats(text,expected): assert parse_birthday(text)==expected

@pytest.mark.parametrize('text',['02-30','1901-02-29','3000-01-01','not a date'])
def test_birthday_invalid(text):
    with pytest.raises(ValueError): parse_birthday(text)

@run
async def test_birthday_due_deduplication_and_scope(tmp_path):
    m,b,ctx=await fixture(tmp_path)
    await b.command(ctx,'10-08')
    now=datetime(2026,10,8,18,tzinfo=timezone.utc)
    assert len(await b.due(now))==1
    await b.tick(now)
    ctx.channel.send.assert_awaited_once()
    await b.tick(now)
    ctx.channel.send.assert_awaited_once()
    # Re-entering the date must not reset this year's claim.
    await b.command(ctx,'10-08')
    await b.tick(now)
    ctx.channel.send.assert_awaited_once()
    await b.command(ctx,'')
    assert '10-08' in ctx.author.send.call_args.args[0]
    assert not any('10-08' in str(c) for c in ctx.send.call_args_list)

@run
async def test_birthday_optout_quiet_hours_and_deletion(tmp_path):
    m,b,ctx=await fixture(tmp_path)
    await b.command(ctx,'10-08')
    now=datetime(2026,10,8,18,tzinfo=timezone.utc)
    await m.set_quiet_hours(1,10,13)
    assert not await b.due(now)
    await m.set_quiet_hours(1,23,8)
    await b.command(ctx,'off')
    assert not await b.due(now)
    await b.command(ctx,'on')
    assert len(await b.due(now))==1
    await m.set_mode(1,'proactive',False)
    assert not await b.due(now)
    await m.reset_user_local(1)
    assert await b.get(1) is None

@run
async def test_birthday_cannot_enroll_another_user(tmp_path):
    m,b,ctx=await fixture(tmp_path)
    await b.command(ctx,'<@2> 10-08')
    assert await b.get(1) is None and await b.get(2) is None

@run
async def test_world_archive_user_channel_and_deletion_isolation(tmp_path):
    m,b,ctx=await fixture(tmp_path)
    archive=WorldArchive(None,m,AsyncMock())
    await archive.add(1,10,'prop','Synthetic feather | user A marker')
    await archive.add(2,10,'prop','Synthetic feather | user B marker')
    await archive.add(1,11,'prop','Synthetic feather | private channel marker')
    text=await archive.read(1,10,'world')
    assert 'user A marker' in text and 'user B marker' in text
    assert 'private channel marker' not in text
    assert 'remembered_artifact' in await archive.read(1,10,'achievements')
    await m.reset_user_shared(1)
    assert 'user A marker' not in await archive.read(2,10,'world')
    assert 'user B marker' in await archive.read(2,10,'world')
    assert 'remembered_artifact' not in await archive.read(1,10,'achievements')
    with sqlite3.connect(m.shared_db_path) as db: assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'

@run
async def test_world_credentials_and_pending_reset_never_write(tmp_path):
    m,b,ctx=await fixture(tmp_path)
    archive=WorldArchive(None,m,AsyncMock(side_effect=PermissionError('no')))
    with pytest.raises(PermissionError): await archive.add(1,10,'prop','marker')
    with sqlite3.connect(m.shared_db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM shared_world_entities').fetchone()[0]==0

@run
async def test_slash_registration_preserves_tarot_google_and_no_bulk_sync(tmp_path):
    bot=commands.Bot(command_prefix='!',intents=discord.Intents.none())
    for name in ('google','tarot','dailycard','tarothistory','tarotsettings'):
        async def callback(i: discord.Interaction): pass
        bot.tree.command(name=name)(callback)
    c=RestoredSlash(bot,None,None,None,None,None,None,None).install()
    names={x.qualified_name for x in bot.tree.walk_commands()}
    assert {'dashboard achievements','world add','world prefs','weatherlocation','prefs voice','duo start','scaramouche','google','tarot'}<=names
    assert len(names)==27
    bot._connection.application_id=123
    bot.http.upsert_global_command=AsyncMock()
    bot.tree.sync=AsyncMock(side_effect=AssertionError('bulk overwrite'))
    await c.sync()
    assert [x.args[1]['name'] for x in bot.http.upsert_global_command.call_args_list]==list(c.ROOTS)
    await c.sync()
    assert bot.http.upsert_global_command.await_count==6
    await bot.close()

@run
async def test_slash_forget_cancels_generation_before_later_learning():
    c=RestoredSlash(None,None,None,None,None,None,None,None)
    work=asyncio.create_task(asyncio.sleep(20))
    c.active[1]={work}
    await c.forget(1)
    assert work.cancelled()

@run
async def test_admin_inventory_fail_closed_and_private():
    for owner,actor in [(0,1),(1,2),(1,1)]:
        bot=commands.Bot(command_prefix='!',intents=discord.Intents.none())
        cmd=install_admin(bot,owner)
        ctx=NS(author=NS(id=actor,send=AsyncMock()),send=AsyncMock())
        await cmd.callback(ctx)
        assert ctx.author.send.await_count==int(owner==actor==1)
        await bot.close()

@run
async def test_current_duo_cases_are_visible_without_duplicate_storage(tmp_path):
    m,b,ctx=await fixture(tmp_path)
    await m.start_duo_story(10,'mission','Synthetic current case')
    await m.start_duo_story(11,'trial','Other channel case')
    archive=WorldArchive(None,m,AsyncMock())
    result=await archive.read(1,10,'cases')
    assert 'Synthetic current case' in result and 'Other channel' not in result
    await m.resolve_duo_story(10,'mission','Synthetic resolution')
    assert 'Synthetic resolution' in await archive.read(1,10,'cases')

@run
async def test_reset_cancels_admitted_prefix_work_before_insert():
    from restored_lifecycle import InteractionTasks
    work=InteractionTasks()
    entered=asyncio.Event()
    insert=AsyncMock()
    async def command():
        async with work.track(1):
            entered.set()
            await asyncio.sleep(30)
            await insert()
    pending=asyncio.create_task(command())
    await entered.wait()
    await work.forget(1)
    assert pending.cancelled() and not work.active
    insert.assert_not_awaited()
