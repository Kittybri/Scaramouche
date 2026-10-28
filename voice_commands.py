"""Discoverable voice controls delegating to the existing session implementation."""
import asyncio
import json
import logging
import re
from types import SimpleNamespace

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from awareness_features import classify_safety
from interaction_policy import credential_disclosure

log = logging.getLogger(__name__)
VOICE_HELP = (
    "Live voice: `!join [scaramouche|wanderer]` (alias `!listen`) or `/join`. "
    "End with `!leave [scaramouche|wanderer]` or `/leave`. "
    "Join an enabled server VC first; choose the bot's app for slash commands. "
    "You can also say 'Scara, join voice chat' or 'Wanderer, join the voice channel' "
    "in text or a short Discord voice note while you are in an enabled VC. "
    "Existing `!voice start`, `!voice stop` and `!voice diagnostics` still work."
)

def join_intent(text, name):
    """Require a short direct request naming exactly this character."""
    text = (text or "").lower().replace("’", "'").strip()
    if len(text) > 180 or len(text.split()) > 24 or text.startswith("!"):
        return False
    self_names = ("scaramouche", "scara", "balladeer") if name.lower() == "scaramouche" else ("wanderer", "hatguy", "hat guy")
    other_names = ("wanderer", "hatguy", "hat guy") if name.lower() == "scaramouche" else ("scaramouche", "scara", "balladeer")
    has_name = any(re.search(r"\b" + re.escape(alias) + r"\b", text) for alias in self_names)
    if not has_name or any(re.search(r"\b" + re.escape(alias) + r"\b", text) for alias in other_names):
        return False
    if re.search(r"\b(?:don't|dont|do not|not|never|stop|why|how|what|who|where|when|whether|does|did|explain|should i|can i|let me|i want to|i'm)\b", text):
        return False
    return bool(
        re.search(r"\b(?:join|hop|come|connect|enter|get into|start)\b.{0,65}\b(?:voice|vc)\b", text)
        or re.search(r"\b(?:voice|vc)\b.{0,15}\b(?:channel|chat|cahat|call)\b", text)
    )


class VoiceCommands:
    def __init__(self, service):
        self.service = service
        self.bot = service.bot
        self.synced = False

    async def prefix(self, ctx, action, target=""):
        target = target.strip().lower()
        if target and target not in {
            "scaramouche", "scara", "balladeer", "wanderer", "hatguy", "hat-guy"
        }:
            await self.service.send(ctx, "Choose scaramouche or wanderer, or omit the name for both bots.")
            return
        await self.service.command(ctx, action + (" " + target if target else ""))

    async def _transcribe_note(self, attachment):
        if not self.service.stt_key or not attachment.is_voice_message():
            return ""
        if not attachment.duration or attachment.duration > 30 or not attachment.size or attachment.size > 2_000_000:
            return ""
        audio = await asyncio.wait_for(attachment.read(), 10)
        if not audio or len(audio) > 2_000_000:
            return ""
        form = aiohttp.FormData()
        form.add_field("file", audio, filename="voice-message.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3-turbo")
        form.add_field("response_format", "json")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as client:
            async with client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + self.service.stt_key}, data=form,
            ) as response:
                if response.status != 200:
                    raise RuntimeError("voice_note_stt_http_failure")
                raw = await response.content.read(8193)
                if len(raw) > 8192:
                    raise RuntimeError("voice_note_stt_response_too_large")
        transcript = json.loads(raw).get("text", "")
        return transcript.strip()[:300] if isinstance(transcript, str) else ""

    async def maybe_join(self, message, ctx):
        """Consume only a direct join request; normal messages keep their old route."""
        if not message.guild or message.author.bot:
            return False
        text = message.content or ""
        note = next((item for item in message.attachments if item.is_voice_message()), None)
        if note and not text.strip():
            if self.service.sessions.get(message.guild.id):
                return False
            channel = getattr(getattr(message.author, "voice", None), "channel", None)
            temporary = self.service.features.games.temporary_channels if self.service.features else set()
            if not channel or channel.id not in self.service.allowed | temporary:
                return False
            prefs = await self.service.mem.get_user_preferences(message.author.id)
            if not prefs.get("voice_enabled", True):
                return False
            try:
                text = await self._transcribe_note(note)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("voice note join transcription unavailable (%s)", type(exc).__name__)
                text = ""
            if not text:
                return False
        if credential_disclosure(text) or classify_safety(text).protective:
            return False
        if not join_intent(text, self.service.name):
            return False
        existing = self.service.sessions.get(message.guild.id)
        await self.service.command(ctx, "join")
        current = self.service.sessions.get(message.guild.id)
        if current and current is not existing and current.active:
            line = (
                "Fine. I'm here. Try to make this worth my time."
                if self.service.name.lower() == "scaramouche"
                else "Alright, I'm here. Go on."
            )
            await message.reply(line, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        return True

    async def slash(self, interaction, action):
        await interaction.response.defer(ephemeral=True)
        async def reply(text, **kwargs):
            kwargs.pop("mention_author", None)
            return await interaction.followup.send(text, ephemeral=True, **kwargs)
        ctx = SimpleNamespace(
            author=interaction.user, guild=interaction.guild,
            channel=interaction.channel, reply=reply,
        )
        try:
            await self.service.command(ctx, action)
        except Exception as exc:
            log.warning("voice control unavailable (%s)", type(exc).__name__)
            await reply("The voice control couldn't complete. Try again shortly.")

    async def ready(self):
        # Wanderer's on_ready already syncs its complete tree. A simultaneous
        # upsert can race that bulk sync and create duplicate Discord entries.
        if self.service.name.lower() == "wanderer":
            return
        if self.synced or not self.bot.application_id:
            return
        try:
            # Never bulk-sync a partial tree: preserve unrelated remote commands.
            for name in ("join", "leave"):
                command = self.bot.tree.get_command(name)
                await self.bot.http.upsert_global_command(
                    self.bot.application_id, command.to_dict(self.bot.tree)
                )
            self.synced = True
        except discord.HTTPException as exc:
            log.warning("voice command registration unavailable (%s)", type(exc).__name__)

    def install(self):
        async def join(ctx, *, target: str = ""):
            await self.prefix(ctx, "join", target)
        async def leave(ctx, *, target: str = ""):
            await self.prefix(ctx, "leave", target)
        self.bot.add_command(commands.Command(
            join, name="join", aliases=["listen"],
            help="Join your enabled voice channel. Optional target: scaramouche or wanderer.",
        ))
        self.bot.add_command(commands.Command(
            leave, name="leave",
            help="End the live voice session (session starter or server manager). Optional bot name.",
        ))
        @self.bot.tree.command(name="join", description="Join your enabled voice channel and start live conversation.")
        @app_commands.guild_only()
        async def join_slash(interaction: discord.Interaction):
            await self.slash(interaction, "join")
        @self.bot.tree.command(name="leave", description="End this bot's live voice session.")
        @app_commands.guild_only()
        async def leave_slash(interaction: discord.Interaction):
            await self.slash(interaction, "leave")
        self.bot.add_listener(self.ready, "on_ready")
        return self

