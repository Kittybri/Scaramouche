import asyncio
import importlib
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_config import AgentConfig
from environment_state import EnvironmentMonitor
from heartbeat import HeartbeatCoordinator
from memory import Memory
from self_model import SelfModelStore


def run(coro):
    return asyncio.run(coro)


def load_runtime(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_API_KEY_2", "")
    monkeypatch.setenv("GROQ_API_KEY_3", "")
    return importlib.import_module("bot")


def test_background_task_start_is_idempotent_and_shutdown_cancels(monkeypatch):
    runtime = load_runtime(monkeypatch)
    assert runtime.ai._clients
    assert all(client.max_retries == 0 for client in runtime.ai._clients)
    assert all(client.timeout == runtime.CONFIG.provider_timeout_seconds for client in runtime.ai._clients)

    async def scenario():
        await runtime._stop_background_tasks()
        started = 0
        gate = asyncio.Event()

        async def worker():
            nonlocal started
            started += 1
            await gate.wait()

        runtime._start_background_once("audit-worker", worker)
        runtime._start_background_once("audit-worker", worker)
        runtime._spawn_transient(worker(), name="audit-transient")
        await asyncio.sleep(0)
        assert started == 2
        assert len(runtime._background_tasks) == 1
        assert len(runtime._transient_tasks) == 1
        await runtime._stop_background_tasks()
        assert runtime._background_tasks == {}
        assert runtime._transient_tasks == set()

    run(scenario())


def test_runtime_initialization_is_once_even_when_ready_overlaps(monkeypatch):
    runtime = load_runtime(monkeypatch)

    class FakeMemory:
        def __init__(self):
            self.init_calls = 0

        async def init(self):
            self.init_calls += 1
            await asyncio.sleep(0)

    class FakeStore:
        def __init__(self):
            self.init_calls = 0
            self.beliefs = []

        async def init(self):
            self.init_calls += 1
            await asyncio.sleep(0)

        async def add_belief(self, belief, **kwargs):
            self.beliefs.append(belief)

    fake_memory = FakeMemory()
    fake_store = FakeStore()
    monkeypatch.setattr(runtime, "mem", fake_memory)
    monkeypatch.setattr(runtime, "self_store", fake_store)
    monkeypatch.setattr(runtime, "_runtime_initialized", False)
    monkeypatch.setattr(runtime, "_initialization_lock", None)

    async def scenario():
        await asyncio.gather(runtime._initialize_runtime_once(), runtime._initialize_runtime_once())

    run(scenario())
    assert fake_memory.init_calls == 1
    assert fake_store.init_calls == 1
    assert len(fake_store.beliefs) == 3


def test_runtime_initialization_failure_can_retry(monkeypatch):
    runtime = load_runtime(monkeypatch)

    class FakeMemory:
        def __init__(self):
            self.calls = 0

        async def init(self):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("temporarily unavailable")

    class FakeStore:
        async def init(self):
            return None

        async def add_belief(self, *args, **kwargs):
            return None

    memory = FakeMemory()
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime, "self_store", FakeStore())
    monkeypatch.setattr(runtime, "_runtime_initialized", False)
    monkeypatch.setattr(runtime, "_initialization_lock", None)
    with pytest.raises(sqlite3.OperationalError):
        run(runtime._initialize_runtime_once())
    assert runtime._runtime_initialized is False
    run(runtime._initialize_runtime_once())
    assert runtime._runtime_initialized is True
    assert memory.calls == 2


def test_multiple_ready_events_do_not_duplicate_workers_or_discord_loops(monkeypatch):
    runtime = load_runtime(monkeypatch)

    class FakeLoop:
        def __init__(self):
            self.running = False
            self.starts = 0

        def is_running(self):
            return self.running

        def start(self):
            self.running = True
            self.starts += 1

    async def scenario():
        supervisor = runtime.TaskSupervisor(logger=runtime.logger)
        gate = asyncio.Event()

        async def worker():
            await gate.wait()

        monkeypatch.setattr(runtime, "_task_supervisor", supervisor)
        monkeypatch.setattr(runtime, "_background_tasks", supervisor.tasks)
        monkeypatch.setattr(runtime, "_initialize_runtime_once", AsyncMock())
        monkeypatch.setattr(runtime, "_self_heartbeat_loop", worker)
        monkeypatch.setattr(runtime, "_duo_autoplay_loop", worker)
        monkeypatch.setattr(runtime, "_weather_proactive_loop", worker)
        monkeypatch.setattr(runtime, "_temporary_setting_restore_loop", worker)
        loops = [FakeLoop(), FakeLoop(), FakeLoop()]
        monkeypatch.setattr(runtime, "status_rotation", loops[0])
        monkeypatch.setattr(runtime, "reminder_checker", loops[1])
        monkeypatch.setattr(runtime, "daily_reset", loops[2])
        monkeypatch.setattr(runtime, "PARTNER_BOT_ID", 0)
        monkeypatch.setattr(runtime.bot._connection, "user", SimpleNamespace(id=99))
        await runtime.on_ready()
        await runtime.on_ready()
        await asyncio.sleep(0)
        assert all(loop.starts == 1 for loop in loops)
        assert len(supervisor.tasks) == 4
        assert all(item["starts"] == 1 for item in supervisor.snapshot())
        await supervisor.stop()

    run(scenario())


def _proactive_fakes(runtime, monkeypatch, *, finish_action=None, generation=None, send=None):
    member = SimpleNamespace(id=7, display_name="User", mention="<@7>")
    guild = SimpleNamespace(get_member=lambda user_id: member if user_id == 7 else None)
    channel = SimpleNamespace(guild=guild, send=send or AsyncMock())
    store = SimpleNamespace(
        reserve_action=AsyncMock(return_value=12),
        finish_action=finish_action or AsyncMock(return_value=True),
        record_event=AsyncMock(),
        add_belief=AsyncMock(return_value=3),
        add_belief_evidence=AsyncMock(),
    )
    memory = SimpleNamespace(
        get_user=AsyncMock(return_value={}),
        add_message=AsyncMock(),
        set_proactive_sent=AsyncMock(),
    )
    coordinator = SimpleNamespace(
        reserve_autonomous_call=AsyncMock(return_value=True),
        provider_succeeded=AsyncMock(),
        provider_failed=AsyncMock(),
    )
    monkeypatch.setattr(runtime, "self_store", store)
    monkeypatch.setattr(
        runtime, "SELF_MODEL_POLICY",
        SimpleNamespace(store=store, observe=AsyncMock()),
    )
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime, "heartbeat", coordinator)
    monkeypatch.setattr(runtime.bot, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(
        runtime,
        "_autonomous_character_generation",
        generation or AsyncMock(return_value="You took long enough."),
    )
    action = SimpleNamespace(
        action=runtime.ActionType.SEND_PROACTIVE_MESSAGE,
        reason="unfinished conversation",
        user_id=7,
        channel_id=8,
        payload={"display_name": "User", "context": "a prior topic"},
    )
    return action, channel, store, memory, coordinator


def test_proactive_send_is_delivered_before_isolated_followups(monkeypatch):
    runtime = load_runtime(monkeypatch)
    action, channel, store, memory, _ = _proactive_fakes(runtime, monkeypatch)
    memory.add_message.side_effect = sqlite3.OperationalError("memory unavailable")
    run(runtime._execute_proactive_action(action))
    channel.send.assert_awaited_once()
    statuses = [call.args[1] for call in store.finish_action.await_args_list]
    assert statuses == ["delivered"]
    memory.set_proactive_sent.assert_awaited_once()
    runtime.SELF_MODEL_POLICY.observe.assert_awaited_once()


def test_proactive_marker_failure_keeps_send_success_and_pending_barrier(monkeypatch):
    runtime = load_runtime(monkeypatch)
    statuses = []

    async def finish_action(action_id, status, **kwargs):
        statuses.append(status)
        if status == "delivered":
            raise sqlite3.OperationalError("marker unavailable")
        return True

    action, channel, _, memory, _ = _proactive_fakes(
        runtime, monkeypatch, finish_action=finish_action,
    )
    run(runtime._execute_proactive_action(action))
    channel.send.assert_awaited_once()
    assert statuses == ["delivered"]
    memory.add_message.assert_awaited_once()


def test_proactive_provider_failure_never_sends(monkeypatch):
    runtime = load_runtime(monkeypatch)
    generation = AsyncMock(side_effect=TimeoutError("provider timeout"))
    action, channel, store, _, coordinator = _proactive_fakes(
        runtime, monkeypatch, generation=generation,
    )
    run(runtime._execute_proactive_action(action))
    channel.send.assert_not_awaited()
    coordinator.provider_failed.assert_awaited_once()
    assert [call.args[1] for call in store.finish_action.await_args_list] == ["failed"]


def test_proactive_forbidden_is_terminal_without_retry(monkeypatch):
    runtime = load_runtime(monkeypatch)

    class Response:
        status = 403
        reason = "Forbidden"
        headers = {}

    send = AsyncMock(side_effect=runtime.discord.Forbidden(Response(), "forbidden"))
    action, channel, store, _, _ = _proactive_fakes(runtime, monkeypatch, send=send)
    run(runtime._execute_proactive_action(action))
    channel.send.assert_awaited_once()
    assert [call.args[1] for call in store.finish_action.await_args_list] == ["failed"]


def test_weather_candidate_failure_does_not_block_next_candidate(monkeypatch):
    runtime = load_runtime(monkeypatch)
    candidates = [
        {"user_id": 1},
        {"user_id": 2, "weather_location": "San Jose", "timezone_name": "UTC",
         "quiet_hours_start": 0, "quiet_hours_end": 0},
    ]
    member = SimpleNamespace(mention="<@2>")
    me = object()
    guild = SimpleNamespace(me=me, get_member=lambda uid: member)
    channel = SimpleNamespace(
        guild=guild,
        permissions_for=lambda _: SimpleNamespace(send_messages=True),
        send=AsyncMock(),
    )
    memory = SimpleNamespace(
        get_weather_candidates=AsyncMock(return_value=candidates),
        phrase_cooldown_remaining=AsyncMock(return_value=0),
        get_user_last_channel=AsyncMock(return_value=9),
        consume_phrase=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime.bot, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(runtime.bot, "is_closed", lambda: False if memory.get_weather_candidates.await_count == 0 else True)
    monkeypatch.setattr(runtime.bot, "get_channel", lambda _: channel)
    monkeypatch.setattr(runtime, "_fetch_nws_weather", AsyncMock(return_value={
        "place": "San Jose", "forecast": "Severe thunderstorms", "temperature": 70,
        "temperature_unit": "F", "wind_speed": "10 mph", "wind_direction": "W",
    }))
    monkeypatch.setattr(runtime.asyncio, "sleep", AsyncMock())
    run(runtime._weather_proactive_loop())
    channel.send.assert_awaited_once()


def test_stale_duo_session_does_not_kill_scan(monkeypatch):
    runtime = load_runtime(monkeypatch)
    memory = SimpleNamespace(get_due_duo_sessions=AsyncMock(return_value=["bad", {"channel_id": 99, "mode": "argue"}]))
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime.bot, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(runtime.bot, "is_closed", lambda: memory.get_due_duo_sessions.await_count > 0)
    monkeypatch.setattr(runtime.bot, "get_channel", lambda _: None)
    monkeypatch.setattr(runtime.asyncio, "sleep", AsyncMock())
    run(runtime._duo_autoplay_loop())
    memory.get_due_duo_sessions.assert_awaited_once()


def test_restoration_malformed_record_does_not_kill_scan(monkeypatch):
    runtime = load_runtime(monkeypatch)
    memory = SimpleNamespace(get_due_temporary_channel_settings=AsyncMock(return_value=[None, {"channel_id": 9, "setting": "slowmode"}]))
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime.bot, "wait_until_ready", AsyncMock())
    monkeypatch.setattr(runtime.bot, "is_closed", lambda: memory.get_due_temporary_channel_settings.await_count > 0)
    monkeypatch.setattr(runtime.bot, "get_channel", lambda _: object())
    monkeypatch.setattr(runtime.asyncio, "sleep", AsyncMock())
    run(runtime._temporary_setting_restore_loop())
    memory.get_due_temporary_channel_settings.assert_awaited_once()


def test_one_reminder_failure_does_not_block_the_next(monkeypatch):
    runtime = load_runtime(monkeypatch)
    reminders = [
        {"id": 1, "user_id": 7, "channel_id": 8, "reminder": "first"},
        {"id": 2, "user_id": 7, "channel_id": 8, "reminder": "second"},
    ]
    channel = SimpleNamespace(send=AsyncMock())
    user = SimpleNamespace(display_name="User", mention="<@7>")
    memory = SimpleNamespace(
        get_due_reminders=AsyncMock(return_value=reminders),
        get_scene_state=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime.HOME, "tick", AsyncMock())
    monkeypatch.setattr(runtime.bot, "get_channel", lambda _: channel)
    monkeypatch.setattr(runtime.bot, "fetch_user", AsyncMock(return_value=user))
    monkeypatch.setattr(
        runtime, "qai", AsyncMock(side_effect=[TimeoutError("provider timeout"), "Second reminder"]),
    )
    run(runtime.reminder_checker.coro())
    channel.send.assert_awaited_once_with("<@7> Second reminder")


def test_provider_rotation_attempts_each_key_once(monkeypatch):
    runtime = load_runtime(monkeypatch)

    class Completions:
        def __init__(self, result=None):
            self.calls = 0
            self.result = result
            self.last_kwargs = None

        def create(self, **kwargs):
            self.calls += 1
            self.last_kwargs = kwargs
            if self.result is None:
                raise RuntimeError("429 rate_limit")
            return self.result

    first = Completions()
    expected = object()
    second = Completions(expected)
    rotator = runtime.RotatingGroq()
    rotator._clients = [
        SimpleNamespace(chat=SimpleNamespace(completions=first)),
        SimpleNamespace(chat=SimpleNamespace(completions=second)),
    ]
    assert rotator.call_with_retry(model="openai/gpt-oss-120b", messages=[]) is expected
    assert first.calls == 1 and second.calls == 1
    assert first.last_kwargs["reasoning_effort"] == "low"
    assert second.last_kwargs["reasoning_effort"] == "low"

    rotator._clients = []
    with pytest.raises(RuntimeError, match="not configured"):
        rotator.call_with_retry(model="test", messages=[])


def test_owner_diagnostics_check_exact_nonzero_owner(monkeypatch):
    runtime = load_runtime(monkeypatch)
    monkeypatch.setattr(runtime, "OWNER_ID", 42)
    assert runtime._owner_only(SimpleNamespace(author=SimpleNamespace(id=42))) is True
    assert runtime._owner_only(SimpleNamespace(author=SimpleNamespace(id=41))) is False
    monkeypatch.setattr(runtime, "OWNER_ID", 0)
    assert runtime._owner_only(SimpleNamespace(author=SimpleNamespace(id=0))) is False


def test_unrestricted_prompt_requires_an_allowed_channel(monkeypatch):
    runtime = load_runtime(monkeypatch)
    user = {"unrestricted_mode": True, "romance_mode": False, "adult_confirmed": True}
    assert "## Unrestricted Mode" not in runtime.build_system(user)
    assert "## Unrestricted Mode" in runtime.build_system(user, allow_unrestricted=True)
    assert "Swear freely and naturally" in runtime.build_system(user, allow_unrestricted=True)
    romance = {**user, "romance_mode": True}
    assert "## Unrestricted Romance — Kai" in runtime.build_system(romance, "Kai", allow_unrestricted=True)
    assert "Unrestricted" not in runtime.build_system(romance, "Kai")
    for prompt in (runtime.build_system(user, allow_unrestricted=True),
                   runtime.build_system(romance, "Kai", allow_unrestricted=True)):
        assert "under 18, none of this applies" in prompt
    unconfirmed = {"unrestricted_mode": True, "romance_mode": False}
    assert "## Unrestricted Mode" not in runtime.build_system(unconfirmed, allow_unrestricted=True)
    discord_age_flag = "is_" + "ns" + "fw"
    public = SimpleNamespace(guild=object(), **{discord_age_flag: lambda: False})
    restricted = SimpleNamespace(guild=object(), **{discord_age_flag: lambda: True})
    assert runtime._channel_allows_unrestricted(public) is False
    assert runtime._channel_allows_unrestricted(restricted) is True
    assert runtime._channel_allows_unrestricted(None, is_dm=True) is True


@pytest.mark.parametrize(
    "mode,current,expected",
    [("on", False, True), ("off", True, False), (None, False, True)],
)
def test_unrestricted_command_reads_and_writes_renamed_preference(
    monkeypatch, mode, current, expected
):
    runtime = load_runtime(monkeypatch)
    # an adult who already confirmed: the command just flips the renamed preference
    setup = AsyncMock(return_value={"unrestricted_mode": current, "adult_confirmed": True})
    set_mode = AsyncMock()
    reply = AsyncMock()
    monkeypatch.setattr(runtime, "_setup", setup)
    monkeypatch.setattr(runtime.mem, "set_mode", set_mode)
    monkeypatch.setattr(runtime, "safe_reply", reply)
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=73), guild=None, channel=None
    )

    run(runtime.bot.get_command("unrestricted").callback(ctx, mode))

    setup.assert_awaited_once_with(ctx)
    set_mode.assert_awaited_once_with(73, "unrestricted_mode", expected)
    reply.assert_awaited_once()
    assert runtime.bot.get_command("ns" + "fw") is None


def test_unrestricted_needs_an_adult_confirmation_first(monkeypatch):
    runtime = load_runtime(monkeypatch)
    monkeypatch.setattr(runtime, "_setup", AsyncMock(return_value={"unrestricted_mode": False}))
    set_mode = AsyncMock()
    monkeypatch.setattr(runtime.mem, "set_mode", set_mode)
    ctx = SimpleNamespace(author=SimpleNamespace(id=73), guild=None, channel=None, reply=AsyncMock())

    run(runtime.bot.get_command("unrestricted").callback(ctx, "on"))

    set_mode.assert_not_awaited()  # DMs are not age-restricted: nothing changes before the 18+ confirmation
    assert isinstance(ctx.reply.await_args.kwargs["view"], runtime.AdultConfirmView)
    assert "18" in ctx.reply.await_args.args[0]
    # and the prompt ignores a stored preference that was never confirmed
    assert "Unrestricted Mode" not in runtime.build_system({"unrestricted_mode": True}, allow_unrestricted=True)
    assert "Unrestricted Mode" in runtime.build_system({"unrestricted_mode": True, "adult_confirmed": True}, allow_unrestricted=True)


def test_member_announcements_are_rate_limited_per_guild(monkeypatch):
    runtime = load_runtime(monkeypatch)
    runtime._member_announcement_last_sent.clear()
    assert runtime._reserve_member_announcement(123, now=1_000) is True
    assert runtime._reserve_member_announcement(123, now=1_299) is False
    assert runtime._reserve_member_announcement(124, now=1_299) is True
    assert runtime._reserve_member_announcement(123, now=1_300) is True


def test_normal_generation_path_uses_bounded_persistent_context(monkeypatch, tmp_path):
    runtime = load_runtime(monkeypatch)
    memory = Memory("audit", db_path=str(tmp_path / "audit.db"), shared_db_path=str(tmp_path / "shared.db"))
    store = SelfModelStore(memory.db_path, AgentConfig())
    run(memory.init())
    run(store.init())
    run(memory.upsert_user(12, "twelve", "Twelve"))
    for index in range(60):
        role = "user" if index % 2 == 0 else "assistant"
        run(memory.add_message(12, 44, role, f"history-{index}-" + "x" * 900))
    run(store.add_belief("This user's return affected my attention.", confidence=.7, scope_user_id=12))
    run(store.add_reflection(
        "return", "generic observation", "I noticed the return before I chose how to answer.",
        importance=8, related_user_id=12,
    ))

    captured = []

    class FakeAI:
        def call_with_retry(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="I am a Discord bot implementation. That does not make your question impressive."),
            )])

    config = AgentConfig()
    monkeypatch.setattr(runtime, "mem", memory)
    monkeypatch.setattr(runtime, "self_store", store)
    from persistent_world import PersistentWorld
    monkeypatch.setattr(runtime, "WORLD", PersistentWorld("scaramouche", memory, {}, self_store=store))
    monkeypatch.setattr(runtime, "heartbeat", HeartbeatCoordinator(store, EnvironmentMonitor(config=config), config))
    monkeypatch.setattr(runtime, "ai", FakeAI())
    run(runtime._record_self_perception(
        12, "I'm back", returned_after_absence=True,
        relationship_significance=80,
    ))
    user = run(memory.get_user(12))
    prior = time.time() - 5 * 86400
    reply = run(runtime.get_response(
        12, 44, "Are you actually an AI Discord bot?", user, "Twelve", "<@12>",
        prior_last_active=prior,
    ))

    assert reply.startswith("I am a Discord bot implementation")
    assert len(captured) == 1
    assert "frequency_penalty" not in captured[0]
    assert "presence_penalty" not in captured[0]
    assert captured[0]["max_completion_tokens"] == 800
    assert "max_tokens" not in captured[0]
    messages = captured[0]["messages"]
    assert len(messages) <= config.conversation_history_limit + 2
    assert sum(len(message["content"]) for message in messages) <= 35_000
    rendered = "\n".join(message["content"] for message in messages)
    assert "SELF_BELIEFS:" in rendered
    assert "RELEVANT_SELF_REFLECTIONS:" in rendered
    assert "A familiar user returned after an absence" in rendered
    assert "LAST_SEEN:5.0d_ago" in rendered
    assert "IMPLEMENTATION_RELEVANT" in rendered
    assert rendered.count("RESOLVED_CHARACTER_STATE:") == 1
    assert "user-scoped relationship=" in rendered
    assert messages[0]["content"].startswith("You are Scaramouche")
    assert "You are Wanderer" not in messages[0]["content"]

    memory.update_mood = AsyncMock()
    memory.update_trust = AsyncMock()
    memory.update_affection = AsyncMock()
    serious_user = dict(user, mood=-10, grudge_nick="pest")
    serious = runtime.classify_interaction(
        "I haven't slept and I'm seriously not doing well.",
        user_id=12,
        channel_id=44,
        user=serious_user,
        direct=True,
    )
    run(runtime.get_response(
        12, 44, "I haven't slept and I'm seriously not doing well.",
        serious_user, "Twelve", "<@12>", interaction=serious,
    ))

    serious_rendered = "\n".join(
        message["content"] for message in captured[-1]["messages"]
    )
    assert serious_rendered.count("RESOLVED_CHARACTER_STATE:") == 1
    assert "PROTECTIVE_OVERRIDE" not in serious_rendered  # removed at the owner's request
    assert "ACCURACY_FIRST:" in serious_rendered
    assert "GRUDGE:pest" not in serious_rendered
    memory.update_mood.assert_not_awaited()
    memory.update_trust.assert_not_awaited()
    memory.update_affection.assert_not_awaited()
