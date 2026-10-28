"""Discoverable voice controls delegating to the existing session implementation."""
import logging
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)
VOICE_HELP = (
    "Live voice: `!join [scaramouche|wanderer]` (alias `!listen`) or `/join`. "
    "End with `!leave [scaramouche|wanderer]` or `/leave`. "
    "Join an enabled server VC first; choose the bot's app for slash commands. "
    "Existing `!voice start`, `!voice stop` and `!voice diagnostics` still work."
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

