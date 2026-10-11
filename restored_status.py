"""Recovered read-only provider status; no provider requests or credential output."""
import discord
from discord.ext import commands

class ProviderStatus:
    def __init__(self, bot, name, client, owner_id, monitor=None):
        self.bot, self.name, self.client = bot, name.lower(), client
        self.owner_id, self.monitor = owner_id, monitor

    async def state(self):
        """Read-only diagnostic; never call the text provider or expose errors."""
        try:
            clients = getattr(self.client, "_clients", ())
            if not clients:
                return "not_configured"
            exhausted = getattr(self.client, "is_exhausted", None)
            if exhausted is not None and exhausted():
                return "cooldown"
            if self.monitor:
                snapshot = await self.monitor.collect()
                status = getattr(snapshot, "provider_status", "")
                if status in {"recovering", "degraded"}:
                    return status
            return "configured"  # Configuration/observations, NOT a live API probe.
        except Exception:
            # Provider/monitor failures must not crash status commands or
            # publish exception text, credentials, prompts or raw responses.
            return "diagnostics_unavailable"

    async def command(self, ctx):
        state = await self.state()
        if self.owner_id and ctx.author.id == self.owner_id:
            text = f"{self.name.title()} provider state: {state}. Configuration/recent observations only; no live API probe."
            try:
                await ctx.author.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.Forbidden:
                await ctx.reply("Open a DM with me for the private diagnostic.", mention_author=False)
            return
        if self.name == "scaramouche":
            text = ("I'm here. Obviously." if state == "configured" else
                    "I'm here. Some answers need a moment. Do try to be patient.")
        else:
            text = ("I'm here." if state == "configured" else
                    "I'm listening. Some answers may take a little longer.")
        await ctx.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    def install(self):
        aliases = ["aistatus", "scarastatus" if self.name == "scaramouche" else "wanderstatus"]
        async def status(ctx):
            await self.command(ctx)
        self.bot.add_command(commands.Command(status, name="status", aliases=aliases,
                                              help="Check provider availability without making an API request."))
        return self
