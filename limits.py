"""Rate limiting and size-capped downloads.

``SlidingWindowLimiter`` keeps one bad actor from burning the shared model quota; ``download_capped``
makes sure attachments are never read into memory without an upper bound.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Hashable


class SlidingWindowLimiter:
    """Allow at most ``max_events`` per ``window_seconds`` for every key (a user id, a channel id, ...)."""

    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 5000,
    ):
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
        """Record an event and return True when it is within the limit; False (and no record) otherwise."""
        now = self._clock()
        events = self._events.setdefault(key, deque())
        self._trim(events, now)
        if len(events) >= self.max_events:
            return False
        events.append(now)
        if len(self._events) > self._max_keys:
            self.prune()
        return True

    def peek(self, key: Hashable) -> bool:
        """Would ``allow`` succeed right now? Does not record anything."""
        events = self._events.get(key)
        if not events:
            return True
        self._trim(events, self._clock())
        return len(events) < self.max_events

    def retry_after(self, key: Hashable) -> float:
        events = self._events.get(key)
        if not events:
            return 0.0
        now = self._clock()
        self._trim(events, now)
        if len(events) < self.max_events:
            return 0.0
        return max(0.0, self.window - (now - events[0]))

    def prune(self) -> None:
        now = self._clock()
        for key in list(self._events):
            events = self._events[key]
            self._trim(events, now)
            if not events:
                del self._events[key]

    def reset(self, key: Hashable | None = None) -> None:
        if key is None:
            self._events.clear()
        else:
            self._events.pop(key, None)


class DownloadError(Exception):
    """The download failed (HTTP error, timeout, network problem)."""


class DownloadTooLarge(DownloadError):
    """The download exceeded the byte cap."""


async def download_capped(url: str, max_bytes: int, *, timeout: float = 60.0) -> bytes:
    """Download ``url`` but never hold more than ``max_bytes`` in memory."""
    import aiohttp  # imported lazily so this module is importable without the dependency

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise DownloadError(f"HTTP {resp.status}")
                if resp.content_length is not None and resp.content_length > max_bytes:
                    raise DownloadTooLarge(f"{resp.content_length} bytes > {max_bytes}")
                buffer = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise DownloadTooLarge(f"more than {max_bytes} bytes")
                return bytes(buffer)
    except DownloadError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise DownloadError(f"{type(exc).__name__}: {exc}") from exc
