"""Run and worker-attempt state machines for the execution plane."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from ehai import ID, new_id, normalize_id, utc_now
from ehai.domain.workers import (
    AgentSessionRef,
    AttemptActivity,
    ExecutionHandle,
    WorkerEndpoint,
    WorkerProfile,
)

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
    predecessor_run_id: ID | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "goal_id", _validated_id(self.goal_id, "goal_id"))
        object.__setattr__(
            self,
            "plan_revision_id",
            _validated_id(self.plan_revision_id, "plan_revision_id"),
        )
        object.__setattr__(self, "run_id", _validated_id(self.run_id, "run_id"))
        if self.predecessor_run_id is not None:
            predecessor = _validated_id(self.predecessor_run_id, "predecessor_run_id")
            if predecessor == self.run_id:
                raise ValueError("Run cannot be its own predecessor")
            object.__setattr__(self, "predecessor_run_id", predecessor)
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
        predecessor_run_id: ID | None = None,
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
            predecessor_run_id=predecessor_run_id,
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
    worker_profile_id: ID | None = None
    worker_endpoint_id: ID | None = None
    agent_session_ref_id: ID | None = None
    execution_handle: ExecutionHandle | None = None
    activity: AttemptActivity | None = None
    event_cursor: str | None = None
    heartbeat_at: datetime | None = None
    progress_at: datetime | None = None
    deadline_at: datetime | None = None
    lease_expires_at: datetime | None = None
    queue_reason: str | None = None
    process_revision_id: ID | None = None
    _rehydrate_token: InitVar[object | None] = None

    def __post_init__(self, _rehydrate_token: object | None) -> None:
        object.__setattr__(self, "run_id", _validated_id(self.run_id, "run_id"))
        object.__setattr__(self, "plan_node_id", _validated_id(self.plan_node_id, "plan_node_id"))
        object.__setattr__(self, "attempt_id", _validated_id(self.attempt_id, "attempt_id"))
        object.__setattr__(
            self,
            "process_revision_id",
            _optional_validated_id(self.process_revision_id, "process_revision_id"),
        )
        object.__setattr__(
            self,
            "worker_profile_id",
            _optional_validated_id(self.worker_profile_id, "worker_profile_id"),
        )
        object.__setattr__(
            self,
            "worker_endpoint_id",
            _optional_validated_id(self.worker_endpoint_id, "worker_endpoint_id"),
        )
        object.__setattr__(
            self,
            "agent_session_ref_id",
            _optional_validated_id(self.agent_session_ref_id, "agent_session_ref_id"),
        )
        object.__setattr__(
            self,
            "activity",
            None if self.activity is None else AttemptActivity(self.activity),
        )
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
        object.__setattr__(self, "heartbeat_at", _optional_utc(self.heartbeat_at, "heartbeat_at"))
        object.__setattr__(self, "progress_at", _optional_utc(self.progress_at, "progress_at"))
        object.__setattr__(self, "deadline_at", _optional_utc(self.deadline_at, "deadline_at"))
        object.__setattr__(
            self,
            "lease_expires_at",
            _optional_utc(self.lease_expires_at, "lease_expires_at"),
        )
        if self.event_cursor is not None and (
            not isinstance(self.event_cursor, str) or not self.event_cursor.strip()
        ):
            raise ValueError(f"attempt {self.attempt_id}: event_cursor must not be blank")
        if self.queue_reason is not None and not self.queue_reason.strip():
            raise ValueError(f"attempt {self.attempt_id}: queue_reason must not be blank")
        _validate_reason(self.outcome_reason, "outcome_reason")
        if self.status is not AttemptStatus.PENDING and _rehydrate_token is not _REHYDRATE:
            raise ValueError(
                f"attempt {self.attempt_id}: non-pending state must use a transition or rehydrate()"
            )
        self._validate_lifecycle()
        self._validate_runtime_binding()

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
        worker_profile_id: ID | None = None,
        worker_endpoint_id: ID | None = None,
        agent_session_ref_id: ID | None = None,
        execution_handle: ExecutionHandle | None = None,
        activity: AttemptActivity | None = None,
        event_cursor: str | None = None,
        heartbeat_at: datetime | None = None,
        progress_at: datetime | None = None,
        deadline_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
        queue_reason: str | None = None,
        process_revision_id: ID | None = None,
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
            worker_profile_id=worker_profile_id,
            worker_endpoint_id=worker_endpoint_id,
            agent_session_ref_id=agent_session_ref_id,
            execution_handle=execution_handle,
            activity=activity,
            event_cursor=event_cursor,
            heartbeat_at=heartbeat_at,
            progress_at=progress_at,
            deadline_at=deadline_at,
            lease_expires_at=lease_expires_at,
            queue_reason=queue_reason,
            process_revision_id=process_revision_id,
            _rehydrate_token=_REHYDRATE,
        )

    def queue(self, reason: str) -> Self:
        """Observe a pending Attempt waiting without consuming Worker capacity."""
        if self.status is not AttemptStatus.PENDING:
            raise ValueError(f"attempt {self.attempt_id}: only pending work can be queued")
        if not reason.strip():
            raise ValueError("queue reason must not be blank")
        return replace(
            self,
            activity=AttemptActivity.QUEUED,
            queue_reason=reason,
            _rehydrate_token=_REHYDRATE,
        )

    def assign(
        self,
        *,
        profile: WorkerProfile,
        endpoint: WorkerEndpoint,
        session: AgentSessionRef,
        deadline_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
    ) -> Self:
        """Bind an immutable Worker, Endpoint, and Session allocation once."""
        if self.status not in {AttemptStatus.PENDING, AttemptStatus.RUNNING}:
            raise ValueError(f"attempt {self.attempt_id}: terminal work cannot be assigned")
        if self.worker_profile_id is not None:
            raise ValueError(f"attempt {self.attempt_id}: assignment is immutable")
        if session.run_id != self.run_id:
            raise ValueError(f"attempt {self.attempt_id}: Session belongs to another Run")
        if (
            session.worker_profile_id != profile.worker_profile_id
            or session.worker_endpoint_id != endpoint.worker_endpoint_id
        ):
            raise ValueError(f"attempt {self.attempt_id}: Session allocation does not match")
        if profile.kind != endpoint.worker_kind:
            raise ValueError(f"attempt {self.attempt_id}: Worker and Endpoint kinds do not match")
        return replace(
            self,
            worker_profile_id=profile.worker_profile_id,
            worker_endpoint_id=endpoint.worker_endpoint_id,
            agent_session_ref_id=session.agent_session_ref_id,
            activity=AttemptActivity.QUEUED,
            deadline_at=deadline_at,
            lease_expires_at=lease_expires_at,
            queue_reason=self.queue_reason,
            _rehydrate_token=_REHYDRATE,
        )

    def bind_execution(self, execution_handle: ExecutionHandle) -> Self:
        """Attach exactly one provider execution to an assigned Attempt."""
        if self.worker_profile_id is None or self.agent_session_ref_id is None:
            raise ValueError(f"attempt {self.attempt_id}: execution requires an assignment")
        if self.execution_handle is not None:
            raise ValueError(f"attempt {self.attempt_id}: execution binding is immutable")
        if execution_handle.attempt_id != self.attempt_id:
            raise ValueError(f"attempt {self.attempt_id}: execution belongs to another Attempt")
        if execution_handle.agent_session_ref_id != self.agent_session_ref_id:
            raise ValueError(f"attempt {self.attempt_id}: execution belongs to another Session")
        return replace(
            self,
            execution_handle=execution_handle,
            _rehydrate_token=_REHYDRATE,
        )

    def observe(
        self,
        activity: AttemptActivity,
        *,
        event_cursor: str | None = None,
        heartbeat_at: datetime | None = None,
        progress_at: datetime | None = None,
        lease_expires_at: datetime | None = None,
    ) -> Self:
        """Record provider activity without changing the Attempt lifecycle."""
        if self.status not in {AttemptStatus.PENDING, AttemptStatus.RUNNING}:
            raise ValueError(f"attempt {self.attempt_id}: terminal work has no live activity")
        if self.worker_profile_id is None:
            raise ValueError(f"attempt {self.attempt_id}: activity requires an assignment")
        return replace(
            self,
            activity=AttemptActivity(activity),
            event_cursor=event_cursor,
            heartbeat_at=heartbeat_at,
            progress_at=progress_at,
            lease_expires_at=lease_expires_at,
            _rehydrate_token=_REHYDRATE,
        )

    def record_terminal_cursor(self, event_cursor: str) -> Self:
        """Attach the last durably applied provider cursor to terminal work."""
        if self.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}:
            raise ValueError(f"attempt {self.attempt_id}: live work must use observe")
        if not isinstance(event_cursor, str) or not event_cursor.strip():
            raise ValueError(f"attempt {self.attempt_id}: event_cursor must not be blank")
        return replace(
            self,
            event_cursor=event_cursor,
            _rehydrate_token=_REHYDRATE,
        )

    def extend_deadline(self, deadline_at: datetime) -> Self:
        """Extend, but never shorten, the absolute deadline of running work."""
        if self.status is not AttemptStatus.RUNNING:
            raise ValueError(f"attempt {self.attempt_id}: only running work has a deadline")
        deadline = _utc(deadline_at, "deadline_at")
        if self.deadline_at is None:
            raise ValueError(f"attempt {self.attempt_id}: deadline is not initialized")
        if deadline <= self.deadline_at:
            raise ValueError(f"attempt {self.attempt_id}: deadline extension must move forward")
        return replace(
            self,
            deadline_at=deadline,
            _rehydrate_token=_REHYDRATE,
        )

    def start(self, *, at: datetime | None = None) -> Self:
        """Start a pending Attempt."""
        self._ensure_transition(AttemptStatus.RUNNING)
        return replace(
            self,
            status=AttemptStatus.RUNNING,
            activity=(None if self.worker_profile_id is None else AttemptActivity.RUNNING),
            started_at=_utc(at or utc_now(), "at"),
            ended_at=None,
            outcome_reason=None,
            queue_reason=None,
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
            activity=None,
            lease_expires_at=None,
            queue_reason=None,
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
            activity=None,
            lease_expires_at=None,
            queue_reason=None,
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
            activity=None,
            lease_expires_at=None,
            queue_reason=None,
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

    def _validate_runtime_binding(self) -> None:
        assignment = (
            self.worker_profile_id,
            self.worker_endpoint_id,
            self.agent_session_ref_id,
        )
        if any(value is None for value in assignment) and any(
            value is not None for value in assignment
        ):
            raise ValueError(f"attempt {self.attempt_id}: assignment must be complete")
        if self.execution_handle is not None:
            if self.agent_session_ref_id is None:
                raise ValueError(f"attempt {self.attempt_id}: execution requires assignment")
            if self.execution_handle.attempt_id != self.attempt_id:
                raise ValueError(f"attempt {self.attempt_id}: execution belongs to another Attempt")
            if self.execution_handle.agent_session_ref_id != self.agent_session_ref_id:
                raise ValueError(f"attempt {self.attempt_id}: execution belongs to another Session")
        if (
            self.activity is not None
            and self.activity is not AttemptActivity.QUEUED
            and self.worker_profile_id is None
        ):
            raise ValueError(f"attempt {self.attempt_id}: activity requires assignment")
        if self.status not in {AttemptStatus.PENDING, AttemptStatus.RUNNING} and self.activity:
            raise ValueError(f"attempt {self.attempt_id}: terminal work has no live activity")
        for field_name in (
            "heartbeat_at",
            "progress_at",
            "deadline_at",
            "lease_expires_at",
        ):
            timestamp = getattr(self, field_name)
            if timestamp is not None and timestamp < self.created_at:
                raise ValueError(f"attempt {self.attempt_id}: {field_name} precedes created_at")


def _validated_id(value: ID, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"{field_name}: {error}") from error


def _optional_validated_id(value: ID | None, field_name: str) -> ID | None:
    return None if value is None else _validated_id(value, field_name)


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
