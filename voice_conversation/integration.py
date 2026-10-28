"""Thin integration with the existing character engine, memory and Fish VoiceState."""

from __future__ import annotations

import asyncio
import contextlib
import json
import io
import shutil
import random
import re
import time

import discord

from awareness_features import classify_safety, protective_prompt
from .receive import ReceiveBackend
from .session import Playback, Session
from .speech import GroqSTT, Limits


class VoiceConversation:
    def __init__(
        self, bot, mem, name, respond, tts, stt_key, owner_id=0, on_spoken=None
    ):
        self.bot, self.mem, self.name = bot, mem, name
        self.respond, self.tts, self.stt_key = respond, tts, stt_key
        self.owner_id = owner_id
        self.on_spoken = on_spoken
        self.sessions = {}
        self.features = None
        self.lock = None
        self.greeting_tasks = {}
        self.arrivals_seen = {}

    def install(self):
        self.bot.add_listener(self.voice_state, "on_voice_state_update")
        self.bot.add_listener(self.channel_deleted, "on_guild_channel_delete")
        original = self.bot.close

        async def close():
            await self.close()
            await original()

        self.bot.close = close

    async def close(self):
        if self.features:
            await self.features.close()
        for gid in list(self.sessions):
            await self.leave(gid)

    async def leave(self, gid, *, announce=True):
        tasks = [task for (guild_id, _), task in self.greeting_tasks.items() if guild_id == gid]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.arrivals_seen.pop(gid, None)
        session = self.sessions.pop(gid, None)
        if session:
            channel = session.backend.vc.channel
            await session.stop()
            with contextlib.suppress(Exception):
                await session.backend.vc.disconnect(force=True)
            if announce:
                with contextlib.suppress(Exception):
                    await channel.send(
                        "Live voice ended. Listening and temporary audio buffers cleared.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )

    async def spoken_leave(self, gid, session, uid):
        """Finish one in-character farewell before disconnecting the voice client."""
        if self.sessions.get(gid) is not session:
            return
        lines = (
            (
                "Tch. I've heard enough. Try surviving without me.",
                "Fine. I'll leave you to your charming company.",
                "I'm going. Don't make a scene over it.",
            )
            if self.name.lower() == "scaramouche"
            else (
                "I'm leaving. Try not to make a spectacle of it.",
                "Fine. You can have the room to yourself.",
                "I'll go. Don't mistake that for obedience.",
            )
        )
        try:
            await session.cancel_response()
            if session.active and uid in session.participants:
                if await session.submit(uid, "", reply=random.choice(lines)):
                    await asyncio.gather(session.response_task, return_exceptions=True)
        finally:
            if self.sessions.get(gid) is session:
                await self.leave(gid, announce=False)

    async def voice_state(self, member, before, after):
        if self.features:
            # A social-feature DB failure must never prevent a participant's
            # departure or the foundation disconnecting a moved bot.
            with contextlib.suppress(Exception):
                await self.features.voice_state(member, before, after)
        session = self.sessions.get(member.guild.id)
        if not session:
            return
        if member.id == self.bot.user.id:
            if (
                getattr(after.channel, "id", None) != session.channel_id
                or after.deaf
                or after.self_deaf
            ):
                await self.leave(member.guild.id)
            return
        if getattr(after.channel, "id", None) != session.channel_id:
            task = self.greeting_tasks.pop((member.guild.id, member.id), None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            session.consent(member.id, False)
        elif (
            not member.bot
            and getattr(before.channel, "id", None) != session.channel_id
        ):
            if await self.enroll(session, member, announce=True):
                self.queue_greeting(session, member)

    async def enroll(self, session, member, *, announce=False):
        if member.bot or not session.active:
            return False
        if getattr(getattr(member.voice, "channel", None), "id", None) != session.channel_id:
            return False
        if member.id in session.participants:
            return False
        epoch = getattr(session, "epochs", {}).get(member.id, 0)
        if len(session.participants) >= session.limits.participants:
            return False
        # A leave/stop still invalidates enrollment, even though no notice or
        # per-user voice preference is required for live channel listening.
        if (
            not session.active
            or getattr(session, "epochs", {}).get(member.id, 0) != epoch
            or getattr(getattr(member.voice, "channel", None), "id", None) != session.channel_id
            or len(session.participants) >= session.limits.participants
        ):
            return False
        session.consent(member.id, True)
        return True

    def queue_greeting(self, session, member):
        key = (member.guild.id, member.id)
        if key in self.greeting_tasks:
            return
        seen = self.arrivals_seen.setdefault(member.guild.id, {})
        returning = seen.get(member.id, 0) > 0
        seen[member.id] = min(255, seen.get(member.id, 0) + 1)
        if len(seen) > 128:
            seen.pop(next(iter(seen)))

        async def greet():
            try:
                deadline = time.monotonic() + 20
                while session.active and member.id in session.participants:
                    if not session.busy() and not session.segmenter.users and session.jobs.empty():
                        name = re.sub(r"[@#\x00-\x1f]", "", member.display_name)[:60].strip() or "you"
                        if self.name.lower() == "scaramouche":
                            lines = (
                                [
                                    f"Look who came back. {name}, did you miss me already?",
                                    f"{name} again? You just left. What is it this time?",
                                    f"So {name} returned. Try making this entrance less disappointing.",
                                ]
                                if returning
                                else [
                                    f"Look who arrived. {name}, what do you want?",
                                    f"{name} has arrived. Try not to make me regret noticing.",
                                    f"Oh, {name}. Make your entrance worth the interruption.",
                                ]
                            )
                        else:
                            lines = (
                                [
                                    f"{name} came back. Changed your mind?",
                                    f"Look who returned. What do you want now, {name}?",
                                    f"{name} again. I suppose leaving didn't suit you.",
                                ]
                                if returning
                                else [
                                    f"Look who arrived. {name}, what brings you here?",
                                    f"{name} is here. Go on, say what you came to say.",
                                    f"Oh, {name}. I noticed.",
                                ]
                            )
                        if await session.submit(member.id, "", reply=random.choice(lines)):
                            session.focus, session.focus_until = member.id, time.monotonic() + 90
                        return
                    if time.monotonic() >= deadline:
                        return
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception:
                session.decision("arrival_greeting_failed", member.id)

        task = asyncio.create_task(greet())
        self.greeting_tasks[key] = task
        task.add_done_callback(lambda done: self.greeting_tasks.pop(key, None))

    async def channel_deleted(self, channel):
        session = self.sessions.get(channel.guild.id)
        if session and session.channel_id == channel.id:
            await self.leave(channel.guild.id)

    async def send(self, ctx, text):
        await ctx.reply(
            text, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
        )

    async def status(self, ctx):
        session = self.sessions.get(ctx.guild.id) if ctx.guild else None
        if not session:
            await self.send(
                ctx,
                "Live voice: inactive. Join a server VC, then ask me to join or use `!voice start`. "
                "Listening is automatic for humans in the VC.",
            )
            return
        info = session.status()
        await self.send(
            ctx,
            f"Live voice: {info['state']}; listening={info['listening']}; "
            f"receive speech verified={info['receive_proven']} (connection alone is not proof). "
            f"Targeting={info['mode']}; interruption={info['interrupt']}; "
            f"participants={info['participants']}; queued utterances={info['pending_audio']}. "
            + "Automatic listening; end session: `!voice stop`.",
        )

    async def command(self, ctx, message):
        value = (message or "").strip().lower()
        if value in {"diag", "diagnostic", "diagontics", "diagonstics"}:
            value = "diagnostics"
        parts = value.split()
        # Both character bots intentionally expose the same !voice command.  An
        # unqualified start/stop keeps the existing shared behavior, while an
        # explicit character suffix lets operators isolate one bot without
        # taking the partner service offline.
        if len(parts) == 2 and parts[0] in {"start", "join", "stop", "leave"}:
            aliases = {
                "scaramouche": {"scaramouche", "scara", "balladeer"},
                "wanderer": {"wanderer", "hatguy", "hat-guy"},
            }
            target = parts[1]
            known_targets = set().union(*aliases.values())
            if target in known_targets:
                if target not in aliases.get(self.name.lower(), {self.name.lower()}):
                    return True
                parts = parts[:1]
                value = parts[0]
        # Preserve the old emergency stop gesture, but it now ends the whole
        # live session instead of changing a persistent per-person VC setting.
        if value == "off" and ctx.guild and self.sessions.get(ctx.guild.id):
            parts = ["stop"]
            value = "stop"
        if value == "status":
            # Add diagnostics, then allow the legacy voice-note status to run.
            await self.status(ctx)
            return False
        if not parts or parts[0] not in {
            "start",
            "join",
            "stop",
            "leave",
            "mode",
            "interrupt",
            "diagnostics",
            "session",
        }:
            return False
        if value in {"session", "session status"}:
            await self.status(ctx)
            return True
        if not ctx.guild:
            await self.send(ctx, "Live voice controls require a server voice channel.")
            return True
        # Serialize session controls; no duplicate sessions on concurrent joins.
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            await self.handle(ctx, parts)
        return True

    async def handle(self, ctx, parts):
        action = parts[0]
        session = self.sessions.get(ctx.guild.id)
        channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        admin = bool(
            ctx.author.id == self.owner_id or ctx.author.guild_permissions.manage_guild
        )
        if action in {"stop", "leave"}:
            in_session_channel = bool(channel and session and channel.id == session.channel_id)
            if not session:
                await self.send(ctx, "I'm not in a voice channel right now.")
            elif admin or ctx.author.id == session.initiator or in_session_channel:
                await self.leave(ctx.guild.id)
                await self.send(
                    ctx,
                    "Live voice ended; ephemeral audio and temporary speaker state cleared.",
                )
            else:
                await self.send(
                    ctx,
                    "Anyone in my current VC, the session starter, or a server manager can ask me to leave.",
                )
            return
        if action == "diagnostics":
            if ctx.author.id != self.owner_id:
                await self.send(ctx, "Detailed voice diagnostics are owner-only.")
                return
            result = session.status() if session else {"state": "IDLE"}
            if self.features:
                result["advanced_features"] = {
                    "maintenance_error_seen": self.features.reported_error,
                    "pending_duo_turns": len(self.features.duo_pending),
                    "mockingbird_mode": getattr(
                        getattr(session, "feature_router", None), "mock_mode", "OFF"
                    ),
                }
            if session:
                result["events"] = list(session.events)[-16:]
                from .personality import priority_status

                result["priority_speaker"] = priority_status(
                    getattr(
                        getattr(getattr(session, "backend", None), "vc", None),
                        "channel",
                        None,
                    ),
                    ctx.guild.me,
                )
            with contextlib.suppress(discord.HTTPException):
                await ctx.author.send(
                    "Sanitized voice diagnostics; no transcript or raw audio.",
                    file=discord.File(
                        io.BytesIO(json.dumps(result, indent=2).encode()),
                        filename="voice-health.json",
                    ),
                )
            return
        if not channel:
            await self.send(
                ctx,
                "Join a voice channel in this server first, then ask me to join you.",
            )
            return
        if action in {"start", "join"}:
            if session or ctx.guild.voice_client or len(self.sessions) >= 2:
                await self.send(
                    ctx,
                    "A voice connection/session already exists, or the session limit is reached. End it before starting another.",
                )
                return
            permissions = channel.permissions_for(ctx.guild.me)
            if (
                not permissions.connect
                or not permissions.speak
                or not permissions.view_channel
            ):
                await self.send(
                    ctx, "I need View Channel, Connect and Speak in that VC."
                )
                return
            if not self.stt_key or not shutil.which("ffmpeg"):
                await self.send(
                    ctx,
                    "Live voice needs Groq STT credentials and FFmpeg configured on the host.",
                )
                return
            backend = ReceiveBackend(
                lambda: session.participants if session else frozenset()
            )
            vc = None
            try:
                client_class = backend.client_class()
                vc = await channel.connect(cls=client_class, self_deaf=False)

                async def respond(uid, text, extra):
                    member = ctx.guild.get_member(uid)
                    if not member or uid not in session.participants:
                        return ""
                    await self.mem.upsert_user(uid, str(member), member.display_name)
                    user = await self.mem.get_user(uid) or {}
                    safety = protective_prompt(self.name, classify_safety(text))
                    return await self.respond(
                        uid,
                        channel.id,
                        text,
                        user,
                        member.display_name,
                        member.mention,
                        extra_context=extra + "\n" + safety,
                        is_owner=uid == self.owner_id,
                        channel_obj=channel,
                        is_dm=False,
                        defer_delivery=True,
                    )

                async def synthesize(uid, text):
                    user = await self.mem.get_user(uid) or {}
                    return await self.tts(
                        text, user.get("mood", 0), user, voice_key=uid
                    )

                async def remember(uid, text):
                    await self.mem.add_message(
                        uid, channel.id, "assistant", "[voice spoken] " + text
                    )
                    if self.on_spoken:
                        await self.on_spoken(uid, text)

                async def notify(category):
                    await channel.send(
                        "Live voice needs attention; text and ordinary voice notes remain available. Try `!voice status`, then stop/start after checking setup.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    if self.owner_id:
                        owner = self.bot.get_user(
                            self.owner_id
                        ) or await self.bot.fetch_user(self.owner_id)
                        await owner.send(
                            f"{self.name}: voice diagnostic `{category}` in channel {channel.id}. No transcript/audio attached."
                        )

                session = Session(
                    channel_id=channel.id,
                    initiator=ctx.author.id,
                    name=self.name,
                    backend=backend,
                    stt=GroqSTT(self.stt_key),
                    playback=Playback(vc),
                    respond=respond,
                    synthesize=synthesize,
                    remember=remember,
                    notify=notify,
                    on_spoken_leave=lambda uid: self.spoken_leave(ctx.guild.id, session, uid),
                    limits=Limits.from_env(),
                )
                session.consent(ctx.author.id, True)
                self.sessions[ctx.guild.id] = session
                if self.features:
                    self.features.attach(session, ctx.guild.id)
                await backend.start(vc)
                await session.start()
                for member in getattr(channel, "members", ()):
                    await self.enroll(session, member)
                    if not member.bot and member.id in session.participants:
                        self.arrivals_seen.setdefault(ctx.guild.id, {})[
                            member.id
                        ] = 1
                await self.status(ctx)
            except Exception:
                if session:
                    await self.leave(ctx.guild.id)
                elif vc:
                    await vc.disconnect(force=True)
                await self.send(
                    ctx,
                    "Live voice could not start. Check the pinned receive dependencies, Opus/FFmpeg, DAVE and permissions. Text and voice notes are unaffected.",
                )
            return
        if not session or not session.active or not channel or channel.id != session.channel_id:
            await self.send(
                ctx, "Start a session in your current server VC first: `!voice start`."
            )
            return
        if (
            action == "interrupt"
            and len(parts) == 3
            and parts[1] == "me"
            and parts[2].upper() in {"OFF", "KEYWORD", "NATURAL"}
        ):
            if ctx.author.id in session.participants:
                session.user_interrupt[ctx.author.id] = parts[2].upper()
                await self.send(
                    ctx,
                    "Your interruption preference is "
                    + parts[2].upper()
                    + " for this session.",
                )
            return
        if not (admin or ctx.author.id == session.initiator):
            await self.send(
                ctx, "Session-wide settings require its initiator or a server manager."
            )
            return
        if (
            action == "mode"
            and len(parts) == 2
            and parts[1].upper() in {"DIRECT_ONLY", "CONVERSATION", "ACTIVE_ROOM"}
        ):
            session.mode = parts[1].upper()
        elif (
            action == "interrupt"
            and len(parts) == 2
            and parts[1].upper() in {"OFF", "KEYWORD", "NATURAL"}
        ):
            session.interrupt_mode = parts[1].upper()
        else:
            await self.send(
                ctx,
                "Controls: `start`, `stop`, `mode direct_only/conversation/active_room`, `interrupt off/keyword/natural`, `interrupt me off/keyword/natural`, `status`, `diagnostics`. Prefix each with `!voice `.",
            )
            return
        await self.status(ctx)
