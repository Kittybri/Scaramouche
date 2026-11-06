"""Regressions for the November audit: reply clean-up, mentions, delivery, privacy, safety and uploads."""

import asyncio
from functools import wraps
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest

from narration import chunk_text, limit_user_mentions, neutralize_mass_mentions, strip_narration


def async_test(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    import bot

    monkeypatch.setattr(bot.bot._connection, "user", NS(id=999, bot=True), raising=False)
    return bot


@pytest.fixture
def memory(tmp_path):
    import memory as memory_module

    store = memory_module.Memory("scaramouche", db_path=str(tmp_path / "scaramouche.db"),
                                 shared_db_path=str(tmp_path / "shared.db"))
    asyncio.run(store.init())
    return store


# ── reply clean-up ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "The answer is **42**, obviously.",
    "I do *not* care about that",
    "You are *never* going to win",
    "Compute f(x) = (x+1)^2 then check (see page 4).",
    "They said the library closes at 5.",
    "Which Archon? [ANSWER: Ei] [SOURCE: Inazuma]",
    "It was released in 2020 [1] and updated later [2].",
    "Sources:\n[1] Boiling - <https://en.wikipedia.org/wiki/Boiling_(disambiguation)>",
    "* item one\n* item two\n2 * 3 * 4 = 24",
])
def test_ordinary_content_survives_narration_stripping(text):
    assert strip_narration(text) == text


def test_stage_directions_still_go():
    assert strip_narration("*smirks* Pathetic. (sighs heavily) Try again. [he rolls his eyes] Fine.") == "Pathetic. Try again. Fine."
    assert strip_narration("Scaramouche muttered, Tch. How quaint.") == "Tch. How quaint."
    assert strip_narration("[voice message] Spare me.") == "Spare me."
    assert strip_narration("*sigh*") == "sigh"


def test_mass_and_raw_mentions_never_survive():
    out = strip_narration("@everyone and @here, ask <@123456789012345678> or <@&55>. @herewhat")
    for token in ("@everyone", "@here", "<@123456789012345678>", "<@&55>"):
        assert token not in out
    assert "<@&" not in neutralize_mass_mentions("<@&<@&1>1>").replace("<@& 1>", "")
    assert neutralize_mass_mentions("@@everyone") == "everyone"
    assert limit_user_mentions("<@1> <@2> <@3> <@4>", 2) == "<@1> <@2>"


def test_chunking_keeps_every_word_under_the_limit():
    text = "A fairly ordinary sentence about nothing at all. " * 120
    pieces = chunk_text(text, 1900)
    assert len(pieces) > 1 and all(len(p) <= 1900 for p in pieces)
    assert " ".join(pieces).split() == text.split()


def test_cleanup_is_cheap_on_hostile_text():
    import time

    started = time.perf_counter()
    for text in ("*a* " * 30000, "(" * 100000, "* a" * 40000, "@" * 200000, "<@&" * 50000):
        strip_narration(text)
    assert time.perf_counter() - started < 3


# ── delivery ─────────────────────────────────────────────────────────────────
def test_bot_never_pings_everyone_or_roles_by_default(runtime):
    allowed = runtime.bot.allowed_mentions
    assert allowed is not None and not allowed.everyone and not allowed.roles and allowed.users


def _delivery_message(runtime, members=()):
    replies = []

    async def reply(content=None, **kwargs):
        if content and len(content) > 2000:
            raise discord.HTTPException(NS(status=400, reason="Bad Request", headers={}), "Must be 2000 or fewer in length.")
        replies.append(content)
        return NS(id=len(replies))

    guild = NS(id=30, get_member=lambda member_id: None, members=list(members))
    channel = NS(id=20, send=AsyncMock())
    message = NS(id=9, content="explain it", author=NS(id=7, display_name="User", mention="<@7>"), guild=guild,
                 channel=channel, reply=reply, attachments=[], reference=None, add_reaction=AsyncMock())
    return message, replies


def _patch_delivery(runtime, monkeypatch):
    for name in ("_record_assistant_delivery", "_record_delivered_reply", "_run_home_roommate_side_effect", "maybe_react"):
        monkeypatch.setattr(runtime, name, AsyncMock())
    monkeypatch.setattr(runtime, "troll", NS(after_reply=AsyncMock()))
    monkeypatch.setattr(runtime, "FISH_AUDIO_API_KEY", "")
    monkeypatch.setattr(runtime.random, "random", lambda: 0.99)


def _prepared():
    from message_pipeline import PreparedMessage

    return PreparedMessage(user={}, user_id=7, channel_id=20, guild_id=30, is_dm=False, is_owner=False, romance=False,
                           content="explain it", previous_last_active=0.0, reference_message=None)


@async_test
async def test_long_replies_are_split_instead_of_lost(runtime, monkeypatch):
    from interaction_policy import classify

    _patch_delivery(runtime, monkeypatch)
    message, replies = _delivery_message(runtime)
    long_reply = "A thorough explanation of the topic. " * 120
    assert await runtime._deliver_normal_reply(message, classify("explain it"), _prepared(), long_reply)
    pieces = replies + [call.args[0] for call in message.channel.send.await_args_list]
    assert len(pieces) >= 2 and all(len(p) <= 2000 for p in pieces)
    assert " ".join(pieces).split() == long_reply.split()


@async_test
async def test_mentions_survive_but_mass_pings_do_not(runtime, monkeypatch):
    from interaction_policy import classify

    _patch_delivery(runtime, monkeypatch)
    alice = NS(id=1, bot=False, display_name="Alice", name="alice", mention="<@1>")
    message, replies = _delivery_message(runtime, [alice])
    await runtime._deliver_normal_reply(message, classify("explain it"), _prepared(),
                                        "@everyone listen. @Alice, you are *not* forgiven.")
    sent = replies[0]
    assert "@everyone" not in sent
    assert "<@1>" in sent  # resolved after stripping, so it is no longer deleted again
    assert "*not*" in sent


@async_test
async def test_trivia_keeps_its_answer_tag(runtime, monkeypatch, memory):
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime, "_qai_blocking", lambda prompt, max_tokens=200: "Which Archon rules Inazuma? [ANSWER: Ei] [SOURCE: Archon Quest]")
    monkeypatch.setattr(runtime, "_recent_reply_samples", AsyncMock(return_value=[]))
    monkeypatch.setattr(runtime, "_apply_phrase_policy", AsyncMock(side_effect=lambda reply, recent, **kw: reply))
    sent = []
    ctx = NS(author=NS(id=7, display_name="User"), channel=NS(id=20), guild=NS(id=30),
             reply=AsyncMock(side_effect=lambda text, **kw: sent.append(text)))
    await runtime.trivia_cmd.callback(ctx)
    trivia = await memory.get_active_trivia(20)
    assert trivia and trivia["answer"] == "Ei"  # the old stripper deleted [ANSWER: ...] before it was read
    assert "ANSWER" not in sent[0]


# ── privacy ──────────────────────────────────────────────────────────────────
async def _seed_victim(memory, victim=222):
    await memory.upsert_user(victim, "victim#1", "Victim")
    await memory.track_channel(500, 50)   # this server
    await memory.track_channel(600, 60)   # another server
    await memory.add_message(victim, 500, "user", "public line said in this server")
    await memory.add_message(victim, 600, "user", "line said in a different server")
    await memory.add_message(victim, victim, "user", "PRIVATE DM: I failed my exams and lied about it")  # DM key = user id


@async_test
async def test_recent_messages_can_be_scoped_to_a_server(memory):
    await _seed_victim(memory)
    assert "PRIVATE DM: I failed my exams and lied about it" in await memory.get_recent_messages(222, 10)
    assert await memory.get_recent_messages(222, 10, guild_id=50) == ["public line said in this server"]
    assert set(await memory.get_recent_messages(222, 10, public_only=True)) == {
        "public line said in this server", "line said in a different server",
    }


@async_test
async def test_profiling_commands_never_read_dms_or_other_servers(runtime, monkeypatch, memory):
    await _seed_victim(memory)
    monkeypatch.setattr(runtime, "mem", memory)
    prompts = []

    async def fake_qai(prompt, max_tokens=200):
        prompts.append(prompt)
        return "A report."

    monkeypatch.setattr(runtime, "qai", fake_qai)
    author = NS(id=111, display_name="Asker", mention="<@111>", name="asker")
    victim = NS(id=222, display_name="Victim", mention="<@222>", name="victim")
    ctx = NS(author=author, guild=NS(id=50), channel=NS(id=500), reply=AsyncMock(), send=AsyncMock(), message=NS(id=1))
    for name in ("blackmail", "stalk", "judge", "possess", "duel"):
        prompts.clear()
        await runtime.bot.get_command(name).callback(ctx, victim)
        seen = " ".join(prompts)
        assert "PRIVATE DM" not in seen, name
        assert "different server" not in seen, name
        if name != "duel":
            assert "public line said in this server" in seen, name


@async_test
async def test_memories_in_a_server_are_sent_privately(runtime, monkeypatch, memory):
    await memory.upsert_user(7, "user#1", "User")
    await memory.add_memory_event(7, "confession", "something said in a DM", 5)
    monkeypatch.setattr(runtime, "mem", memory)
    author = NS(id=7, display_name="User", mention="<@7>", name="user", send=AsyncMock())
    ctx = NS(author=author, guild=NS(id=50), channel=NS(id=500), reply=AsyncMock(), send=AsyncMock())
    await runtime.memories_cmd.callback(ctx)
    assert author.send.await_count == 1 and "something said in a DM" in author.send.await_args.args[0]
    assert all("something said in a DM" not in str(call.args) for call in ctx.reply.await_args_list)


# ── safety ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "I'm suicidal", "thinking about self-harm", "I'm killing myself tonight", "I want to unalive myself", "i wanna kms",
    "I want to end my life", "I don't want to live anymore", "nobody would miss me if I was gone", "I took too many pills",
    "I want to die", "I'm going to kill myself", "I can't go on like this", "su1c1de", "k1ll myself", "s u i c i d e",
    "i want to killmyself", "kill\u200bmyself", "ki\u200bll myself",
])
def test_crisis_phrasings_are_recognised(text):
    from awareness_features import classify_safety

    assert classify_safety(text).crisis


@pytest.mark.parametrize("text", [
    "this poison build is so broken lol", "can't go on vacation this year", "that boss knocked me unconscious lol",
    "I'm not okay with pineapple pizza", "I walked 5 kms", "suicide squad was fine", "I cut myself shaving",
    "Scaramouche is annoyingly dramatic", "I hurt myself at the gym",
])
def test_everyday_text_is_not_treated_as_an_emergency(text):
    from awareness_features import classify_safety

    assert not classify_safety(text).protective


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _guild_message(runtime, mid, content, *, mentions=()):
    author = NS(id=7, bot=False, name="user", display_name="User", mention="<@7>")
    channel = NS(id=20, typing=lambda: Typing(), fetch_message=AsyncMock(), send=AsyncMock())
    guild = NS(id=30, get_member=lambda member_id: None)
    return NS(id=mid, content=content, author=author, channel=channel, guild=guild, mentions=list(mentions),
              attachments=[], embeds=[], reference=None, reply=AsyncMock(), add_reaction=AsyncMock(),
              _state=runtime.bot._connection)


@async_test
async def test_no_unsolicited_replies_over_keywords_and_opt_out_is_respected(runtime, monkeypatch):
    monkeypatch.setattr(runtime.bot, "get_context", AsyncMock(return_value=NS(command=None, prefix=None, invoked_with=None, valid=False)))
    monkeypatch.setattr(runtime, "PRIVACY_DELETION", NS(is_pending=AsyncMock(return_value=False)))
    monkeypatch.setattr(runtime, "VOICE_COMMANDS", NS(maybe_leave=AsyncMock(return_value=False), maybe_join=AsyncMock(return_value=False)))
    monkeypatch.setattr(runtime, "HOME", NS(natural_proposal=AsyncMock(return_value=None)))
    priority = AsyncMock()
    routed = AsyncMock()
    monkeypatch.setattr(runtime, "_priority_reply", priority)
    monkeypatch.setattr(runtime, "_on_message_routed", routed)
    monkeypatch.setattr(runtime, "_interaction_session", AsyncMock(return_value=("", None)))
    quiet = {"now": False}  # pinned: the default quiet hours (23:00-08:00) would otherwise depend on when this runs
    monkeypatch.setattr(runtime, "_is_in_quiet_hours", lambda user: quiet["now"])
    runtime._UNSOLICITED_PROTECTIVE.clear()

    def memory_for(user):
        return NS(upsert_user=AsyncMock(return_value=0.0), track_channel=AsyncMock(), get_user=AsyncMock(return_value=user),
                  is_muted=AsyncMock(return_value=False), add_message=AsyncMock())

    async def dispatch(mid, text, user, **kw):
        monkeypatch.setattr(runtime, "mem", memory_for(user))
        runtime._processed_msgs.discard(mid)
        await runtime._dispatch_message(_guild_message(runtime, mid, text, **kw))

    await dispatch(8001, "this poison build is so broken lol", {"proactive": True})
    await dispatch(8002, "I need tax advice for my side hustle", {"proactive": True})
    await dispatch(8003, "awful day at work, whatever", {"proactive": False})
    assert priority.await_count == 0  # keywords alone never make the bot answer a message that was not for it
    await dispatch(8004, "honestly i just want to die", {"proactive": False})
    assert priority.await_count == 0  # an opted-out person is not answered unprompted either
    quiet["now"] = True
    await dispatch(8009, "honestly i just want to die", {"proactive": True})
    assert priority.await_count == 0  # nor during their quiet hours
    quiet["now"] = False
    await dispatch(8005, "honestly i just want to die", {"proactive": True})
    await dispatch(8006, "i really want to die", {"proactive": True})
    assert priority.await_count == 1  # a real crisis signal gets one protective reply, then a cooldown
    bot_user = runtime.bot._connection.user
    await dispatch(8007, "I need tax advice", {"proactive": True}, mentions=[bot_user])
    assert priority.await_count == 2  # aimed at the bot: answered carefully as before


# ── !rank ────────────────────────────────────────────────────────────────────
@async_test
async def test_rank_lists_only_this_servers_people_by_this_servers_messages(runtime, monkeypatch, memory):
    await memory.track_channel(500, 50)
    await memory.track_channel(600, 60)
    for uid, name in ((1, "Alice"), (2, "Bob"), (77, "Stranger Elsewhere")):
        await memory.upsert_user(uid, name.lower(), name)
    for _ in range(3):
        await memory.add_message(1, 500, "user", "alice here")
    await memory.add_message(2, 500, "user", "bob here")
    for _ in range(9):
        await memory.add_message(1, 1, "user", "a DM line")          # DMs do not count
        await memory.add_message(77, 600, "user", "other server")   # neither do other servers
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime, "qai", AsyncMock(return_value="Mediocre."))
    members = {1: NS(id=1, bot=False, display_name="Alice"), 2: NS(id=2, bot=False, display_name="Bob")}
    sent = []
    ctx = NS(author=NS(id=1), guild=NS(id=50, get_member=members.get), channel=NS(id=500),
             send=AsyncMock(side_effect=lambda *a, **k: sent.append(k.get("embed"))), reply=AsyncMock())
    await runtime.rank_cmd.callback(ctx)
    description = sent[0].description
    assert "**Alice** — 3 messages" in description and "**Bob** — 1 messages" in description
    assert "Stranger" not in description


# ── uploads ──────────────────────────────────────────────────────────────────
@async_test
async def test_tedtalk_refuses_a_zip_bomb_without_parsing_it(runtime, monkeypatch):
    import time
    from test_doc_extract import make_docx, zip_with_part

    bomb = zip_with_part("word/document.xml", 100_000_000, make_docx())
    attachment = NS(filename="notes.docx", content_type="", size=len(bomb), read=AsyncMock(return_value=bomb))
    sent = []
    ctx = NS(author=NS(id=7, display_name="User"), guild=None, channel=NS(id=20), message=NS(id=1),
             send=AsyncMock(side_effect=lambda text=None, **kw: sent.append(text)), typing=lambda: Typing())
    started = time.perf_counter()
    await runtime._do_tedtalk(ctx, attachment, None, 1)
    assert time.perf_counter() - started < 5
    assert any("far more data" in (text or "") for text in sent)


# ── integrations ─────────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status, self.payload, self.headers = status, payload or {}, headers or {}

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, script, log):
        self.script, self.log = script, log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        self.log.append(method)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _client(script, log):
    from integrations.http import AsyncJSONClient

    async def no_sleep(_delay):
        return None

    return AsyncJSONClient(session_factory=lambda **kw: _FakeSession(script, log), sleep=no_sleep)


@async_test
async def test_a_post_that_may_have_happened_is_not_sent_twice():
    from integrations.http import IntegrationError

    for failure in (_FakeResponse(503), asyncio.TimeoutError()):
        log = []
        client = _client([failure, _FakeResponse(200, {"id": "created twice"})], log)
        with pytest.raises(IntegrationError):
            await client.request("POST", "https://example.test/events", json={"summary": "x"})
        assert log == ["POST"]  # a duplicate calendar event / task / issue is worse than an error
    log = []
    client = _client([_FakeResponse(429, headers={"Retry-After": "0"}), _FakeResponse(200, {"ok": True})], log)
    assert await client.request("POST", "https://example.test/events") == {"ok": True}
    assert log == ["POST", "POST"]  # 429: nothing was done, so retrying is safe
    log = []
    client = _client([_FakeResponse(503), _FakeResponse(200, {"ok": True})], log)
    assert await client.request("GET", "https://example.test/events") == {"ok": True}
    assert log == ["GET", "GET"]
