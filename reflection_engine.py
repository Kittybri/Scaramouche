"""Selective metacognitive reflection planning."""

from __future__ import annotations

from dataclasses import dataclass
import time

from agent_config import AgentConfig, CONFIG


@dataclass(frozen=True)
class ReflectionRequest:
    event_ids: tuple[int, ...]
    trigger: str
    observation: str
    importance: int
    related_user_id: int | None


class ReflectionEngine:
    def __init__(self, config: AgentConfig = CONFIG):
        self.config = config
        self._last_requested_at = 0.0

    def select(self, events: list[dict], *, now: float | None = None) -> ReflectionRequest | None:
        """Use cheap heuristics before any LLM call is considered."""
        now = now or time.time()
        meaningful = [e for e in events if int(e.get("importance", 0)) >= self.config.reflection_threshold]
        if not meaningful:
            return None
        meaningful.sort(key=lambda item: (int(item.get("importance", 0)), float(item.get("ts", 0))), reverse=True)
        top = meaningful[0]
        if now - self._last_requested_at < 900:
            return None
        self._last_requested_at = now
        related = {e.get("related_user_id") for e in meaningful if e.get("related_user_id") is not None}
        user_id = related.pop() if len(related) == 1 else top.get("related_user_id")
        observation = " | ".join(str(e.get("summary", ""))[:240] for e in meaningful[:4])
        return ReflectionRequest(
            tuple(int(e["id"]) for e in meaningful[:4]), str(top.get("event_type", "event")),
            observation, int(top.get("importance", 1)), int(user_id) if user_id else None,
        )

    @staticmethod
    def prompt(request: ReflectionRequest) -> str:
        return (
            "Write a private self-reflection for Scaramouche's persistent self-model. "
            "This is an implementation-generated metacognitive note, not a claim of consciousness.\n"
            f"Trigger: {request.trigger}\nObservation: {request.observation}\n"
            "Return one concise interpretation under 70 words. Focus on why he reacted, any conflict "
            "between behavior and self-image, and what may matter later. No dialogue, no roleplay narration, "
            "no claims of biological sensation."
        )
