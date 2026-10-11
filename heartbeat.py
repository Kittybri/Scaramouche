"""Low-cost heartbeat for decay, environment updates, and bounded decisions."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import logging
import time
from typing import Awaitable, Callable

from agency import ActionProposal, ActionType, AgencyPolicy, no_action
from agent_config import AgentConfig, CONFIG
from environment_state import EnvironmentMonitor, EnvironmentSnapshot
from reflection_engine import ReflectionEngine
from self_model import SelfModelStore


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HeartbeatResult:
    action: ActionProposal
    environment: EnvironmentSnapshot
    reflected: bool = False
    expired_goals: int = 0


class HeartbeatCoordinator:
    def __init__(self, store: SelfModelStore, environment: EnvironmentMonitor,
                 config: AgentConfig = CONFIG):
        self.store = store
        self.environment = environment
        self.config = config
        self.reflections = ReflectionEngine(config)
        self.policy = AgencyPolicy()
        self._lock: asyncio.Lock | None = None
        self._last_environment: EnvironmentSnapshot | None = None
        self._last_tick = 0.0
        self._provider_backoff_until = 0.0

    @property
    def running(self) -> bool:
        return bool(self._lock and self._lock.locked())

    @property
    def last_tick(self) -> float:
        return self._last_tick

    @property
    def last_environment(self) -> EnvironmentSnapshot | None:
        return self._last_environment

    async def reserve_autonomous_call(self, now: float | None = None) -> bool:
        now = now or time.time()
        persisted = await self.store.get_runtime_float("provider_backoff_until", 0.0)
        self._provider_backoff_until = max(self._provider_backoff_until, persisted)
        if now < self._provider_backoff_until:
            return False
        return await self.store.consume_budget(now=now)

    async def provider_failed(self, now: float | None = None) -> None:
        self.environment.record_provider_failure()
        now = now or time.time()
        self._provider_backoff_until = max(
            self._provider_backoff_until, now + self.config.provider_backoff_seconds
        )
        await self.store.set_runtime_value("provider_backoff_until", self._provider_backoff_until)

    async def provider_succeeded(self, latency_ms: int | None = None) -> None:
        self.environment.record_provider_success(latency_ms)
        self._provider_backoff_until = 0.0
        await self.store.set_runtime_value("provider_backoff_until", 0.0)

    async def tick(
        self, *, discord_latency: float | None = None, db_probe=None,
        active_conversations: int = 0,
        reflection_generator: Callable[[str], Awaitable[str]] | None = None,
        proactive_candidates: list[dict] | None = None,
        now: float | None = None,
    ) -> HeartbeatResult:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._lock.locked():
            snapshot = self._last_environment or await self.environment.collect(
                discord_latency=discord_latency, db_probe=db_probe, active_conversations=active_conversations
            )
            return HeartbeatResult(no_action("heartbeat already running"), snapshot)

        async with self._lock:
            now = now or time.time()
            self._last_tick = now
            await self.store.decay_mood(now)
            expired = await self.store.expire_goals(now)
            snapshot = await self.environment.collect(
                discord_latency=discord_latency, db_probe=db_probe, active_conversations=active_conversations
            )
            await self._record_environment_change(snapshot)
            self._last_environment = snapshot

            events = await self.store.pending_events(self.config.reflection_threshold, 20)
            request = self.reflections.select(events, now=now)
            reflected = False
            if request and reflection_generator and await self.reserve_autonomous_call(now):
                started = time.monotonic()
                try:
                    interpretation = (await reflection_generator(self.reflections.prompt(request))).strip()
                    if not interpretation:
                        raise ValueError("reflection provider returned empty content")
                    await self.store.add_reflection(
                        request.trigger, request.observation, interpretation,
                        importance=request.importance, confidence=.65,
                        related_user_id=request.related_user_id,
                    )
                    await self.store.record_action(
                        ActionType.WRITE_REFLECTION.value, "completed", request.trigger,
                        related_user_id=request.related_user_id,
                    )
                    await self.store.mark_events_processed(request.event_ids)
                    reflected = True
                    await self.provider_succeeded(int((time.monotonic() - started) * 1000))
                except Exception as exc:
                    await self.provider_failed(now)
                    await self.store.record_action(
                        ActionType.WRITE_REFLECTION.value, "failed", request.trigger,
                        related_user_id=request.related_user_id, error_category=type(exc).__name__,
                    )
                    log.warning("reflection failed", extra={"action_type": "WRITE_REFLECTION", "error_category": type(exc).__name__})

            action = await self._choose_proactive(proactive_candidates or [], now)
            if action.action is ActionType.NO_ACTION and not await self.store.action_on_cooldown(
                ActionType.NO_ACTION.value, 21_600
            ):
                await self.store.record_action(
                    ActionType.NO_ACTION.value, "completed", action.reason,
                    details={"heartbeat": True},
                )
            return HeartbeatResult(action, snapshot, reflected, expired)

    async def _record_environment_change(self, current: EnvironmentSnapshot) -> None:
        previous = self._last_environment
        if previous is None:
            return
        changes = []
        for name in ("database_health", "provider_status", "cpu_pressure", "memory_pressure", "disk_pressure"):
            old, new = getattr(previous, name), getattr(current, name)
            if old != new:
                changes.append(f"{name} changed from {old} to {new}")
        if changes:
            severity = 8 if any("degraded" in change or "high" in change for change in changes) else 4
            await self.store.record_event(
                "environment_change", "; ".join(changes), importance=severity,
                dedupe_key="environment:" + ":".join(changes),
            )

    async def _choose_proactive(self, candidates: list[dict], now: float) -> ActionProposal:
        """Choose only candidates with an explicit reason and opt-in."""
        eligible = [
            item for item in candidates
            if item.get("reason") and item.get("proactive", False) and not item.get("muted", False)
            and int(item.get("relationship_significance", 0)) >= 60
        ]
        eligible.sort(key=lambda item: int(item.get("relationship_significance", 0)), reverse=True)
        for item in eligible:
            user_id, channel_id = int(item.get("user_id", 0)), int(item.get("channel_id", 0))
            if not user_id or not channel_id:
                continue
            if await self.store.action_on_cooldown(
                ActionType.SEND_PROACTIVE_MESSAGE.value, self.config.proactive_user_cooldown_seconds,
                user_id=user_id,
            ):
                continue
            if await self.store.action_on_cooldown(
                ActionType.SEND_PROACTIVE_MESSAGE.value, self.config.proactive_cooldown_seconds,
                channel_id=channel_id,
            ):
                continue
            try:
                return self.policy.validate({
                    "action": ActionType.SEND_PROACTIVE_MESSAGE.value,
                    "reason": str(item["reason"]), "user_id": user_id, "channel_id": channel_id,
                    "payload": {"display_name": item.get("display_name", ""), "context": item.get("context", "")},
                }, permission_allowed=bool(item.get("permission_allowed", True)),
                   user_opted_in=bool(item.get("proactive", False)), muted=bool(item.get("muted", False)),
                   channel_sendable=bool(item.get("channel_sendable", True)))
            except (ValueError, PermissionError):
                continue
        return no_action("no meaningful eligible autonomous action")
