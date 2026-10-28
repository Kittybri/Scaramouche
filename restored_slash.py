"""Complete legacy Scaramouche slash groups using supported current handlers.

Registration uses per-root upserts, never a partial-tree bulk sync. Personal
dashboards/settings are ephemeral; only explicitly requested duo scenes are public.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Literal
import discord
from discord import app_commands
from discord.ext import commands
from interaction_policy import CURRENT, classify as classify_interaction, optional_command_blocked

log=logging.getLogger(__name__)


class RestoredSlash:
    ROOTS=('scaramouche','dashboard','world','prefs','duo','weatherlocation')
    def __init__(self,bot,mem,privacy,secret,archive,setup,respond,delivered,world=None,weather_location=None):
        self.bot,self.mem,self.privacy,self.secret=bot,mem,privacy,secret
        self.archive,self.setup,self.respond,self.delivered=archive,setup,respond,delivered
        self.world,self.weather_location=world,weather_location
        self.active={}
        self.synced=False

    async def forget(self,uid):
        tasks=list(self.active.get(uid,()))
        for task in tasks:
            if task is not asyncio.current_task(): task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not asyncio.current_task()),return_exceptions=True)

    async def invoke(self,i,name,text='',**kwargs):
        task=asyncio.current_task()
        self.active.setdefault(i.user.id,set()).add(task)
        context=classify_interaction(text,user_id=i.user.id,channel_id=i.channel_id or i.user.id,
                                    guild_id=i.guild_id,command=True,direct=True)
        token=CURRENT.set(context)
        try:
            await i.response.defer(ephemeral=name!='duo')
            if i.user.bot or not i.channel: raise PermissionError('A human and a valid channel are required.')
            if self.secret(text): raise PermissionError('Keep passwords and credentials out of chat. Rotate any exposed credential.')
            if await self.privacy.is_pending(i.user.id): raise PermissionError('Your privacy reset is still finishing.')
            if await self.mem.is_muted(i.user.id): raise PermissionError('Your bot-silence setting is active.')
            if i.guild and not i.channel.permissions_for(i.user).view_channel: raise PermissionError('You cannot use that channel.')
            if optional_command_blocked(context,kwargs.get('mode',name),text):
                raise PermissionError('This sounds serious. I will not turn it into a game.')
            context.consume('command')
            ctx=await commands.Context.from_interaction(i)
            if name in ('world','cases','achievements'):
                await ctx.send(await self.archive.read(i.user.id,i.channel_id,name),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            elif name=='worldadd':
                await ctx.send(await self.archive.add(i.user.id,i.channel_id,kwargs['entity_type'],text),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            elif name=='worldprefs':
                await ctx.send(await self.world.preferences(i.user.id,kwargs['setting'],kwargs['value']),
                               ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            elif name=='weatherlocation':
                await self.setup(ctx)
                await ctx.send(await self.weather_location(i.user.id,text or None),
                               ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
            elif name=='scaramouche':
                user=await self.setup(ctx)
                answer=await self.respond(i.user.id,i.channel_id,text,user,i.user.display_name,i.user.mention,
                    channel_obj=i.channel,is_dm=i.guild is None,defer_delivery=True,interaction=context)
                if await self.privacy.is_pending(i.user.id): return
                await ctx.send(answer,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
                await self.delivered(i.user.id,answer)
                # Deliberately do not put an ephemeral exchange into public channel
                # history or the duo transcript.
            elif name=='pref_voice':
                await self.setup(ctx)
                mode=kwargs['mode']
                if mode!='status': await self.mem.set_user_preference(i.user.id,'voice_enabled',int(mode=='on'))
                prefs=await self.mem.get_user_preferences(i.user.id)
                await ctx.send(f"Voice notes are {'on' if prefs.get('voice_enabled',True) else 'off'}.",ephemeral=True)
            else:
                if name=='duo':
                    mode=kwargs['mode']
                    parameter={'both':'prompt','duet':'prompt','argue':'topic','compare':'topic',
                               'interrogate':'topic','trial':'charge','mission':'objective','truthdare':'prompt'}[mode]
                    name,kwargs=mode,{parameter:text}
                cmd=self.bot.get_command(name)
                if cmd is None or not await cmd.can_run(ctx): raise PermissionError('That command is not available here.')
                ctx.command=cmd
                await cmd.callback(ctx,**kwargs)
        except asyncio.CancelledError:
            raise
        except (PermissionError,ValueError) as exc:
            await i.followup.send(str(exc),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            log.warning('Restored slash handler unavailable (%s)',type(exc).__name__)
            await i.followup.send('That command could not finish. Try again shortly.',ephemeral=True)
        finally:
            CURRENT.reset(token)
            self.active.get(i.user.id,set()).discard(task)
            if not self.active.get(i.user.id): self.active.pop(i.user.id,None)

    async def sync(self):
        if self.synced or not self.bot.application_id: return
        try:
            for name in self.ROOTS:
                cmd=self.bot.tree.get_command(name)
                await self.bot.http.upsert_global_command(self.bot.application_id,cmd.to_dict(self.bot.tree))
            self.synced=True
        except Exception as exc: log.warning('Restored slash registration unavailable (%s)',type(exc).__name__)

    def install(self):
        @self.bot.tree.command(name='scaramouche',description='Talk directly to Scaramouche.')
        async def scaramouche(i: discord.Interaction,message: str): await self.invoke(i,'scaramouche',message)
        @self.bot.tree.command(name='weatherlocation',description='Set, clear, or check your weather location privately.')
        async def weatherlocation(i: discord.Interaction,location: str=''):
            await self.invoke(i,'weatherlocation',location)
        dashboard=app_commands.Group(name='dashboard',description='Your relationship, achievements, and scene continuity.')
        for name in ('relationship','arc','duostate','scene','achievements'):
            def make(name):
                async def callback(i: discord.Interaction): await self.invoke(i,name)
                return callback
            dashboard.command(name=name,description=f'Show {name}.')(make(name))
        world=app_commands.Group(name='world',description='Shared story archive in this channel.')
        @world.command(name='state',description='Read this channel’s world entries.')
        async def state(i: discord.Interaction): await self.invoke(i,'world')
        @world.command(name='cases',description='Read this channel’s story cases.')
        async def cases(i: discord.Interaction): await self.invoke(i,'cases')
        @world.command(name='add',description='Remember your own story-world entry in this channel.')
        async def add(i: discord.Interaction,entity_type: Literal['enemy','ally','gift','place','prop','faction','figure','case'],name: str,summary: str=''):
            await self.invoke(i,'worldadd',name+' | '+summary,entity_type=entity_type)
        @world.command(name='prefs',description='Manage your grudge and lullaby world settings privately.')
        async def world_prefs(i: discord.Interaction,
                              setting: Literal['status','grudges','lullaby','lullaby_hour']='status',
                              value: str=''):
            await self.invoke(i,'worldprefs',setting+' '+value,setting=setting,value=value)
        prefs=app_commands.Group(name='prefs',description='Voice, utility, duo and speaker preferences.')
        @prefs.command(name='voice',description='Set voice-note replies; not voice-channel enrollment.')
        async def voice(i: discord.Interaction,mode: Literal['on','off','status']): await self.invoke(i,'pref_voice',mode=mode)
        @prefs.command(name='utility',description='Toggle cleaner factual formatting.')
        async def utility(i: discord.Interaction,mode: Literal['on','off']): await self.invoke(i,'utility',mode=mode)
        @prefs.command(name='duoauto',description='Toggle duo autoplay.')
        async def duoauto(i: discord.Interaction,mode: Literal['on','off']): await self.invoke(i,'duoauto',mode=mode)
        @prefs.command(name='rpdepth',description='Choose roleplay detail.')
        async def rpdepth(i: discord.Interaction,level: Literal['low','medium','high']): await self.invoke(i,'rpdepth',depth=level)
        @prefs.command(name='speaker',description='Choose this channel’s ambient speaker.')
        async def speaker(i: discord.Interaction,mode: Literal['auto','scaramouche','wanderer','both']): await self.invoke(i,'speaker',mode=mode)
        duo=app_commands.Group(name='duo',description='Start or inspect a coordinated two-bot scene.')
        @duo.command(name='state',description='Inspect the current duo scene.')
        async def duo_state(i: discord.Interaction): await self.invoke(i,'duostate')
        @duo.command(name='start',description='Explicitly start a public duo scene.')
        async def duo_start(i: discord.Interaction,mode: Literal['both','duet','argue','compare','interrogate','trial','mission','truthdare'],prompt: str):
            await self.invoke(i,'duo',prompt,mode=mode)
        for group in (dashboard,world,prefs,duo): self.bot.tree.add_command(group)
        self.bot.add_listener(self.sync,'on_ready')
        return self
