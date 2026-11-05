"""Mock Discord objects and a fake model, used to drive bot.py offline. Nothing here touches the network."""

import os
import types
from unittest.mock import AsyncMock, MagicMock

# bot.py reads its configuration when imported, so set it first (never real credentials).
os.environ.update(
    DISCORD_TOKEN="test-token", GROQ_API_KEY="test-key", OWNER_ID="999", PARTNER_BOT_ID="777", LOG_LEVEL="CRITICAL"
)

import discord  # noqa: E402

BOT_ID = 111
BOT_USER = types.SimpleNamespace(id=BOT_ID, name="Scaramouche", display_name="Scaramouche", mention="<@111>")


class FakeAI:
    """Stands in for llm.RotatingGroq. ``responder(messages, kwargs)`` returns text or an Exception to raise."""

    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda messages, kwargs: "Pathetic. Go on.")

    def call_with_retry(self, *, vision=False, model=None, **kwargs):
        self.calls.append({"vision": vision, **kwargs})
        out = self.responder(kwargs.get("messages"), kwargs)
        if isinstance(out, Exception):
            raise out
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=out))])

    def prompts(self) -> str:
        """Everything the model has been shown, as one string."""
        chunks = []
        for call in self.calls:
            for message in call.get("messages") or []:
                content = message["content"]
                chunks.append(content if isinstance(content, str) else " ".join(p.get("text", "") for p in content if isinstance(p, dict)))
        return "\n".join(chunks)


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _agen(items):
    for item in items:
        yield item


def member(uid, name, *, is_bot=False, perms=None):
    m = MagicMock(spec=discord.Member)
    m.id, m.display_name, m.name, m.global_name, m.bot = uid, name, name.lower().replace(" ", ""), None, is_bot
    m.mention = f"<@{uid}>"
    m.guild_permissions = perms or discord.Permissions.none()
    return m


def dm_user(uid, name):
    u = MagicMock(spec=discord.User)
    u.id, u.display_name, u.name, u.bot, u.mention = uid, name, name.lower(), False, f"<@{uid}>"
    u.send = AsyncMock()
    return u


def guild(gid, members):
    g = MagicMock(spec=discord.Guild)
    g.id, g.members = gid, members
    me = member(BOT_ID, "Scaramouche", is_bot=True)
    g.me = me
    g.get_member = lambda uid: next((m for m in members + [me] if m.id == uid), None)
    return g


def channel(cid, g=None, *, nsfw=False, history=()):
    ch = MagicMock(spec=discord.TextChannel if g else discord.DMChannel)
    ch.id, ch.guild = cid, g
    ch.send = AsyncMock(side_effect=lambda *a, **k: MagicMock())
    ch.is_nsfw = lambda: nsfw
    ch.permissions_for = lambda who: discord.Permissions.all()
    ch.typing = lambda: _Typing()
    ch.history = lambda limit=None: _agen(list(history))
    ch.fetch_message = AsyncMock(return_value=None)
    return ch


def attachment(name="a.png", ctype="image/png", size=1000, url="https://cdn.example/x"):
    a = MagicMock(spec=discord.Attachment)
    a.filename, a.content_type, a.size, a.url = name, ctype, size, url
    return a


_next_id = [1000]


def message(author, ch, content, *, g=None, attachments=(), mentions=(), reference=None):
    _next_id[0] += 1
    m = MagicMock(spec=discord.Message)
    m.id, m.author, m.channel, m.guild, m.content = _next_id[0], author, ch, g, content
    m.attachments, m.mentions, m.reference = list(attachments), list(mentions), reference
    m.reply = AsyncMock(side_effect=lambda *a, **k: MagicMock())
    m.add_reaction = AsyncMock()
    return m


class Ctx:
    """The small part of commands.Context the commands use."""

    def __init__(self, author, ch, g=None, content="", attachments=()):
        self.author, self.channel, self.guild = author, ch, g
        self.message = message(author, ch, content, g=g, attachments=attachments)
        self.sent = []
        self.command = types.SimpleNamespace(reset_cooldown=lambda ctx: None)

    async def reply(self, content=None, **kwargs):
        self.sent.append(("reply", content, kwargs))
        return MagicMock()

    async def send(self, content=None, **kwargs):
        self.sent.append(("send", content, kwargs))
        sent = MagicMock()
        sent.add_reaction = AsyncMock()
        return sent

    def typing(self):
        return _Typing()

    def texts(self):
        """Everything said through this context (including what went to the channel mock)."""
        direct = [c for _, c, _ in self.sent if c]
        via_channel = [call.args[0] for call in getattr(self.channel.send, "await_args_list", []) if call.args]
        return direct + via_channel
