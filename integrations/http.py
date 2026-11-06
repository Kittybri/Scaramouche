from __future__ import annotations

import asyncio
import aiohttp
from enum import Enum


class IntegrationErrorCategory(str, Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_REQUEST = "INVALID_REQUEST"


class IntegrationError(RuntimeError):
    def __init__(self, message: str, category: IntegrationErrorCategory = IntegrationErrorCategory.PROVIDER_UNAVAILABLE):
        super().__init__(message)
        self.category = category


class IntegrationAuthError(IntegrationError):
    def __init__(self, message: str = "integration authorization failed"):
        super().__init__(message, IntegrationErrorCategory.AUTH_FAILED)


class IntegrationForbiddenError(IntegrationError):
    def __init__(self, message: str = "integration access was forbidden"):
        super().__init__(message, IntegrationErrorCategory.FORBIDDEN)


class AsyncJSONClient:
    def __init__(self, timeout_seconds: float = 12.0, *, session_factory=None, sleep=None):
        self.timeout = aiohttp.ClientTimeout(total=max(2.0, min(30.0, timeout_seconds)))
        self.session_factory = session_factory or aiohttp.ClientSession
        self.sleep = sleep or asyncio.sleep

    async def request(self, method: str, url: str, *, headers=None, json=None, data=None, expected=(200, 201)):
        # A POST/PATCH that timed out or got a 5xx may already have been carried out (a calendar event, a task, an
        # issue, a playlist): retrying it can create a duplicate. Only requests that safely repeat are retried after
        # an ambiguous failure; a 429 or a refused connection means nothing was done, so those are retried for all.
        repeatable = method.upper() in {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
        for attempt in range(2):
            try:
                async with self.session_factory(timeout=self.timeout) as session:
                    async with session.request(method, url, headers=headers, json=json, data=data) as response:
                        if response.status in expected:
                            if response.status == 204:
                                return None
                            return await response.json(content_type=None)
                        if response.status == 401:
                            raise IntegrationAuthError()
                        if response.status == 403:
                            raise IntegrationForbiddenError()
                        if response.status == 429 or (response.status >= 500 and repeatable):
                            if attempt == 0:
                                retry_after = response.headers.get("Retry-After", "1")
                                try:
                                    delay = max(0.25, min(3.0, float(retry_after)))
                                except ValueError:
                                    delay = 1.0
                                await self.sleep(delay)
                                continue
                        if response.status == 429:
                            raise IntegrationError("integration rate limit reached", IntegrationErrorCategory.RATE_LIMITED)
                        if response.status >= 500:
                            raise IntegrationError("integration provider is unavailable", IntegrationErrorCategory.PROVIDER_UNAVAILABLE)
                        if response.status == 404:
                            raise IntegrationError("integration resource was not found", IntegrationErrorCategory.NOT_FOUND)
                        if response.status in {400, 405, 409, 412, 422}:
                            raise IntegrationError("integration request was rejected", IntegrationErrorCategory.INVALID_REQUEST)
                        raise IntegrationError("integration request failed", IntegrationErrorCategory.PROVIDER_UNAVAILABLE)
            except asyncio.TimeoutError as exc:
                if attempt == 0 and repeatable:
                    continue
                raise IntegrationError("integration request timed out", IntegrationErrorCategory.TIMEOUT) from exc
            except aiohttp.ClientError as exc:
                if attempt == 0 and (repeatable or isinstance(exc, aiohttp.ClientConnectorError)):
                    await self.sleep(0.25)
                    continue
                raise IntegrationError(
                    "integration provider is unavailable",
                    IntegrationErrorCategory.PROVIDER_UNAVAILABLE,
                ) from exc
        raise IntegrationError("integration request failed", IntegrationErrorCategory.PROVIDER_UNAVAILABLE)
