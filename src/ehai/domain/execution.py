"""Run and worker-attempt state machines for the execution plane."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from ehai import ID, new_id, normalize_id, utc_now

if TYPE_CHECKING:
    from ehai.domain.checking import GateDecision


_REHYDRATE = object()


class RunStatus(StrEnum):
    """Lifecycle states for one execution of a PlanRevision."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    """Lifecycle states for one Worker invocation."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class InvalidRunTransition(ValueError):
    """Raised when a Run is asked to perform an illegal state transition."""

    def __init__(self, run_id: ID, source: RunStatus, target: RunStatus) -> None:
        self.run_id = run_id
        self.source = source
        self.target = target
        super().__init__(f"run {run_id}: cannot transition from {source.value} to {target.value}")


class InvalidAttemptTransition(ValueError):
    """Raised when an Attempt is asked to perform an illegal state transition."""

    def __init__(
        self,
        attempt_id: ID,
        run_id: ID,
        plan_node_id: ID,
        source: AttemptStatus,
        target: AttemptStatus,
    ) -> None:
        self.attempt_id = attempt_id
        self.run_id = run_id
        self.plan_node_id = plan_node_id
        self.source = source
        self.target = target
        super().__init__(
            f"attempt {attempt_id} for run {run_id} and node {plan_node_id}: "
            f"cannot transition from {source.value} to {target.value}"
        )


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.PAUSED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

_ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.PENDING: frozenset({AttemptStatus.RUNNING, AttemptStatus.CANCELLED}),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.INTERRUPTED,
        }
    ),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.INTERRUPTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Run:
    """An immutable snapshot of one PlanRevision execution."""

    goal_id: ID
    plan_revision_id: ID
    run_id: ID = field(default_factory=new_id)
    status: RunStatus = RunStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status_reason: str | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "goal_id", _validated_id(self.goal_id, "goal_id"))
        object.__setattr__(
            self,
            "plan_revision_id",
            _validated_id(self.plan_revision_id, "plan_revision_id"),
        )
        object.__setattr__(self, "run_id", _validated_id(self.run_id, "run_id"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "started_at", _optional_utc(self.started_at, "started_at"))
        object.__setattr__(self, "ended_at", _optional_utc(self.ended_at, "ended_at"))
        _validate_reason(self.status_reason, "status_reason")
        if self.status is not RunStatus.PENDING and _rehydrate_token is not _REHYDRATE:
            raise ValueError(
                f"run {self.run_id}: non-pending state must use a transition or rehydrate()"
            )
        self._validate_lifecycle()

    @classmethod
    def rehydrate(
        cls,
        *,
        goal_id: ID,
        plan_revision_id: ID,
        run_id: ID,
        status: RunStatus,
        created_at: datetime,
        started_at: datetime | None,
        ended_at: datetime | None,
        status_reason: str | None = None,
    ) -> Self:
        """Restore a persisted Run snapshot through an explicit validation boundary."""
        return cls(
            goal_id=goal_id,
            plan_revision_id=plan_revision_id,
            run_id=run_id,
            status=status,
            created_at=created_at,
            started_at=started_at,
            ended_at=ended_at,
            status_reason=status_reason,
            _rehydrate_token=_REHYDRATE,
        )

    def start(self, *, at: datetime | None = None) -> Self:
        """Start a pending Run."""
        timestamp = _utc(at or utc_now(), "at")
        self._ensure_transition(RunStatus.RUNNING)
        return replace(
            self,
            status=RunStatus.RUNNING,
            started_at=timestamp,
            ended_at=None,
            status_reason=None,
            _rehydrate_token=_REHYDRATE,
        )

    def pause(self) -> Self:
        """Pause a running Run without changing its execution timestamps."""
        self._ensure_transition(RunStatus.PAUSED)
        return replace(self, status=RunStatus.PAUSED, _rehydrate_token=_REHYDRATE)

    def resume(self) -> Self:
        """Resume a paused Run."""
        self._ensure_transition(RunStatus.RUNNING)
        return replace(self, status=RunStatus.RUNNING, _rehydrate_token=_REHYDRATE)

    def complete(self, decision: GateDecision, *, at: datetime | None = None) -> Self:
        """Mark a running Run complete after application-level final Gate approval."""
        self._ensure_transition(RunStatus.COMPLETED)
        if not decision.passed or not decision.evidence_artifact_ids:
            raise ValueError(f"run {self.run_id}: Gate {decision.gate_id} must pass with evidence")
        if decision.run_id != self.run_id:
            raise ValueError(
                f"run {self.run_id}: Gate {decision.gate_id} belongs to run {decision.run_id}"
            )
        return replace(
            self,
            status=RunStatus.COMPLETED,
            ended_at=_utc(at or utc_now(), "at"),
            status_reason=None,
            _rehydrate_token=_REHYDRATE,
        )

    def fail(self, reason: str, *, at: datetime | None = None) -> Self:
        """Fail a running or paused Run with an auditable reason."""
        _require_reason(reason)
        self._ensure_transition(RunStatus.FAILED)
        return replace(
            self,
            status=RunStatus.FAILED,
            ended_at=_utc(at or utc_now(), "at"),
            status_reason=reason,
            _rehydrate_token=_REHYDRATE,
        )

    def cancel(self, reason: str | None = None, *, at: datetime | None = None) -> Self:
        """Cancel a non-terminal Run."""
        _validate_reason(reason, "reason")
        self._ensure_transition(RunStatus.CANCELLED)
        return replace(
            self,
            status=RunStatus.CANCELLED,
            ended_at=_utc(at or utc_now(), "at"),
            status_reason=reason,
            _rehydrate_token=_REHYDRATE,
        )

    def _ensure_transition(self, target: RunStatus) -> None:
        if target not in _RUN_TRANSITIONS[self.status]:
            raise InvalidRunTransition(self.run_id, self.status, target)

    def _validate_lifecycle(self) -> None:
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError(f"run {self.run_id}: started_at precedes created_at")
        if self.ended_at is not None:
            lower_bound = self.started_at or self.created_at
            if self.ended_at < lower_bound:
                raise ValueError(f"run {self.run_id}: ended_at precedes its lifecycle start")

        if self.status is RunStatus.PENDING:
            if self.started_at is not None or self.ended_at is not None:
                raise ValueError(f"run {self.run_id}: pending run cannot have lifecycle timestamps")
        elif self.status in {RunStatus.RUNNING, RunStatus.PAUSED}:
            if self.started_at is None or self.ended_at is not None:
                raise ValueError(
                    f"run {self.run_id}: {self.status.value} run requires only started_at"
                )
        elif self.status is RunStatus.CANCELLED and self.started_at is None:
            if self.ended_at is None:
                raise ValueError(f"run {self.run_id}: cancelled run requires ended_at")
        elif self.started_at is None or self.ended_at is None:
            raise ValueError(
                f"run {self.run_id}: {self.status.value} run requires start and end timestamps"
            )

        if self.status is RunStatus.FAILED:
            _require_reason(self.status_reason)
        elif self.status is not RunStatus.CANCELLED and self.status_reason is not None:
            raise ValueError(
                f"run {self.run_id}: {self.status.value} run cannot have a status reason"
            )


@dataclass(frozen=True, slots=True)
class Attempt:
    """An immutable snapshot of one Worker attempt for a PlanNode.

    ``succeeded`` means only that the Worker returned normally. It deliberately
    does not carry or mutate PlanNode completion state; checks and a Gate decide
    whether the candidate can complete the node.
    """

    run_id: ID
    plan_node_id: ID
    sequence: int
    attempt_id: ID = field(default_factory=new_id)
    status: AttemptStatus = AttemptStatus.PENDING
    artifact_ids: tuple[ID, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    outcome_reason: str | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "run_id", _validated_id(self.run_id, "run_id"))
        object.__setattr__(self, "plan_node_id", _validated_id(self.plan_node_id, "plan_node_id"))
        object.__setattr__(self, "attempt_id", _validated_id(self.attempt_id, "attempt_id"))
        if self.sequence < 1:
            raise ValueError(f"attempt {self.attempt_id}: sequence must be positive")
        object.__setattr__(
            self,
            "artifact_ids",
            _validated_ids(self.artifact_ids, "artifact_ids", allow_empty=True),
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "started_at", _optional_utc(self.started_at, "started_at"))
        object.__setattr__(self, "ended_at", _optional_utc(self.ended_at, "ended_at"))
        _validate_reason(self.outcome_reason, "outcome_reason")
        if self.status is not AttemptStatus.PENDING and _rehydrate_token is not _REHYDRATE:
            raise ValueError(
                f"attempt {self.attempt_id}: non-pending state must use a transition or rehydrate()"
            )
        self._validate_lifecycle()

    @classmethod
    def rehydrate(
        cls,
        *,
        run_id: ID,
        plan_node_id: ID,
        sequence: int,
        attempt_id: ID,
        status: AttemptStatus,
        artifact_ids: tuple[ID, ...],
        created_at: datetime,
        started_at: datetime | None,
        ended_at: datetime | None,
        outcome_reason: str | None = None,
    ) -> Self:
        """Restore a persisted Attempt snapshot through an explicit validation boundary."""
        return cls(
            run_id=run_id,
            plan_node_id=plan_node_id,
            sequence=sequence,
            attempt_id=attempt_id,
            status=status,
            artifact_ids=artifact_ids,
            created_at=created_at,
            started_at=started_at,
            ended_at=ended_at,
            outcome_reason=outcome_reason,
            _rehydrate_token=_REHYDRATE,
        )

    def start(self, *, at: datetime | None = None) -> Self:
        """Start a pending Attempt."""
        self._ensure_transition(AttemptStatus.RUNNING)
        return replace(
            self,
            status=AttemptStatus.RUNNING,
            started_at=_utc(at or utc_now(), "at"),
            ended_at=None,
            outcome_reason=None,
            _rehydrate_token=_REHYDRATE,
        )

    def succeed(
        self,
        artifact_ids: tuple[ID, ...] = (),
        *,
        at: datetime | None = None,
    ) -> Self:
        """Record normal Worker return and immutable candidate Artifact references."""
        self._ensure_transition(AttemptStatus.SUCCEEDED)
        return replace(
            self,
            status=AttemptStatus.SUCCEEDED,
            artifact_ids=_validated_ids(artifact_ids, "artifact_ids", allow_empty=True),
            ended_at=_utc(at or utc_now(), "at"),
            outcome_reason=None,
            _rehydrate_token=_REHYDRATE,
        )

    def fail(self, reason: str, *, at: datetime | None = None) -> Self:
        """Record a Worker failure."""
        _require_reason(reason)
        return self._terminal_failure(AttemptStatus.FAILED, reason, at)

    def time_out(self, reason: str, *, at: datetime | None = None) -> Self:
        """Record an Attempt timeout."""
        _require_reason(reason)
        return self._terminal_failure(AttemptStatus.TIMED_OUT, reason, at)

    def cancel(self, reason: str | None = None, *, at: datetime | None = None) -> Self:
        """Cancel a pending or running Attempt."""
        _validate_reason(reason, "reason")
        self._ensure_transition(AttemptStatus.CANCELLED)
        return replace(
            self,
            status=AttemptStatus.CANCELLED,
            ended_at=_utc(at or utc_now(), "at"),
            outcome_reason=reason,
            _rehydrate_token=_REHYDRATE,
        )

    def interrupt(self, reason: str, *, at: datetime | None = None) -> Self:
        """Mark a running Attempt interrupted during process recovery."""
        _require_reason(reason)
        return self._terminal_failure(AttemptStatus.INTERRUPTED, reason, at)

    def _terminal_failure(
        self,
        status: AttemptStatus,
        reason: str,
        at: datetime | None,
    ) -> Self:
        self._ensure_transition(status)
        return replace(
            self,
            status=status,
            ended_at=_utc(at or utc_now(), "at"),
            outcome_reason=reason,
            _rehydrate_token=_REHYDRATE,
        )

    def _ensure_transition(self, target: AttemptStatus) -> None:
        if target not in _ATTEMPT_TRANSITIONS[self.status]:
            raise InvalidAttemptTransition(
                self.attempt_id,
                self.run_id,
                self.plan_node_id,
                self.status,
                target,
            )

    def _validate_lifecycle(self) -> None:
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError(f"attempt {self.attempt_id}: started_at precedes created_at")
        if self.ended_at is not None:
            lower_bound = self.started_at or self.created_at
            if self.ended_at < lower_bound:
                raise ValueError(f"attempt {self.attempt_id}: ended_at precedes lifecycle start")

        if self.status is AttemptStatus.PENDING:
            if self.started_at is not None or self.ended_at is not None:
                raise ValueError(
                    f"attempt {self.attempt_id}: pending attempt cannot have lifecycle timestamps"
                )
        elif self.status is AttemptStatus.RUNNING:
            if self.started_at is None or self.ended_at is not None:
                raise ValueError(
                    f"attempt {self.attempt_id}: running attempt requires only started_at"
                )
        elif self.status is AttemptStatus.CANCELLED and self.started_at is None:
            if self.ended_at is None:
                raise ValueError(f"attempt {self.attempt_id}: cancelled attempt requires ended_at")
        elif self.started_at is None or self.ended_at is None:
            raise ValueError(
                f"attempt {self.attempt_id}: {self.status.value} requires start and end timestamps"
            )

        failed_statuses = {
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.INTERRUPTED,
        }
        if self.status in failed_statuses:
            _require_reason(self.outcome_reason)
        elif self.status is not AttemptStatus.CANCELLED and self.outcome_reason is not None:
            raise ValueError(
                f"attempt {self.attempt_id}: {self.status.value} cannot have an outcome reason"
            )
        if self.status is not AttemptStatus.SUCCEEDED and self.artifact_ids:
            raise ValueError(
                f"attempt {self.attempt_id}: only a succeeded attempt can reference artifacts"
            )


def _validated_id(value: ID, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{field_name}: {error}") from error


def _validated_ids(
    values: tuple[ID, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[ID, ...]:
    normalized = tuple(_validated_id(value, field_name) for value in values)
    if not allow_empty and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None, field_name: str) -> datetime | None:
    return None if value is None else _utc(value, field_name)


def _validate_reason(reason: str | None, field_name: str) -> None:
    if reason is not None and not reason.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_reason(reason: str | None) -> None:
    if reason is None or not reason.strip():
        raise ValueError("reason must not be blank")
