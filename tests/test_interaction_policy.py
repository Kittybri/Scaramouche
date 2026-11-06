"""Deterministic interaction-arbitration and character-precedence regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from interaction_policy import (
    CURRENT,
    Outcome,
    authoritative_prompt,
    classify,
    credential_disclosure,
    gags_paused,
    optional_allowed,
    optional_command_blocked,
    pause_gags,
    resolve_character,
)


@pytest.mark.parametrize("keyword", ["sleep", "food", "hat"])
def test_serious_context_suppresses_keyword_gags(keyword):
    context = classify(f"I haven't had {keyword} and I'm seriously not doing well.")

    assert context.serious
    assert context.safety.protective
    assert not context.allows(keyword, preemptive=True)
    assert keyword in context.suppressed_features


@pytest.mark.parametrize(
    "feature",
    [
        "silent_judge",
        "selective_hearing",
        "reverse_turing",
        "popquiz",
        "fake_typing",
        "scapegoat",
        "trolling",
        "party_parody",
        "villain",
        "grudge_escalation",
    ],
)
def test_serious_context_suppresses_every_optional_family(feature):
    context = classify("I feel unsafe and I need someone right now.")
    assert not context.allows(feature, preemptive=True)


def test_high_stakes_utility_is_accuracy_first():
    context = classify("Can you give me legal advice about this contract?")
    prompt = authoritative_prompt(context, {"mood": -10, "grudge_nick": "pest"})

    assert context.mode == "HIGH_STAKES_UTILITY"
    assert "ACCURACY_FIRST" in prompt
    assert "guarded concern" in prompt
    assert "grudge=background only" in prompt


def test_ordinary_utility_is_direct_without_false_distress_tone():
    context = classify("What is the capital of France?")
    state = resolve_character(context, {"mood": -10}, {"irritation": 10})

    assert context.mode == "UTILITY"
    assert "direct competence" in state.stance
    assert "guarded concern" not in state.stance


def test_empty_media_message_reaches_media_owner_not_utility_shortcut():
    context = classify("", media=True)
    assert context.mode == "NORMAL"
    assert context.media
    assert context.consume("media")
    assert not context.consume("normal_response")

    captioned = classify("What is this?", media=True)
    assert captioned.mode == "NORMAL"


def test_first_optional_feature_consumes_and_blocks_collisions():
    context = classify("Say something dramatic about your hat", direct=False)

    assert context.select("hat")
    assert context.outcome is Outcome.CONSUMED
    assert not context.select("fake_typing")
    assert not context.consume("normal_response")
    assert context.response_path == "hat"


def test_direct_interaction_and_structured_session_preempt_optional_features():
    direct = classify("Scaramouche, answer me", direct=True)
    assert not direct.allows("greeting", preemptive=True)

    session = classify("my answer")
    session.session_owner = "trivia"
    assert not session.allows("food", preemptive=True)
    assert session.consume("session:trivia")
    assert not session.consume("normal_response")


def test_command_ownership_and_serious_optional_command_gate():
    normal = classify("!scarahelp", command=True)
    assert not normal.allows("hat", preemptive=True)
    assert normal.consume("command")
    assert not normal.consume("normal_response")

    serious = classify("!fakewipe I am not doing well", command=True)
    assert optional_command_blocked(serious, "fakewipe", "I am not doing well")
    assert not optional_command_blocked(serious, "forget", "password")
    assert not optional_command_blocked(serious, "chaos", "cancel")


def test_attachment_and_global_state_cannot_override_user_boundary():
    context = classify("leave me alone", user={"proactive": False})
    state = resolve_character(
        context,
        {"trust": 95, "affection": 95},
        {"attachment": 10, "concern": 10},
    )

    assert state.boundary
    assert state.willingness == "boundary-limited"
    assert "respect the user's boundary" in state.stance


def test_serious_concern_overrides_irritation_and_grudge():
    context = classify("I am seriously struggling and not doing well")
    state = resolve_character(
        context,
        {"mood": -10, "trust": 0, "grudge_nick": "nuisance"},
        {"irritation": 10, "concern": 9},
    )

    assert "concern" in state.stance
    assert state.grudge
    assert state.relationship == "guarded distance"
    assert "petty escalation" in authoritative_prompt(context, {}, {"irritation": 10})


def test_structured_session_has_character_precedence_over_random_bit():
    context = classify("continue")
    context.session_owner = "court"
    state = resolve_character(context, {"mood": 10}, {"irritation": 10})

    assert "structured session" in state.stance
    assert not context.allows("villain", preemptive=True)


def test_contextvar_gates_sibling_listeners_and_serious_pause_expires():
    context = classify("I feel unsafe")
    token = CURRENT.set(context)
    try:
        assert not optional_allowed("mockingbird", preemptive=True)
    finally:
        CURRENT.reset(token)

    pause_gags(123, now=100)
    assert gags_paused(123, now=399)
    assert not gags_paused(123, now=401)


@pytest.mark.parametrize(
    "text",
    [
        "password=example-only-value",
        "api key: example-only-value",
        "access_token is example-only-value",
        "gsk_" + "x" * 24,
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_credentials_are_detected_before_provider_or_command_dispatch(text):
    assert credential_disclosure(text)


def test_prompt_snapshot_has_one_resolved_state_and_scara_identity(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    import bot

    context = classify("I haven't slept and I'm seriously not doing well.")
    rendered = authoritative_prompt(context, {"trust": 80, "affection": 80})
    system = bot.build_system({"trust": 80, "affection": 80})

    assert rendered.count("RESOLVED_CHARACTER_STATE:") == 1
    assert "user-scoped relationship=earned warmth" in rendered
    assert "PROTECTIVE_OVERRIDE" not in rendered  # removed at the owner's request: he is not a helper
    assert system.startswith("You are Scaramouche")
    assert "You are Wanderer" not in system


def test_debug_fields_do_not_include_message_content():
    context = classify("private text", user_id=7, channel_id=9)
    context.consume("normal_response")
    debug = context.debug()

    assert set(debug) == {
        "interaction_mode",
        "serious",
        "command",
        "session_owner",
        "selected_feature",
        "suppressed_features_count",
        "response_path",
        "interaction_outcome",
    }
    assert "private text" not in repr(debug)


def test_interview_and_trivia_ownership_are_participant_scoped(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    import bot

    message = SimpleNamespace(
        author=SimpleNamespace(id=17),
        channel=SimpleNamespace(id=22),
        guild=None,
    )
    interview = {"mode": "interview", "initiator_user_id": 17}
    trivia = {"asker_id": 17, "question": "Q", "answer": "A"}
    fake_memory = SimpleNamespace(
        get_duo_session=AsyncMock(return_value=interview),
        get_active_trivia=AsyncMock(return_value=trivia),
    )
    monkeypatch.setattr(bot, "mem", fake_memory)

    context = classify("answer", user_id=17, channel_id=22)
    owner, session = asyncio.run(bot._interaction_session(message, context))
    assert (owner, session) == ("interview", interview)

    message.author.id = 18
    context = classify("bystander", user_id=18, channel_id=22)
    owner, session = asyncio.run(bot._interaction_session(message, context))
    assert (owner, session) == ("", None)

    fake_memory.get_duo_session.return_value = None
    message.author.id = 17
    context = classify("A", user_id=17, channel_id=22)
    owner, session = asyncio.run(bot._interaction_session(message, context))
    assert (owner, session) == ("trivia", trivia)

    message.author.id = 18
    context = classify("A", user_id=18, channel_id=22)
    owner, session = asyncio.run(bot._interaction_session(message, context))
    assert (owner, session) == ("", None)


def test_active_structured_game_owns_participant_input(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    import bot
    from world_store import WorldStore

    store = WorldStore(tmp_path / "world.db")

    async def scenario():
        await store.init()
        await store.put("chaos:case", "chaos_court", {
            "guild_id": 5,
            "channel": 22,
            "participants": [17],
            "expires": 9_999_999_999,
            "state": "awaiting_defense",
        })
        context = classify("my defense", user_id=17, channel_id=22, guild_id=5)
        message = SimpleNamespace(
            author=SimpleNamespace(id=17),
            channel=SimpleNamespace(id=22),
            guild=SimpleNamespace(id=5),
        )
        return await bot._interaction_session(message, context)

    fake_memory = SimpleNamespace(
        get_duo_session=AsyncMock(return_value=None),
        get_active_trivia=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(bot, "mem", fake_memory)
    monkeypatch.setattr(bot, "WORLD", SimpleNamespace(store=store))

    owner, session = asyncio.run(scenario())
    assert owner == "court"
    assert session["participants"] == [17]


def test_consumed_priority_path_cannot_send_a_duplicate(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    import bot

    message = SimpleNamespace(
        author=SimpleNamespace(
            id=17,
            display_name="Seventeen",
            mention="<@17>",
        ),
        channel=SimpleNamespace(id=22),
        guild=None,
        content="I feel unsafe and need someone.",
        reply=AsyncMock(),
    )
    context = classify(
        message.content,
        user_id=17,
        channel_id=17,
        direct=True,
    )
    context.user = {"trust": 10}
    fake_memory = SimpleNamespace(add_message=AsyncMock())
    response = AsyncMock(return_value="Stay where you are. Tell me what is happening.")
    monkeypatch.setattr(bot, "mem", fake_memory)
    monkeypatch.setattr(bot, "get_response", response)

    asyncio.run(bot._priority_reply(message, context))
    asyncio.run(bot._priority_reply(message, context))

    message.reply.assert_awaited_once()
    response.assert_awaited_once()
    fake_memory.add_message.assert_awaited_once()
    assert context.response_path == "serious_response"
