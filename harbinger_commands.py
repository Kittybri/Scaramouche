"""The historical Harbinger Gauntlet, with scoped, revocable Discord panels."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import discord
from awareness_features import classify_safety
from restoration_store import StaleCampaign
from restored_lifecycle import WORK
from rpg_visuals import illustrated_send
from harbinger_lore import (
    HARBINGERS_HORROR, HARBINGERS_TAKEOVER, FIVE_STAR_CHARS,
    FOUR_STAR_CHARS, MONSTERS, FORBIDDEN_ROLL,
)

log = logging.getLogger(__name__)
ELEMENTS = ('pyro','hydro','electro','dendro','cryo','anemo','geo')


def stage(state):
    """Also understands the original snapshot's persisted setup/progress shape."""
    if state.get('stage'):
        return state['stage']
    if not state.get('world_type'):
        return 'world'
    if not state.get('char_type'):
        return 'origin'
    if state['char_type']=='teyvat' and not state.get('element'):
        return 'element'
    return 'play' if state.get('active') else 'finished'


def parse_scenario(raw):
    try:
        data=json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
        choices=data['choices']
        if len(choices)!=3 or sorted(c['points'] for c in choices)!=[0,1,3]:
            return None
        return {'scenario':str(data['scenario'])[:1800], 'choices':[
            {'label':str(c['label'])[:70],'result':str(c['result'])[:700],'points':int(c['points'])}
            for c in choices]}
    except (ValueError,KeyError,TypeError):
        return None


class QuestView(discord.ui.View):
    def __init__(self, controller, uid, guild, channel, revision, state, *, reset=False):
        super().__init__(timeout=300)
        self.controller=controller
        self.uid,self.guild,self.channel,self.revision=uid,guild,channel,revision
        self.state=state
        self.claimed=False
        if reset:
            options=[('Confirm reset','reset'),('Keep my quest','cancel')]
        elif stage(state)=='world':
            options=[('Corrupted Horror World','horror'),('Harbinger Takeover World','takeover')]
        elif stage(state)=='origin':
            options=[('Transmigrated from Earth','transmigrated'),('Born in Teyvat','teyvat')]
        elif stage(state)=='element':
            options=[(e.title(),e) for e in ELEMENTS]
        else:
            options=[(f"{chr(65+i)}) {c['label']}",str(i)) for i,c in enumerate(state.get('scenario_data',{}).get('choices',[]))]
        for i,(label,value) in enumerate(options):
            button=discord.ui.Button(label=label[:80],style=discord.ButtonStyle.primary,row=i//3)
            async def callback(interaction, value=value):
                await self.choose(interaction,value)
            button.callback=callback
            self.add_item(button)

    async def choose(self, interaction, choice):
        if interaction.user.id!=self.uid or (interaction.guild_id or 0)!=self.guild or interaction.channel_id!=self.channel:
            await interaction.response.send_message("This isn't your quest.",ephemeral=True)
            return
        if self.claimed:
            await interaction.response.send_message('That choice is already locked in.',ephemeral=True)
            return
        # Set before the first await: rapid double clicks cannot both enter.
        self.claimed=True
        await interaction.response.defer()
        try:
            await self.controller.guard(self.uid)
            await self.controller.store.valid(self.uid,self.guild,self.revision)
            for child in self.children:
                child.disabled=True
            await interaction.edit_original_response(view=self)
            if choice=='cancel':
                return
            if choice=='reset':
                await self.controller.store.reset(self.uid,self.guild,self.revision)
                await interaction.followup.send('Fine. Your campaign is reset. Use !rpg1 to start again.',ephemeral=True)
                return
            revision,state=await self.controller.advance(self.uid,self.guild,self.revision,self.state,choice)
            await self.controller.show(interaction.channel,self.uid,self.guild,revision,state)
        except (PermissionError,ValueError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            log.warning('Harbinger interaction unavailable (%s)',type(exc).__name__)
            await interaction.followup.send('The path can wait. Use !rpg1 to resume.',ephemeral=True)
        finally:
            self.stop()


class HarbingerController:
    def __init__(self, bot, store, client, model, deletion_pending, secret_detector):
        self.bot,self.store,self.client,self.model=bot,store,client,model
        self.deletion_pending,self.secret_detector=deletion_pending,secret_detector
        self.slots=None
        self.slot_loop=None

    async def guard(self, uid, text=''):
        if await self.deletion_pending(uid):
            raise StaleCampaign('Your privacy reset is still finishing.')
        if self.secret_detector(text):
            raise ValueError('Keep credentials out of the game.')
        if classify_safety(text).protective:
            raise ValueError('Put the game aside for this. Use a normal message for real-world concerns.')

    async def generate(self, prompt):
        loop=asyncio.get_running_loop()
        if self.slot_loop is not loop:
            self.slots=asyncio.Semaphore(2)
            self.slot_loop=loop
        def call():
            r=self.client.call_with_retry(model=self.model,max_completion_tokens=650,
                messages=[{'role':'system','content':
                    'You narrate Scaramouche’s fictional Harbinger Gauntlet. Keep his cutting, theatrical voice. '
                    'Follow the requested output format. No real-world personal information or secrets.'},
                    {'role':'user','content':prompt}],temperature=0.8)
            return r.choices[0].message.content or ''
        try:
            async with self.slots:
                return await asyncio.wait_for(asyncio.to_thread(call),35)
        except Exception as exc:
            log.warning('Harbinger generation unavailable (%s)',type(exc).__name__)
            return ''

    def boss(self,state):
        bosses=HARBINGERS_HORROR if state.get('world_type')=='horror' else HARBINGERS_TAKEOVER
        return bosses[min(10,int(state.get('current_boss',0)))]

    async def prepare(self, uid, guild, revision, state):
        if stage(state)!='play' or state.get('scenario_data',{}).get('choices'):
            return revision,state
        boss=self.boss(state)
        raw=await self.generate(
            f"Setting: {boss['theme']}\nPlayer origin: {state['char_type']}; element: {state.get('element','none')}. "
            'Earth-born characters must use stealth, wits, or escape, not magical combat. '
            f"Round {state['current_round']}/10. Generate one survival scenario and three tactical choices. "
            'Return JSON only: {"scenario":"2-3 sentences","choices":'
            '[{"label":"short choice","points":3,"result":"one sentence"},'
            '{"label":"short choice","points":1,"result":"one sentence"},'
            '{"label":"short choice","points":0,"result":"one sentence"}]}. Randomize order; exactly 0,1,3 points.')
        await self.guard(uid)
        await self.store.valid(uid,guild,revision)
        scenario=parse_scenario(raw)
        if not scenario:
            raise ValueError('The path is obscured. Your progress is safe; use !rpg1 to resume.')
        state={**state,'scenario_data':scenario}
        revision=await self.store.update(uid,guild,revision,state)
        return revision,state

    async def advance(self, uid, guild, revision, state, choice):
        state=json.loads(json.dumps(state))
        current=stage(state)
        if current=='world' and choice in ('horror','takeover'):
            state.update(world_type=choice,stage='origin')
        elif current=='origin' and choice in ('teyvat','transmigrated'):
            state.update(char_type=choice,stage='element' if choice=='teyvat' else 'play',element='none')
        elif current=='element' and choice in ELEMENTS:
            state.update(element=choice,stage='play')
        elif current=='play':
            choices=state.get('scenario_data',{}).get('choices',[])
            if choice not in ('0','1','2') or len(choices)!=3:
                raise ValueError('That choice is not available.')
            selected=choices[int(choice)]
            points=selected['points']
            state['boss_points']+=points
            state['total_points']+=points
            state['last_result']=selected['result']
            state['current_round']+=1
            state['scenario_data']={}
            if state['current_round']==10:
                # Preserve the historical round-ten encounter/point distribution.
                roll=random.randint(1,20)
                if roll==FORBIDDEN_ROLL:
                    encounter=random.choice(MONSTERS)
                    delta=-1
                    line=f"{encounter['name']}: {encounter['attack']}."
                else:
                    encounter=random.choice(FIVE_STAR_CHARS if roll>=16 else FOUR_STAR_CHARS)
                    delta=4 if roll>=16 else 2
                    line=f"{encounter['name']}: {encounter['line']}"
                state['boss_points']=max(0,state['boss_points']+delta)
                state['total_points']=max(0,state['total_points']+delta)
                state['last_result']+=f'\nDice {roll} ({delta:+d} points). {line}'
                state['stage']='boss'
        else:
            raise StaleCampaign('That quest panel is outdated.')
        if stage(state)=='play' and 'current_boss' not in state:
            state.update(current_boss=0,current_round=1,boss_points=0,total_points=0,
                         bosses_beaten=[],scenario_data={},active=True)
        # Atomic claim happens BEFORE generation: stale panels cannot award points twice.
        revision=await self.store.update(uid,guild,revision,state)
        return revision,state

    async def resolve_boss(self, uid, guild, revision, state):
        if stage(state)!='boss':
            return revision,state
        boss=self.boss(state)
        won=state['boss_points']>=boss['pts']
        narration=await self.generate(f"Fictional boss encounter: {boss['theme']}. "
            f"Player: {state['char_type']}, {state.get('element','none')}. "
            f"Points {state['boss_points']}, needs {boss['pts']}. {'Victory' if won else 'Defeat'}. "
            'Narrate the outcome in 2-4 sentences, as the established game narrator. '
            'Earth-born players escape or outwit their opponent. No JSON.')
        await self.guard(uid)
        state={**state,'last_result':state.get('last_result','')+'\n'+(narration[:1500] or ('The Harbinger falls.' if won else "You weren't ready."))}
        if won:
            state['bosses_beaten']=[*state['bosses_beaten'],boss['rank']]
            state['current_boss']+=1
            state.update(current_round=1,boss_points=0,scenario_data={})
        complete=won and state['current_boss']==11
        state['stage']='finished' if complete or not won else 'play'
        state['active']=state['stage']=='play'
        state['victory']=complete
        revision=await self.store.update(uid,guild,revision,state,completed=complete)
        return revision,state

    async def show(self, channel, uid, guild, revision, state):
        if stage(state)=='dice':
            # Resume an imported legacy round-ten checkpoint without creating
            # a tenth tactical choice. Persist the roll before any generation.
            state=dict(state)
            roll=random.randint(1,20)
            delta=-1 if roll==FORBIDDEN_ROLL else 4 if roll>=16 else 2
            state.update(stage='boss',current_round=11,scenario_data={},
                         boss_points=max(0,state['boss_points']+delta),
                         total_points=max(0,state['total_points']+delta),
                         last_result=f'Dice {roll} ({delta:+d} points).')
            revision=await self.store.update(uid,guild,revision,state)
        revision,state=await self.resolve_boss(uid,guild,revision,state)
        revision,state=await self.prepare(uid,guild,revision,state)
        s=stage(state)
        descriptions={'world':'Choose your world: corrupted horror or Fatui occupation.',
            'origin':'Choose your origin: Earth-born survivor or a Teyvat Vision holder.',
            'element':'Choose your Vision.',
            'finished':('All eleven defeated. You actually earned my respect.' if state.get('victory') else 'Your run is over. Use !rpg1reset to try again.')}
        description=descriptions.get(s,state.get('scenario_data',{}).get('scenario',''))
        embed=discord.Embed(title='⚔️ HARBINGER GAUNTLET — The Fall of Teyvat',description=description,color=0x8B0000)
        if state.get('last_result'):
            embed.add_field(name='Last outcome',value=state['last_result'][:1024],inline=False)
        if s=='play':
            boss=self.boss(state)
            embed.add_field(name=f"#{boss['rank']} {boss['name']}",value=
                f"Round {state['current_round']}/10 • {state['boss_points']}/{boss['pts']} points • Total {state['total_points']}")
        view=None if s=='finished' else QuestView(self,uid,guild,channel.id,revision,state)
        await self.guard(uid)
        await self.store.valid(uid,guild,revision)
        visual_stage = (
            "victory" if s == "finished" and state.get("victory")
            else "defeat" if s == "finished"
            else "world" if s in ("world", "origin", "element")
            else "encounter"
        )
        await illustrated_send(
            channel, embed, view=view, allowed_mentions=discord.AllowedMentions.none(),
            kind=visual_stage, bot_name="Scaramouche",
            world=state.get("world_type") or "horror",
            boss=self.boss(state) if s not in ("world", "origin", "element") else None,
            round_num=state.get("current_round", 0),
            points=state.get("boss_points", 0),
            total=state.get("total_points", 0),
            defeated=state.get("bosses_beaten", ()),
            origin=state.get("char_type", ""), element=state.get("element", ""),
        )

    async def command(self, ctx, action):
        uid,guild=ctx.author.id,getattr(ctx.guild,'id',0)
        try:
            await self.guard(uid,getattr(ctx.message,'content',''))
            if action=='rank':
                rows=await self.store.leaderboard(guild,uid)
                lines=[]
                for user,clears,points in rows:
                    member=ctx.guild.get_member(user) if ctx.guild else ctx.author
                    if member:
                        lines.append(f'{discord.utils.escape_markdown(member.display_name)} — {clears} clears, best {points}')
                await ctx.send('🏆 Harbinger Gauntlet — Hall of Champions\n'+('\n'.join(lines) or 'No champions here. Yet.'),allowed_mentions=discord.AllowedMentions.none())
                return
            current=await self.store.get(uid,guild)
            if action=='stats':
                if not current:
                    await ctx.send('Use !rpg1 to begin your campaign here.'); return
                _,s=current
                await ctx.send(f"Harbingers defeated: {len(s.get('bosses_beaten',[]))}/11 • Total points: {s.get('total_points',0)} • Stage: {stage(s)}")
                await illustrated_send(
                    ctx.channel,
                    discord.Embed(title="Harbinger War Table", color=0x8B0000),
                    board=True, bot_name="Scaramouche",
                    world=s.get("world_type") or "horror",
                    defeated=s.get("bosses_beaten", ()), total=s.get("total_points", 0),
                )
                return
            if action=='reset':
                if not current:
                    await ctx.send('Nothing to reset. Use !rpg1 to begin.'); return
                revision,state=current
                await ctx.send('Reset this campaign? Your earned medals remain.',view=QuestView(self,uid,guild,ctx.channel.id,revision,state,reset=True))
                return
            revision,state=current or await self.store.open(uid,guild)
            # Reissuing !rpg1 revokes earlier menus without losing the saved choices.
            revision=await self.store.update(uid,guild,revision,state)
            await self.show(ctx.channel,uid,guild,revision,state)
        except (PermissionError,ValueError) as exc:
            await ctx.send(str(exc),allowed_mentions=discord.AllowedMentions.none())

    def install(self):
        for name,aliases,action in [('rpg1',['quest1','harbinger1'],'play'),
            ('rpgstats1',['queststats1'],'stats'),('rpg1reset',['rpgreset1'],'reset'),
            ('gamerank1',['rpgrank1','medals1'],'rank')]:
            def make(action):
                async def callback(ctx):
                    async with WORK.track(ctx.author.id):
                        await self.command(ctx,action)
                return callback
            self.bot.command(name=name,aliases=aliases)(make(action))
        return self
