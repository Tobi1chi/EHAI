"""Structured causes for persisted RunPaused facts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from ehai import ID, JsonValue


class PauseCause(StrEnum):
    """Why a RunPaused fact was persisted.

    ``UNKNOWN`` is reserved for legacy events that predate ``pause_cause`` or
    contain an unrecognised value.  Writers must use one of the explicit
    causes; readers must not infer a legacy cause from an old reason string.
    """

    OPERATOR = "operator"
    WORKER_WAITING = "worker_waiting"
    RUNTIME_IDLE = "runtime_idle"
    RUNTIME_ERROR = "runtime_error"
    STARTUP_RECOVERY = "startup_recovery"
    UNKNOWN_EXECUTION = "unknown_execution"
    BRANCH_EXHAUSTED = "branch_exhausted"
    RETRY_EXHAUSTED = "retry_exhausted"
    GOAL_BUDGET_EXHAUSTED = "goal_budget_exhausted"
    REVIEW_REWORK = "review_rework"
    UNKNOWN = "unknown"


def require_pause_cause(value: object) -> PauseCause:
    """Validate an application-owned pause cause before writing an event."""
    if not isinstance(value, PauseCause):
        raise TypeError("pause_cause must be an explicit PauseCause")
    if value is PauseCause.UNKNOWN:
        raise ValueError("pause_cause must be an explicit PauseCause")
    return value


def pause_cause_from_payload(payload: Mapping[str, JsonValue]) -> PauseCause:
    """Read a cause without guessing when the persisted field is absent/invalid."""
    value = payload.get("pause_cause")
    if not isinstance(value, str):
        return PauseCause.UNKNOWN
    try:
        return PauseCause(value)
    except ValueError:
        return PauseCause.UNKNOWN


def pause_event_payload(
    run_id: ID,
    cause: PauseCause,
    *,
    reason: str | None = None,
    **extra: JsonValue,
) -> dict[str, JsonValue]:
    """Build the common payload for a persisted RunPaused fact."""
    payload: dict[str, JsonValue] = dict(extra)
    if reason is not None:
        payload["reason"] = reason
    payload["run_id"] = run_id
    payload["pause_cause"] = require_pause_cause(cause).value
    return payload
