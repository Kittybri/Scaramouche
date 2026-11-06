"""Per-user sliding-window limits, so one person cannot burn the shared model quota for everyone."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable


class SlidingWindowLimiter:
    """Allow at most ``max_events`` per ``window_seconds`` for every key (a user id, a channel id, ...)."""

    def __init__(self, max_events: int, window_seconds: float, *, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 5000):
        if max_events < 1 or window_seconds <= 0:
            raise ValueError("max_events must be >= 1 and window_seconds > 0")
        self.max_events = max_events
        self.window = float(window_seconds)
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[Hashable, deque[float]] = {}

    def _trim(self, events: deque[float], now: float) -> None:
        while events and now - events[0] >= self.window:
            events.popleft()

    def allow(self, key: Hashable) -> bool:
        """Record an event and return True when it is within the limit; False (and nothing recorded) otherwise."""
        now = self._clock()
        events = self._events.setdefault(key, deque())
        self._trim(events, now)
        if len(events) >= self.max_events:
            return False
        events.append(now)
        if len(self._events) > self._max_keys:
            self.prune()
        return True

    def prune(self) -> None:
        now = self._clock()
        for key in list(self._events):
            self._trim(self._events[key], now)
            if not self._events[key]:
                del self._events[key]

    def reset(self, key: Hashable | None = None) -> None:
        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)
