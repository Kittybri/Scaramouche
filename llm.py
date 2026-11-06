"""Groq access: API key rotation, model fallback and error classification.

The bot never hard-codes a single model. ``GROQ_MODELS`` / ``GROQ_VISION_MODELS`` (comma separated)
are tried in order. When Groq reports that a model is gone (or blocked for this account) it is skipped for a
while, so a retired model name degrades into a slower start instead of a dead bot. Anything temporary (a rate
limit, an outage) only moves *that request* on to the next model: the preferred model is tried first again as soon
as it recovers.

``call_with_retry`` is *blocking* (the Groq SDK is synchronous) - call it from an executor.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger("scaramouche.llm")

DEFAULT_CHAT_MODELS: tuple[str, ...] = (
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
)
DEFAULT_VISION_MODELS: tuple[str, ...] = (
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
    "llama-3.2-90b-vision-preview",
)

MAX_TOTAL_SECONDS = 90.0   # give up (and let the caller fall back) rather than keep someone waiting for minutes
MAX_KEY_WAIT_SECONDS = 8.0
DEFAULT_RATE_LIMIT_BLOCK = 20.0
MAX_RATE_LIMIT_BLOCK = 3600.0
FORBIDDEN_BLOCK = 600.0          # a 403 we cannot explain: pause that key for that model, not the whole key
MODEL_GONE_SECONDS = 6 * 3600.0  # a retired model is not asked again for this long (then it is probed once more)
MODEL_BLOCKED_SECONDS = 3600.0   # a model this account may not use (org setting, terms not accepted)


def parse_model_list(value: str | None, default: Iterable[str], *, first: str | None = None) -> tuple[str, ...]:
    """Parse a comma separated model list; ``first`` (e.g. a legacy single-model variable) goes in front."""
    models: list[str] = []
    for item in [first or "", *(value or "").split(",")]:
        item = item.strip()
        if item and item not in models:
            models.append(item)
    return tuple(models) if models else tuple(default)


# ── error classification ─────────────────────────────────────────────────────
def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


_MODEL_BLOCKED_MARKERS = (
    "permission_blocked", "model_permission", "blocked at the", "model_terms_required", "terms acceptance",
    "does not have access to model", "not have access to the model",
)


def classify_error(exc: BaseException) -> str:
    """Return one of: too_large, rate_limit, bad_key, model_blocked, forbidden, model_gone, bad_params, transient, fatal.

    ``bad_key``: Groq does not accept the key (401). ``model_blocked``: this account may not use this model (a 403
    about the model, or terms that were never accepted); the key is fine for other models. ``forbidden``: any other
    403, which says nothing about which models still work.
    """
    code = _status_code(exc)
    message = str(exc).lower()
    name = type(exc).__name__.lower()
    if code == 413 or "request too large" in message or "request_too_large" in message:
        return "too_large"
    if code == 429 or "rate_limit" in message or "ratelimit" in name:
        return "rate_limit"
    if any(marker in message for marker in _MODEL_BLOCKED_MARKERS):
        return "model_blocked"
    if code == 401 or "invalid api key" in message or "invalid_api_key" in message:
        return "bad_key"
    if code == 403:
        return "forbidden"
    if code == 404 or any(
        marker in message
        for marker in ("decommission", "model_not_found", "does not exist", "no longer supported", "model not found")
    ):
        return "model_gone"
    if code == 400 and ("frequency_penalty" in message or "presence_penalty" in message):
        return "bad_params"
    if (code is not None and code >= 500) or any(marker in name for marker in ("timeout", "connection")):
        return "transient"
    return "fatal"


def is_request_too_large(exc: BaseException) -> bool:
    return classify_error(exc) == "too_large"


_RETRY_IN_RE = re.compile(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def retry_after_seconds(exc: BaseException) -> float | None:
    """How long Groq asked us to wait (Retry-After header, else the 'try again in 1m2.5s' hint)."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        try:
            value = headers.get("retry-after")
            if value is not None:
                return min(float(value), MAX_RATE_LIMIT_BLOCK)
        except (TypeError, ValueError):
            pass
    match = _RETRY_IN_RE.search(str(exc))
    if match:
        total = sum(float(n) * _UNIT_SECONDS[u.lower()] for n, u in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", match.group(1), re.I))
        return min(total, MAX_RATE_LIMIT_BLOCK)
    return None


def extract_text(resp: Any) -> str:
    """Pull the reply text out of a chat completion; empty string if there is none."""
    try:
        content = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return (content or "").strip()


def _is_gpt_oss(model_id: str) -> bool:
    return "gpt-oss" in model_id


def _default_factory(api_key: str) -> Any:
    from groq import Groq

    return Groq(api_key=api_key, max_retries=0, timeout=45.0)


class RotatingGroq:
    """Round-robins API keys, skips rate-limited ones and falls back through a model list."""

    def __init__(
        self,
        api_keys: Iterable[str],
        chat_models: Iterable[str] = DEFAULT_CHAT_MODELS,
        vision_models: Iterable[str] = DEFAULT_VISION_MODELS,
        *,
        client_factory: Callable[[str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        keys = [key for key in api_keys if key]
        if not keys:
            raise ValueError("at least one Groq API key is required")
        factory = client_factory or _default_factory
        self._clients = [factory(key) for key in keys]
        self._models = {False: tuple(chat_models), True: tuple(vision_models)}
        if not self._models[False] or not self._models[True]:
            raise ValueError("model lists must not be empty")
        # model id -> monotonic time until which it is skipped (retired or blocked for this account)
        self._skip_until: dict[bool, dict[str, float]] = {False: {}, True: {}}
        # (key index, model id) -> monotonic time until which that pair is rate limited; model "*" blocks a whole key.
        self._blocked_until: dict[tuple[int, str], float] = {}
        self._cursor = 0
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock
        log.info("Groq: %d key(s); chat models=%s; vision models=%s", len(self._clients), self._models[False], self._models[True])

    # -- helpers -------------------------------------------------------------
    @property
    def key_count(self) -> int:
        return len(self._clients)

    def _skipped(self, vision: bool, model_id: str) -> bool:
        return self._skip_until[vision].get(model_id, 0.0) > self._clock()

    def current_model(self, vision: bool = False) -> str:
        """The model a new request starts with: the first one that is not currently marked retired / blocked."""
        models = self._models[vision]
        return next((m for m in models if not self._skipped(vision, m)), models[0])

    def _candidates(self, vision: bool, preferred: str | None) -> list[str]:
        """Configured order (``preferred`` first). Models marked retired / blocked go last, as a final resort."""
        ordered = list(self._models[vision])
        if preferred:
            if preferred in ordered:
                ordered.remove(preferred)
            ordered.insert(0, preferred)
        usable = [m for m in ordered if not self._skipped(vision, m)]
        return usable + [m for m in ordered if self._skipped(vision, m)]

    def _skip_model(self, vision: bool, model_id: str, seconds: float) -> None:
        with self._lock:
            self._skip_until[vision][model_id] = self._clock() + seconds

    def _blocked_for(self, idx: int, model_id: str) -> float:
        return max(self._blocked_until.get((idx, model_id), 0.0), self._blocked_until.get((idx, "*"), 0.0))

    def _pick_key(self, model_id: str) -> tuple[int | None, float]:
        """Next key usable for ``model_id``, or (None, seconds until one frees up)."""
        with self._lock:
            now = self._clock()
            for _ in range(len(self._clients)):
                idx = self._cursor % len(self._clients)
                self._cursor += 1
                if self._blocked_for(idx, model_id) <= now:
                    return idx, 0.0
            soonest = min(self._blocked_for(idx, model_id) for idx in range(len(self._clients)))
            return None, max(0.0, soonest - now)

    def _block(self, idx: int, model_id: str, seconds: float) -> None:
        with self._lock:
            self._blocked_until[(idx, model_id)] = self._clock() + max(1.0, min(seconds, MAX_RATE_LIMIT_BLOCK))

    def _forget_skip(self, vision: bool, model_id: str) -> None:
        """A request just worked on this model, so whatever kept it out no longer applies."""
        if self._skip_until[vision].get(model_id):
            with self._lock:
                self._skip_until[vision].pop(model_id, None)

    @staticmethod
    def _adapt_params(model_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if _is_gpt_oss(model_id):
            # Reasoning models spend completion tokens on thinking; keep it short and leave room for the answer.
            extra = dict(params.get("extra_body") or {})
            extra.setdefault("reasoning_effort", "low")
            params["extra_body"] = extra
            params["max_tokens"] = int(params.get("max_tokens", 512)) + 400
        return params

    # -- main entry point ----------------------------------------------------
    def call_with_retry(self, *, vision: bool = False, model: str | None = None, **kwargs: Any) -> Any:
        """Create a chat completion, rotating keys and models as needed. Blocking."""
        last_exc: BaseException | None = None
        deadline = self._clock() + MAX_TOTAL_SECONDS
        for model_id in self._candidates(vision, model):
            if self._clock() >= deadline:
                break
            params = self._adapt_params(model_id, dict(kwargs))
            keys_failed = 0
            transient_left = 2
            penalties_dropped = False
            while self._clock() < deadline:
                idx, wait = self._pick_key(model_id)
                if idx is None:
                    if wait > MAX_KEY_WAIT_SECONDS:
                        break
                    self._sleep(wait)
                    continue
                try:
                    resp = self._clients[idx].chat.completions.create(model=model_id, **params)
                except Exception as exc:  # noqa: BLE001 - classified below, re-raised when fatal
                    last_exc = exc
                    kind = classify_error(exc)
                    log.warning("Groq %s on key %d / model %s: %s", kind, idx + 1, model_id, str(exc)[:200])
                    if kind in ("rate_limit", "bad_key", "forbidden"):
                        if kind == "bad_key":
                            self._block(idx, "*", MAX_RATE_LIMIT_BLOCK)  # Groq rejects this key outright
                        elif kind == "forbidden":
                            self._block(idx, model_id, FORBIDDEN_BLOCK)  # unexplained: the other models may be fine
                        else:
                            self._block(idx, model_id, retry_after_seconds(exc) or DEFAULT_RATE_LIMIT_BLOCK)
                        keys_failed += 1
                        if keys_failed >= len(self._clients):
                            break
                        continue
                    if kind in ("model_gone", "model_blocked"):
                        self._skip_model(vision, model_id, MODEL_GONE_SECONDS if kind == "model_gone" else MODEL_BLOCKED_SECONDS)
                        break
                    if kind == "bad_params" and not penalties_dropped:
                        penalties_dropped = True
                        params.pop("frequency_penalty", None)
                        params.pop("presence_penalty", None)
                        continue
                    if kind == "transient":
                        if transient_left > 0:
                            transient_left -= 1
                            self._sleep(0.8)
                            continue
                        break  # still failing: try the next model instead of giving up on all of them
                    raise
                self._forget_skip(vision, model_id)
                return resp
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no Groq model could be used")
