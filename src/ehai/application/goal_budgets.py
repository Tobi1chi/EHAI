"""Goal-scoped Worker Attempt budgets and their durable usage queries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ehai import ID, JsonValue
from ehai.application.ports import ReadSession, StateConflictError, StoredEvent, UnitOfWork
from ehai.domain.events import EventType

_BUDGET_KEY = "goal_worker_budget"
_BUDGET_KEYS = frozenset({"max_worker_attempts"})


@dataclass(frozen=True, slots=True)
class GoalWorkerBudget:
    """Immutable Goal-level Worker Attempt budget."""

    max_worker_attempts: int

    def __post_init__(self) -> None:
        if type(self.max_worker_attempts) is not int or self.max_worker_attempts < 1:
            raise ValueError("max_worker_attempts must be a positive integer")

    @classmethod
    def from_document(cls, document: Mapping[str, JsonValue]) -> GoalWorkerBudget:
        """Parse the closed JSON object used for Goal Worker budget authorization."""

        if not isinstance(document, Mapping):
            raise ValueError("goal_worker_budget must be an object")
        keys = set(document)
        if keys != _BUDGET_KEYS:
            missing = sorted(_BUDGET_KEYS - keys)
            extra = sorted(keys - _BUDGET_KEYS)
            raise ValueError(f"invalid goal_worker_budget keys; missing={missing}, extra={extra}")
        max_worker_attempts = document["max_worker_attempts"]
        if type(max_worker_attempts) is not int or max_worker_attempts < 1:
            raise ValueError("goal_worker_budget.max_worker_attempts must be a positive integer")
        return cls(max_worker_attempts=max_worker_attempts)

    def to_document(self) -> dict[str, JsonValue]:
        """Return the closed JSON object used for authorization."""

        return {"max_worker_attempts": self.max_worker_attempts}


def goal_worker_budget(
    uow: UnitOfWork | ReadSession,
    goal_id: ID,
) -> GoalWorkerBudget | None:
    """Read one unambiguous Worker budget authorized for a Goal.

    Run ownership comes from current state.  Event payloads cannot associate a
    Run with a different Goal merely by claiming a ``goal_id`` of their own.
    """

    run_ids = {run.run_id for run in uow.states.list_runs(goal_id)}
    if not run_ids:
        return None

    found: GoalWorkerBudget | None = None
    for stored in uow.events.list_events():
        event = stored.event
        if event.type is not EventType.RUN_STARTED or event.run_id not in run_ids:
            continue
        candidate = _budget_from_run_started(stored)
        if candidate is None:
            continue
        if found is None:
            found = candidate
        elif found != candidate:
            raise StateConflictError(
                f"Goal {goal_id} has conflicting goal_worker_budget authorizations"
            )
    return found


def validate_goal_worker_budget(
    uow: UnitOfWork | ReadSession,
    goal_id: ID,
    document: Mapping[str, JsonValue] | None,
) -> GoalWorkerBudget | None:
    """Validate a new optional budget against the Goal's retained policy.

    An omitted document inherits the existing policy.  A supplied document may
    establish a policy only when none exists, or repeat the exact same policy.
    """

    existing = goal_worker_budget(uow, goal_id)
    if document is None:
        return existing
    try:
        candidate = GoalWorkerBudget.from_document(document)
    except (TypeError, ValueError) as error:
        raise StateConflictError("invalid goal_worker_budget authorization") from error
    if existing is not None and candidate != existing:
        raise StateConflictError(f"Goal {goal_id} has a different authorized goal_worker_budget")
    return candidate


def goal_worker_attempts_used(
    uow: UnitOfWork | ReadSession,
    goal_id: ID,
) -> int:
    """Return all persisted Worker Attempts consumed by a Goal's Runs."""

    return sum(len(uow.states.list_attempts(run.run_id)) for run in uow.states.list_runs(goal_id))


def _budget_from_run_started(stored: StoredEvent) -> GoalWorkerBudget | None:
    payload = stored.event.payload
    execution_config = payload.get("execution_config")
    if not isinstance(execution_config, Mapping) or _BUDGET_KEY not in execution_config:
        return None

    authorization = payload.get("authorization")
    explicit = isinstance(authorization, Mapping) and authorization.get("explicit") is True
    if not explicit:
        raise StateConflictError(
            f"RunStarted event {stored.event.id} contains goal_worker_budget without "
            "explicit authorization"
        )

    raw_document = execution_config[_BUDGET_KEY]
    if raw_document is None:
        return None
    if not isinstance(raw_document, Mapping):
        raise StateConflictError(
            f"RunStarted event {stored.event.id} has an invalid goal_worker_budget"
        )
    document = cast(Mapping[str, JsonValue], raw_document)
    try:
        return GoalWorkerBudget.from_document(document)
    except (TypeError, ValueError) as error:
        raise StateConflictError(
            f"RunStarted event {stored.event.id} has an invalid goal_worker_budget"
        ) from error
