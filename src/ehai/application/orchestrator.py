"""Orchestrator facade: plan execution split by responsibility into ``orchestration``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.checks import CheckRunner
from ehai.application.evaluation import (
    BranchEvaluator,
)
from ehai.application.orchestration.adoption import AdoptionMixin
from ehai.application.orchestration.attempts import AttemptLifecycleMixin
from ehai.application.orchestration.branches import BranchEvaluationMixin
from ehai.application.orchestration.candidates import CandidatesMixin
from ehai.application.orchestration.common import (
    AdoptionInputsChanged,
    ArtifactPersistenceError,
    AttemptBudgetExceededError,
    BranchEvaluationError,
    GateRejectedError,
    OrchestrationError,
    UnitOfWorkFactory,
    UnsupportedPlanError,
    WorkerCancellationUnsettledError,
    WorkerEventReceipt,
    WorkerFailedError,
    _required_plan,
    _required_run,
)
from ehai.application.orchestration.interventions import InterventionsMixin
from ehai.application.orchestration.readiness import (
    ready_nodes,
)
from ehai.application.orchestration.verification import VerificationMixin
from ehai.application.orchestration.worker_context import WorkerContextMixin
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.process_control import run_contract_is_current
from ehai.application.workers import (
    AdoptedResultInputs,
    WorkerAdapter,
)
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Run
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import (
    PlanRevision,
)

__all__ = [
    "AdoptionInputsChanged",
    "ArtifactPersistenceError",
    "AttemptBudgetExceededError",
    "BranchEvaluationError",
    "GateRejectedError",
    "OrchestrationError",
    "Orchestrator",
    "UnitOfWorkFactory",
    "UnsupportedPlanError",
    "WorkerCancellationUnsettledError",
    "WorkerEventReceipt",
    "WorkerFailedError",
    "ready_nodes",
]


class Orchestrator(
    AttemptLifecycleMixin,
    WorkerContextMixin,
    CandidatesMixin,
    VerificationMixin,
    BranchEvaluationMixin,
    AdoptionMixin,
    InterventionsMixin,
):
    """Execute the deterministic I3 single-node vertical slice."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        worker: WorkerAdapter | None,
        artifact_store: ArtifactStore,
        check_runner: CheckRunner,
        workspace: str | Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], ID] = new_id,
        cancellation_settle_seconds: float = 2.0,
        branch_evaluator: BranchEvaluator | None = None,
        attempt_budget: int = 5,
    ) -> None:
        if not isinstance(cancellation_settle_seconds, (int, float)) or not (
            0 < cancellation_settle_seconds < float("inf")
        ):
            raise ValueError("cancellation_settle_seconds must be finite and positive")
        if type(attempt_budget) is not int or attempt_budget < 1:
            raise ValueError("attempt_budget must be a positive integer")
        self._uow_factory = uow_factory
        self._worker = worker
        self._artifact_store = artifact_store
        self._check_runner = check_runner
        self._workspace = Path.cwd() if workspace is None else Path(workspace)
        self._clock = clock
        self._id_factory = id_factory
        self._cancellation_settle_seconds = float(cancellation_settle_seconds)
        self._branch_evaluator = branch_evaluator
        self._attempt_budget = attempt_budget
        self._execution_workspace: Callable[[ID], Path | None] | None = None
        self._adoption_workspace: Callable[[ID], Path] | None = None
        self._adoption_inputs: Callable[[AdoptedResultInputs], bool] | None = None

    def enable_code_execution(self, workspace_resolver: Callable[[ID], Path | None]) -> None:
        """Bind isolated code workspaces and authorized final-Gate repair at composition."""
        self._execution_workspace = workspace_resolver

    def read_artifact(self, artifact_id: ID) -> bytes:
        """Read retained immutable bytes for application-level result adoption validation."""
        return self._artifact_store.read(artifact_id)

    def enable_adoption_execution(
        self,
        workspace_resolver: Callable[[ID], Path],
        input_validator: Callable[[AdoptedResultInputs], bool],
    ) -> None:
        """Bind host-verified independent workspaces for accepted prior results."""
        self._adoption_workspace = workspace_resolver
        self._adoption_inputs = input_validator

    @property
    def worker(self) -> WorkerAdapter:
        """Return the configured Worker Adapter for local Runtime composition."""
        if self._worker is None:
            raise OrchestrationError("this Orchestrator has no synchronous WorkerAdapter")
        return self._worker

    def _load_run_context(
        self,
        uow: UnitOfWork,
        run_id: ID,
    ) -> tuple[Run, PlanRevision, Goal, CompletionContract]:
        run = _required_run(uow, run_id)
        plan = _required_plan(uow, run.run_id)
        goal = uow.states.get_goal(run.goal_id)
        if goal is None:
            raise OrchestrationError(f"Goal {run.goal_id} is not persisted")
        contract = goal.completion_contract
        if contract is None or not contract.is_confirmed:
            raise OrchestrationError(f"Goal {goal.goal_id} has no confirmed CompletionContract")
        if not run_contract_is_current(uow, run, plan):
            raise OrchestrationError(
                f"PlanRevision {plan.plan_revision_id} does not reference Goal "
                f"{goal.goal_id}'s current CompletionContract version"
            )
        return run, plan, goal, contract

    @staticmethod
    def _require_single_node(plan: PlanRevision, contract: CompletionContract) -> None:
        if len(plan.nodes) != 1 or plan.edges or plan.branches:
            raise UnsupportedPlanError(
                f"I3 Orchestrator supports one PlanNode; PlanRevision "
                f"{plan.plan_revision_id} is reserved for I6"
            )
        node = plan.nodes[0]
        if set(node.required_check_ids) != set(contract.required_check_ids):
            raise OrchestrationError(
                f"PlanNode {node.plan_node_id} Checks do not match CompletionContract"
            )

    def _event(
        self,
        event_type: EventType,
        run: Run,
        correlation_id: ID,
        payload: dict[str, JsonValue],
    ) -> Event:
        return Event(
            type=event_type,
            run_id=run.run_id,
            correlation_id=correlation_id,
            payload=payload,
            occurred_at=self._clock(),
        )
