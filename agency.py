"""Validated action proposals for bounded autonomy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    NO_ACTION = "NO_ACTION"
    WRITE_REFLECTION = "WRITE_REFLECTION"
    SEND_PROACTIVE_MESSAGE = "SEND_PROACTIVE_MESSAGE"


@dataclass(frozen=True)
class ActionProposal:
    action: ActionType
    reason: str
    user_id: int | None = None
    channel_id: int | None = None
    goal_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class AgencyPolicy:
    """Application-owned validator.  Model output cannot bypass this class."""

    def validate(self, raw: dict[str, Any], *, permission_allowed: bool = True,
                 user_opted_in: bool = True, muted: bool = False,
                 channel_sendable: bool = True) -> ActionProposal:
        try:
            action = ActionType(str(raw.get("action", "NO_ACTION")).upper())
        except ValueError as exc:
            raise ValueError("action is not allowlisted") from exc
        reason = str(raw.get("reason", "")).strip()[:500]
        if not reason and action is not ActionType.NO_ACTION:
            raise ValueError("non-empty reason required")
        user_id = _optional_positive_int(raw.get("user_id"))
        channel_id = _optional_positive_int(raw.get("channel_id"))
        goal_id = _optional_positive_int(raw.get("goal_id"))
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        payload = {str(k)[:60]: _safe_scalar(v) for k, v in list(payload.items())[:10]}

        if not permission_allowed and action is not ActionType.NO_ACTION:
            raise PermissionError("application permission denied")
        if action is ActionType.SEND_PROACTIVE_MESSAGE:
            if not user_id or not channel_id:
                raise ValueError("proactive actions require user_id and channel_id")
            if muted or not user_opted_in or not channel_sendable:
                raise PermissionError("proactive target is not eligible")
        return ActionProposal(action, reason, user_id, channel_id, goal_id, payload)


def no_action(reason: str) -> ActionProposal:
    return ActionProposal(ActionType.NO_ACTION, reason[:500])


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    converted = int(value)
    if converted <= 0:
        raise ValueError("IDs must be positive")
    return converted


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:500] if isinstance(value, str) else value
    return str(value)[:500]
