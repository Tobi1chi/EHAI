from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ehai import new_id
from ehai.application.execution_policy import (
    ExecutionPolicy,
    ExecutionTimeoutKind,
    ExecutionWatchdog,
)
from ehai.domain.execution import Attempt, AttemptStatus

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_execution_policy_deadlines_are_opt_in_by_default() -> None:
    policy = ExecutionPolicy()

    assert policy.absolute_attempt_timeout is None
    assert policy.max_run_duration is None
    assert policy.heartbeat_lease > timedelta(0)
    assert policy.no_progress_timeout > timedelta(0)


def test_execution_policy_accepts_explicit_deadlines() -> None:
    policy = ExecutionPolicy(
        absolute_attempt_timeout=timedelta(seconds=17),
        max_run_duration=timedelta(hours=3),
    )

    assert policy.absolute_attempt_timeout == timedelta(seconds=17)
    assert policy.max_run_duration == timedelta(hours=3)
    with pytest.raises(ValueError, match="absolute_attempt_timeout"):
        ExecutionPolicy(absolute_attempt_timeout=timedelta(0))
    with pytest.raises(ValueError, match="max_run_duration"):
        ExecutionPolicy(max_run_duration=timedelta(-1))


def test_watchdog_still_enforces_explicit_attempt_deadline() -> None:
    policy = ExecutionPolicy(
        heartbeat_lease=timedelta(hours=1),
        no_progress_timeout=timedelta(hours=1),
        absolute_attempt_timeout=timedelta(seconds=60),
    )
    watchdog = ExecutionWatchdog(policy)
    attempt = _attempt(deadline_at=NOW - timedelta(seconds=1))

    decision = watchdog.evaluate(attempt, at=NOW)

    assert decision is not None
    assert decision.kind is ExecutionTimeoutKind.ABSOLUTE_DEADLINE


def test_watchdog_without_deadline_runs_on_lease_and_progress_only() -> None:
    policy = ExecutionPolicy(
        heartbeat_lease=timedelta(seconds=30),
        no_progress_timeout=timedelta(seconds=600),
    )
    watchdog = ExecutionWatchdog(policy)
    running = _attempt(
        lease_expires_at=NOW + timedelta(seconds=30),
        progress_at=NOW,
        heartbeat_at=NOW,
    )

    assert watchdog.evaluate(running, at=NOW) is None
    assert watchdog.next_check_at(running) == NOW + timedelta(seconds=30)

    idle = _attempt(
        lease_expires_at=NOW - timedelta(seconds=1),
        progress_at=NOW,
        heartbeat_at=NOW,
    )
    expired = watchdog.evaluate(idle, at=NOW)
    assert expired is not None
    assert expired.kind is ExecutionTimeoutKind.HEARTBEAT_LEASE


def _attempt(
    *,
    deadline_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    progress_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
) -> Attempt:
    return Attempt.rehydrate(
        run_id=new_id(),
        plan_node_id=new_id(),
        sequence=1,
        attempt_id=new_id(),
        status=AttemptStatus.RUNNING,
        artifact_ids=(),
        created_at=NOW - timedelta(minutes=5),
        started_at=NOW - timedelta(minutes=5),
        ended_at=None,
        progress_at=progress_at,
        heartbeat_at=heartbeat_at,
        deadline_at=deadline_at,
        lease_expires_at=lease_expires_at,
    )
