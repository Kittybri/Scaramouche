import asyncio
import importlib
import time
from types import SimpleNamespace

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


def test_nsfw_prompt_requires_an_allowed_channel(monkeypatch):
    runtime = load_runtime(monkeypatch)
    user = {"nsfw_mode": True, "romance_mode": False}
    assert "## Unfiltered Mode" not in runtime.build_system(user)
    assert "## Unfiltered Mode" in runtime.build_system(user, allow_nsfw=True)
    public = SimpleNamespace(guild=object(), is_nsfw=lambda: False)
    restricted = SimpleNamespace(guild=object(), is_nsfw=lambda: True)
    assert runtime._channel_allows_nsfw(public) is False
    assert runtime._channel_allows_nsfw(restricted) is True
    assert runtime._channel_allows_nsfw(None, is_dm=True) is True


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
    monkeypatch.setattr(runtime, "heartbeat", HeartbeatCoordinator(store, EnvironmentMonitor(config=config), config))
    monkeypatch.setattr(runtime, "ai", FakeAI())
    run(runtime._record_self_perception(12, "I'm back", returned_after_absence=True))
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
