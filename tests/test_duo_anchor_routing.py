"""Real async and SQLite contracts for exact Discord duo reply ancestry."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from partner_banter_routing import romance_ping_chosen
from memory import Memory
from partner_banter_routing import coherent_partner_reply, resolve_autoplay_anchor, resolve_duo_reply_anchor


def test_false_speaker_attribution_blocks_text_and_real_mention_but_not_romance_teasing():
    assert coherent_partner_reply("deluluqueen, your worst opinion is wrong.", "Wanderer", "deluluqueen") == ""
    assert coherent_partner_reply(
        "<@77>, your worst opinion is wrong.", "Wanderer", "deluluqueen", "<@77>"
    ) == ""
    assert coherent_partner_reply(
        "As usual, deluluqueen, you just said nonsense.", "Wanderer", "deluluqueen"
    ) == ""
    assert coherent_partner_reply(
        "deluluqueen, stop encouraging him.", "Wanderer", "deluluqueen"
    ) == "deluluqueen, stop encouraging him."
    assert coherent_partner_reply(
        "Wanderer, even <@77> can tell you're being ridiculous.",
        "Wanderer", "deluluqueen", "<@77>",
    ) == "Wanderer, even <@77> can tell you're being ridiculous."


def test_async_resolver_always_fetches_a_even_when_b_is_newer():
    async def run():
        a = NS(id=101, author=NS(id=999), content="A")
        b = NS(id=102, author=NS(id=999), content="B")
        channel = NS(fetch_message=AsyncMock(return_value=a))
        selected = await resolve_duo_reply_anchor(
            channel, a.id, fallback=b, partner_bot_id=999,
        )
        assert selected is a
        channel.fetch_message.assert_awaited_once_with(101)
        # A deliberately mismatched author must not become a reply source.
        channel.fetch_message.reset_mock()
        channel.fetch_message.return_value = NS(id=101, author=NS(id=123))
        assert await resolve_duo_reply_anchor(channel, 101, b, partner_bot_id=999) is None
    asyncio.run(run())


def test_deleted_anchor_fails_closed_and_legacy_fallback_remains_available():
    async def run():
        old = NS(id=200)
        channel = NS(fetch_message=AsyncMock())
        assert await resolve_duo_reply_anchor(channel, 0, old) is old
        response = NS(status=404, reason="Not Found")
        channel.fetch_message.side_effect = discord.NotFound(response, "Message not found")
        assert await resolve_duo_reply_anchor(channel, 101, old) is None
    asyncio.run(run())


def test_autoplay_requires_the_saved_partner_message_or_initial_human_handoff():
    async def run():
        source = NS(id=101, author=NS(id=999))
        participant = NS(id=77, author=NS(id=77))
        channel = NS(id=20, fetch_message=AsyncMock(return_value=source))
        memory = NS(get_duo_reply_anchor=AsyncMock(return_value=101))
        session = {"mode": "trial", "last_speaker": "wanderer"}
        assert await resolve_autoplay_anchor(
            channel, session, "scaramouche", "wanderer", 999, participant, memory,
        ) is source
        channel.fetch_message.assert_awaited_once_with(101)

        memory.get_duo_reply_anchor.return_value = None
        channel.fetch_message.reset_mock()
        assert await resolve_autoplay_anchor(
            channel, session, "scaramouche", "wanderer", 999, participant, memory,
        ) is None
        channel.fetch_message.assert_not_awaited()

        session["last_speaker"] = ""
        assert await resolve_autoplay_anchor(
            channel, session, "scaramouche", "wanderer", 999, participant, memory,
        ) is participant
        session["mode"] = "interview"
        assert await resolve_autoplay_anchor(
            channel, session, "scaramouche", "wanderer", 999, participant, memory,
        ) is participant
    asyncio.run(run())


def test_persisted_anchor_is_first_writer_wins_until_next_duo_turn(tmp_path):
    async def run():
        mem = Memory("wanderer" if "Wanderer" in __file__ else "scaramouche")
        mem.db_path = str(tmp_path / "bot.db")
        mem.shared_db_path = str(tmp_path / "shared.db")
        await mem.init()
        await mem.set_duo_session(
            20, "trial", "test", "scaramouche",
            awaiting_bot="wanderer", autoplay_turns=3,
        )
        assert await mem.record_duo_reply_anchor(20, "wanderer", 101, 999)
        assert not await mem.record_duo_reply_anchor(20, "wanderer", 102, 999)
        assert await mem.get_duo_reply_anchor(20, "wanderer") == 101
        # Concurrent channels can never supply another channel's anchor.
        await mem.set_duo_session(
            21, "trial", "separate", "scaramouche",
            awaiting_bot="wanderer", autoplay_turns=2,
        )
        assert await mem.record_duo_reply_anchor(21, "wanderer", 555, 999)
        assert await mem.get_duo_reply_anchor(21, "wanderer") == 555
        assert await mem.get_duo_reply_anchor(20, "wanderer") == 101
        # Successful send carries its exact ID to the next scheduled bot.
        await mem.bump_duo_session(
            20, "wanderer", partner_bot="scaramouche",
            reply_source_message_id=201, reply_source_author_id=888,
        )
        assert await mem.get_duo_reply_anchor(20, "scaramouche") == 201
        assert await mem.get_duo_reply_anchor(20, "wanderer") is None
        # Resetting a session clears old ancestry even for the same bot.
        await mem.set_duo_session(
            20, "trial", "new", "scaramouche",
            awaiting_bot="wanderer", autoplay_turns=2,
        )
        assert await mem.get_duo_reply_anchor(20, "wanderer") is None
        await mem.clear_duo_session(21)
        assert await mem.get_duo_reply_anchor(21, "wanderer") is None
    asyncio.run(run())


def test_original_romance_probability_boundaries():
    assert romance_ping_chosen(0.0)
    assert romance_ping_chosen(0.449999)
    assert not romance_ping_chosen(0.45)
    assert not romance_ping_chosen(0.450001)
    assert not romance_ping_chosen(1.0)
