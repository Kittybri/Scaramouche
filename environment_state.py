"""Sanitized operating-environment observations.

No paths, hostnames, credentials, or raw infrastructure identifiers leave this
module.  These readings are context, not fabricated physical sensations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import asyncio
import os
import shutil
import time

from agent_config import AgentConfig, CONFIG


@dataclass(frozen=True)
class EnvironmentSnapshot:
    observed_ts: float
    uptime_seconds: int
    discord_latency_ms: int | None
    response_latency_ms: int | None
    database_health: str
    provider_status: str
    provider_failures_recent: int
    cpu_pressure: str
    memory_pressure: str
    disk_pressure: str
    active_conversations: int

    def sanitized(self) -> dict:
        return asdict(self)

    def prompt_fragment(self) -> str:
        return (
            "ENVIRONMENT_STATE:" + str(self.sanitized()) + "\n"
            "ENVIRONMENT_RULE: interpret only when relevant. Do not recite metrics, invent pain, "
            "or expose infrastructure details. A degraded service may subtly affect patience or pacing."
        )


class EnvironmentMonitor:
    def __init__(self, *, started_at: float | None = None, config: AgentConfig = CONFIG):
        self.started_at = started_at or time.time()
        self.config = config
        self._provider_failures: list[float] = []
        self._last_response_latency_ms: int | None = None

    def record_provider_failure(self, when: float | None = None) -> None:
        self._provider_failures.append(when or time.time())
        self._trim()

    def record_provider_success(self, response_latency_ms: int | None = None) -> None:
        if response_latency_ms is not None:
            self._last_response_latency_ms = max(0, int(response_latency_ms))
        self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - 3600
        self._provider_failures = [value for value in self._provider_failures if value >= cutoff]

    async def collect(self, *, discord_latency: float | None = None, db_probe=None,
                      active_conversations: int = 0) -> EnvironmentSnapshot:
        self._trim()
        database_health = "unknown"
        if db_probe is not None:
            try:
                result = db_probe()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=3)
                database_health = "healthy"
            except Exception:
                database_health = "degraded"
        cpu, memory, disk = await asyncio.to_thread(self._system_pressure)
        failures = len(self._provider_failures)
        provider = "degraded" if failures >= 2 else "recovering" if failures else "healthy"
        return EnvironmentSnapshot(
            observed_ts=time.time(), uptime_seconds=max(0, int(time.time() - self.started_at)),
            discord_latency_ms=int(discord_latency * 1000) if discord_latency is not None else None,
            response_latency_ms=self._last_response_latency_ms, database_health=database_health,
            provider_status=provider, provider_failures_recent=failures,
            cpu_pressure=cpu, memory_pressure=memory, disk_pressure=disk,
            active_conversations=max(0, int(active_conversations)),
        )

    def _system_pressure(self) -> tuple[str, str, str]:
        try:
            load = os.getloadavg()[0]
            cpu_pct = 100 * load / max(1, os.cpu_count() or 1)
            cpu = _level(cpu_pct, self.config.cpu_warning_percent)
        except (AttributeError, OSError):
            cpu = "unknown"
        # Portable stdlib cannot reliably report total memory. Keep it unknown
        # rather than pretending the process RSS is whole-system pressure.
        memory = "unknown"
        try:
            usage = shutil.disk_usage(os.path.abspath("."))
            disk_pct = 100 * usage.used / max(1, usage.total)
            disk = _level(disk_pct, self.config.disk_warning_percent)
        except OSError:
            disk = "unknown"
        return cpu, memory, disk


def _level(value: float, warning: float) -> str:
    if value >= warning:
        return "high"
    if value >= warning * .75:
        return "elevated"
    return "normal"
