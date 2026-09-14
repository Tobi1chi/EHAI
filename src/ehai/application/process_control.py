"""Transactional freshness checks for host-coordinated process continuation.

A guard preserves an observed control decision; it does not authorize automatic
planning or prove that a failed execution is safe to replace.
"""

from __future__ import annotations

from dataclasses import dataclass

from ehai import ID, normalize_id
from ehai.application.ports import StateConflictError, StoredEvent, UnitOfWork
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanRevision

_CONTROL_EVENTS = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_PAUSED,
        EventType.RUN_RESUMED,
        EventType.RUN_CANCELLED,
        EventType.RUN_FAILED,
        EventType.RUN_COMPLETED,
        EventType.CHECKPOINT_RESTORED,
    }
)


@dataclass(frozen=True, slots=True)
class ProcessControlGuard:
    """The exact pause and process observed before asynchronous planning work."""

    run_id: ID
    pause_event_id: ID
    process_revision_id: ID

    def __post_init__(self) -> None:
        for field in ("run_id", "pause_event_id", "process_revision_id"):
            object.__setattr__(self, field, normalize_id(getattr(self, field)))


def latest_run_control(uow: UnitOfWork, run_id: ID) -> StoredEvent | None:
    """Return the latest durable control fact, not the latest free-text reason."""
    return max(
        (
            stored
            for stored in uow.events.list_events()
            if stored.event.run_id == run_id and stored.event.type in _CONTROL_EVENTS
        ),
        key=lambda stored: stored.offset,
        default=None,
    )


def run_contract_is_current(uow: UnitOfWork, run: Run, plan: PlanRevision) -> bool:
    """Return whether a Run's execution graph still uses Goal's current contract.

    A Goal may advance to a newly approved CompletionContract while a paused
    predecessor is retained for a later successor Run.  Such a predecessor
    remains useful historical input, but its old graph must not be resumed or
    used to recover persisted candidates under the new approval boundary.
    """

    goal = uow.states.get_goal(run.goal_id)
    if goal is None:
        return False
    contract = goal.completion_contract
    return (
        plan.goal_id == run.goal_id
        and plan.plan_revision_id == run.plan_revision_id
        and contract is not None
        and contract.is_confirmed
        and contract.goal_id == run.goal_id
        and contract.completion_contract_id == plan.completion_contract_id
        and contract.version == plan.completion_contract_version
    )


def require_process_control(uow: UnitOfWork, run_id: ID, guard: ProcessControlGuard) -> None:
    """Check inside the same transaction as the guarded apply/resume write."""
    if guard.run_id != run_id:
        raise StateConflictError("Process continuation guard belongs to another Run")
    run = uow.states.get_run(run_id)
    if run is None or run.status is not RunStatus.PAUSED:
        raise StateConflictError("Process continuation requires the observed paused Run")
    if any(
        attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
        for attempt in uow.states.list_attempts(run_id)
    ):
        raise StateConflictError("Process continuation requires drained Attempts")
    control = latest_run_control(uow, run_id)
    if (
        control is None
        or control.event.type is not EventType.RUN_PAUSED
        or control.event.id != guard.pause_event_id
    ):
        raise StateConflictError("Run control changed while process adjustment was in progress")
    process = uow.states.get_active_process_revision(run_id)
    if process is None or process.process_revision_id != guard.process_revision_id:
        raise StateConflictError("Active process changed while continuation was being prepared")
