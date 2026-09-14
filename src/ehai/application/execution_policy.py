"""P2 execution timing, retry, health, budget, and replay policy."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ehai import ID, normalize_id
from ehai.application.ports import StoredEvent
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt
from ehai.domain.workers import AttemptActivity


class ExecutionTimeoutKind(StrEnum):
    """Independent timeout clocks enforced for one provider execution."""

    HEARTBEAT_LEASE = "heartbeat_lease"
    NO_PROGRESS = "no_progress"
    ABSOLUTE_DEADLINE = "absolute_deadline"


class RetrySafety(StrEnum):
    """Provider knowledge that determines whether replacement work is safe."""

    NO_EXECUTION_HANDLE = "no_execution_handle"
    SAFE_FAILURE = "safe_failure"
    EXECUTION_NOT_FOUND = "execution_not_found"
    LOCAL_ROLLBACK = "local_rollback"
    UNKNOWN = "unknown"

    @property
    def allows_retry(self) -> bool:
        return self is not RetrySafety.UNKNOWN


class EndpointHealthStatus(StrEnum):
    """Observed Endpoint reachability, separate from operator management state."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Bounded timing and Run-level resource policy for the P2 Runtime.

    ``absolute_attempt_timeout`` and ``max_run_duration`` are opt-in caller
    deadlines. They default to ``None`` so long-running high-reasoning model
    executions are bounded by heartbeat leases, no-progress observation, and
    explicit cancellation instead of a short wall-clock cutoff.
    """

    start_timeout: timedelta = timedelta(seconds=30)
    heartbeat_lease: timedelta = timedelta(minutes=2)
    no_progress_timeout: timedelta | None = timedelta(minutes=10)
    absolute_attempt_timeout: timedelta | None = None
    cancel_grace: timedelta = timedelta(seconds=2)
    max_run_duration: timedelta | None = None
    max_connector_calls: int | None = 100
    max_concurrency: int = 16
    max_provider_cost: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "start_timeout",
            "heartbeat_lease",
            "cancel_grace",
        ):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta")
        for name in ("absolute_attempt_timeout", "max_run_duration", "no_progress_timeout"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, timedelta) or value <= timedelta(0)):
                raise ValueError(f"{name} must be a positive timedelta or None")
        for name in ("max_concurrency",):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_connector_calls is not None and (
            type(self.max_connector_calls) is not int or self.max_connector_calls < 1
        ):
            raise ValueError("max_connector_calls must be a positive integer or None")
        if self.max_provider_cost is not None and (
            not isinstance(self.max_provider_cost, (int, float))
            or isinstance(self.max_provider_cost, bool)
            or not math.isfinite(self.max_provider_cost)
            or self.max_provider_cost < 0
        ):
            raise ValueError("max_provider_cost must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class TimeoutDecision:
    kind: ExecutionTimeoutKind
    reason: str


class ExecutionWatchdog:
    """Evaluate persisted Attempt clocks without treating waiting as progress."""

    def __init__(self, policy: ExecutionPolicy) -> None:
        self.policy = policy

    def evaluate(self, attempt: Attempt, *, at: datetime) -> TimeoutDecision | None:
        now = _utc(at)
        deadlines: list[tuple[datetime, TimeoutDecision]] = []
        if attempt.deadline_at is not None:
            deadlines.append(
                (
                    attempt.deadline_at,
                    TimeoutDecision(
                        ExecutionTimeoutKind.ABSOLUTE_DEADLINE,
                        "absolute Attempt deadline expired",
                    ),
                )
            )
        if attempt.lease_expires_at is not None:
            deadlines.append(
                (
                    attempt.lease_expires_at,
                    TimeoutDecision(
                        ExecutionTimeoutKind.HEARTBEAT_LEASE,
                        "Attempt heartbeat lease expired",
                    ),
                )
            )
        progress_at = attempt.progress_at or attempt.started_at
        if (
            self.policy.no_progress_timeout is not None
            and attempt.activity is not AttemptActivity.WAITING
            and progress_at is not None
        ):
            deadlines.append(
                (
                    progress_at + self.policy.no_progress_timeout,
                    TimeoutDecision(
                        ExecutionTimeoutKind.NO_PROGRESS,
                        "Attempt made no progress before its timeout",
                    ),
                )
            )
        expired = tuple(item for item in deadlines if now >= item[0])
        return None if not expired else min(expired, key=lambda item: item[0])[1]

    def next_check_at(self, attempt: Attempt) -> datetime:
        candidates: list[datetime] = []
        if attempt.deadline_at is not None:
            candidates.append(attempt.deadline_at)
        if attempt.lease_expires_at is not None:
            candidates.append(attempt.lease_expires_at)
        progress_at = attempt.progress_at or attempt.started_at
        if (
            self.policy.no_progress_timeout is not None
            and attempt.activity is not AttemptActivity.WAITING
            and progress_at is not None
        ):
            candidates.append(progress_at + self.policy.no_progress_timeout)
        if not candidates:
            raise ValueError(f"Attempt {attempt.attempt_id} has no watchdog clock")
        return min(candidates)


@dataclass(frozen=True, slots=True)
class EndpointHealth:
    """Latest health observation for one Worker Endpoint."""

    worker_endpoint_id: ID
    status: EndpointHealthStatus
    checked_at: datetime
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_endpoint_id", normalize_id(self.worker_endpoint_id))
        object.__setattr__(self, "status", EndpointHealthStatus(self.status))
        object.__setattr__(self, "checked_at", _utc(self.checked_at))
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("Endpoint health reason must not be blank")


@dataclass(frozen=True, slots=True)
class ExecutionBudgetUsage:
    """Run usage where unavailable provider cost remains explicitly unknown."""

    attempts: int
    connector_calls: int
    concurrent_executions: int
    provider_cost: float | None
    provider_cost_available: bool

    def __post_init__(self) -> None:
        for name in ("attempts", "connector_calls", "concurrent_executions"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.provider_cost_available, bool):
            raise ValueError("provider_cost_available must be a boolean")
        if self.provider_cost_available != (self.provider_cost is not None):
            raise ValueError("provider cost availability does not match its value")
        if self.provider_cost is not None and (
            not math.isfinite(self.provider_cost) or self.provider_cost < 0
        ):
            raise ValueError("provider_cost must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ExecutionReplayState:
    """Replayable facts needed to reconstruct dispatch and recovery decisions."""

    claimed_work_ids: dict[ID, ID] = field(default_factory=dict)
    dispatched_attempt_ids: set[ID] = field(default_factory=set)
    bindings: dict[ID, tuple[ID, ID, ID, str]] = field(default_factory=dict)
    retries: dict[ID, RetrySafety] = field(default_factory=dict)
    endpoint_health: dict[ID, EndpointHealthStatus] = field(default_factory=dict)


class ExecutionReplay:
    """Project scheduler/runtime facts from the durable Event Log."""

    def replay(self, events: Iterable[StoredEvent]) -> ExecutionReplayState:
        state = ExecutionReplayState()
        for stored in events:
            event = stored.event
            payload = event.payload
            if event.type is EventType.DISPATCH_WORK_CLAIMED and event.run_id is not None:
                work_id = payload.get("dispatch_work_id")
                if isinstance(work_id, str):
                    state.claimed_work_ids[event.run_id] = normalize_id(work_id)
            elif event.type is EventType.ATTEMPT_DISPATCHED:
                attempt_id = payload.get("attempt_id")
                if isinstance(attempt_id, str):
                    state.dispatched_attempt_ids.add(normalize_id(attempt_id))
            elif event.type is EventType.ATTEMPT_BOUND:
                self._replay_binding(state, payload)
            elif event.type is EventType.ATTEMPT_RETRY_SCHEDULED:
                attempt_id = payload.get("attempt_id")
                safety = payload.get("retry_safety")
                if isinstance(attempt_id, str) and isinstance(safety, str):
                    state.retries[normalize_id(attempt_id)] = RetrySafety(safety)
            elif event.type is EventType.ENDPOINT_HEALTH_CHANGED:
                endpoint_id = payload.get("worker_endpoint_id")
                status = payload.get("status")
                if isinstance(endpoint_id, str) and isinstance(status, str):
                    state.endpoint_health[normalize_id(endpoint_id)] = EndpointHealthStatus(status)
        return state

    @staticmethod
    def _replay_binding(state: ExecutionReplayState, payload: Mapping[str, object]) -> None:
        attempt_id = payload.get("attempt_id")
        profile_id = payload.get("worker_profile_id")
        endpoint_id = payload.get("worker_endpoint_id")
        session_id = payload.get("agent_session_ref_id")
        provider_execution_id = payload.get("provider_execution_id")
        if all(
            isinstance(value, str) for value in (attempt_id, profile_id, endpoint_id, session_id)
        ) and isinstance(provider_execution_id, str):
            assert isinstance(attempt_id, str)
            assert isinstance(profile_id, str)
            assert isinstance(endpoint_id, str)
            assert isinstance(session_id, str)
            state.bindings[normalize_id(attempt_id)] = (
                normalize_id(profile_id),
                normalize_id(endpoint_id),
                normalize_id(session_id),
                provider_execution_id,
            )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution policy timestamps must be timezone-aware")
    return value.astimezone(UTC)
