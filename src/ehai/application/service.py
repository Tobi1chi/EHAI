"""ExecutionService facade: core commands split by responsibility into ``execution_service``."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import Lock

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.checkpointing import (
    RecoveryService,
)
from ehai.application.execution_service.common import (
    ApplicationError,
    EntityNotFoundError,
    IdempotencyConflictError,
    UnitOfWorkFactory,
)
from ehai.application.execution_service.planning import PlanningCommandsMixin
from ehai.application.execution_service.process import ProcessCommandsMixin
from ehai.application.execution_service.runs import RunCommandsMixin
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import (
    Planner,
)
from ehai.application.planner_capacity import (
    PlannerCapacity,
)
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.run_control import RunControllerPort
from ehai.domain.events import Event, EventType

__all__ = [
    "ApplicationError",
    "EntityNotFoundError",
    "ExecutionService",
    "IdempotencyConflictError",
    "UnitOfWorkFactory",
]


class ExecutionService(PlanningCommandsMixin, ProcessCommandsMixin, RunCommandsMixin):
    """Handle P1 Commands while keeping all state changes in domain methods."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        planner: Planner,
        orchestrator: Orchestrator,
        run_controller: RunControllerPort | None = None,
        recovery_service: RecoveryService | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], ID] = new_id,
        background_start: bool = False,
        planning_workspace: str | None = None,
        planner_capacity: int = 1,
        project_configuration_resolver: Callable[
            [ID, dict[str, JsonValue] | None], dict[str, JsonValue]
        ]
        | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._planner = planner
        self._orchestrator = orchestrator
        self._run_controller = run_controller
        self._recovery_service = recovery_service
        self._clock = clock
        self._id_factory = id_factory
        self._background_start = background_start
        self._planning_workspace = planning_workspace
        self._planner_capacity = PlannerCapacity(planner_capacity)
        self._project_configuration_resolver = project_configuration_resolver
        self._execution_lock = Lock()

    @property
    def planner_capacity(self) -> PlannerCapacity:
        """The host's shared admission limit; distinct from Worker execution capacity."""
        return self._planner_capacity

    @property
    def orchestrator(self) -> Orchestrator:
        """Return the shared application Orchestrator for local P2 Runtime composition."""
        return self._orchestrator

    @staticmethod
    def _existing_result(
        uow: UnitOfWork,
        idempotency_key: str,
        command_name: str,
        fingerprint: str,
    ) -> Mapping[str, JsonValue] | None:
        receipt = uow.command_receipts.get(idempotency_key)
        if receipt is None:
            return None
        if receipt.command_name != command_name or receipt.command_fingerprint != fingerprint:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} already belongs to {receipt.command_name}"
            )
        return receipt.result

    def _record_receipt(
        self,
        uow: UnitOfWork,
        idempotency_key: str,
        command_name: str,
        fingerprint: str,
        result: Mapping[str, JsonValue],
    ) -> None:
        uow.command_receipts.put(
            self._make_receipt(idempotency_key, command_name, fingerprint, result)
        )

    def _make_receipt(
        self,
        idempotency_key: str,
        command_name: str,
        fingerprint: str,
        result: Mapping[str, JsonValue],
    ) -> CommandReceipt:
        return CommandReceipt(
            idempotency_key=idempotency_key,
            command_name=command_name,
            command_fingerprint=fingerprint,
            result=result,
            created_at=self._clock(),
        )

    def _event(
        self,
        event_type: EventType,
        correlation_id: ID,
        payload: Mapping[str, JsonValue],
    ) -> Event:
        return Event(
            type=event_type,
            correlation_id=correlation_id,
            payload=payload,
            occurred_at=self._clock(),
        )
