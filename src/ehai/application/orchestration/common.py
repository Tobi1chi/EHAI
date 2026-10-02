"""Types, errors and helpers shared by the Orchestrator responsibilities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ehai import ID
from ehai.application.ports import UnitOfWork
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import (
    CheckKind,
    CheckRunStatus,
    CheckSpec,
)
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import (
    Branch,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanRevision,
)

UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class WorkerEventReceipt:
    """Provider event metadata committed with its terminal Attempt transition."""

    worker_event_id: str
    cursor: str
    occurred_at: datetime
    provider_cost: float | None = None
    records_usage: bool = False


class OrchestrationError(RuntimeError):
    """Base failure for the bounded P1 Orchestrator."""


class AdoptionInputsChanged(OrchestrationError):
    """Accepted prior code needs fresh execution under the target's current inputs."""


class UnsupportedPlanError(OrchestrationError):
    """Raised when I3 receives a non-single-node plan reserved for I6."""


class WorkerFailedError(OrchestrationError):
    """Raised after Worker failure state and Events have been committed."""


class WorkerCancellationUnsettledError(OrchestrationError):
    """Raised when Worker cancellation has no matching Run control transition."""


class BranchEvaluationError(OrchestrationError):
    """Raised after branch evaluation fails closed and the Run is failed."""


class AttemptBudgetExceededError(OrchestrationError):
    """Raised when scheduling another Worker would exceed the Run budget."""


class ArtifactPersistenceError(OrchestrationError):
    """Raised when a successful Worker result cannot be persisted as Artifacts."""


class GateRejectedError(OrchestrationError):
    """Raised after a minimal Check or Gate rejects the Worker candidate."""


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    run: Run
    plan_revision: PlanRevision
    goal: Goal
    completion_contract: CompletionContract
    plan_node: PlanNode
    attempt: Attempt
    check_specs: tuple[CheckSpec, ...]


@dataclass(frozen=True, slots=True)
class _VerificationScope:
    """Target verification identity plus its real producer and evidence."""

    run: Run
    plan_revision: PlanRevision
    completion_contract: CompletionContract
    plan_node: PlanNode
    attempt: Attempt
    adoption: ResultAdoption | None
    artifacts: tuple[Artifact, ...]


def _evaluator_branches(plan: PlanRevision, evaluator: PlanNode) -> tuple[Branch, ...]:
    """Return the unique branch group whose terminals feed an Evaluator."""
    if evaluator.kind is not PlanNodeKind.EVALUATOR:
        raise OrchestrationError(f"PlanNode {evaluator.plan_node_id} is not an Evaluator")
    dependencies = {
        edge.source_node_id
        for edge in plan.edges
        if edge.target_node_id == evaluator.plan_node_id and edge.edge_type is EdgeType.DEPENDENCY
    }
    groups: dict[tuple[ID, ID], list[Branch]] = {}
    for branch in plan.branches:
        groups.setdefault((branch.fork_node_id, branch.merge_node_id), []).append(branch)
    matches = [
        tuple(branches)
        for branches in groups.values()
        if {branch.node_ids[-1] for branch in branches}.issubset(dependencies)
    ]
    if len(matches) != 1:
        raise OrchestrationError(
            f"Evaluator {evaluator.plan_node_id} must feed exactly one complete Branch group"
        )
    return matches[0]


def _review_rework_has_active_work(uow: UnitOfWork, run_id: ID) -> bool:
    """Wait for existing producers and automatic verification, not human replies."""
    return any(
        item.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
        for item in uow.states.list_attempts(run_id)
    ) or any(
        check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
        and (
            (spec := uow.states.get_check_spec(check.check_id)) is None
            or spec.kind is not CheckKind.HUMAN
        )
        for check in uow.states.list_check_runs(run_id)
    )


def _review_rework_uses_planner(uow: UnitOfWork, run_id: ID) -> bool:
    """Defer a selected adjustment policy to its owner before reopening work."""
    for stored in uow.events.list_events():
        event = stored.event
        if event.run_id != run_id or event.type is not EventType.RUN_STARTED:
            continue
        authorization = event.payload.get("authorization")
        if not isinstance(authorization, dict) or authorization.get("explicit") is not True:
            continue
        config = event.payload.get("execution_config")
        return isinstance(config, dict) and config.get("process_adjustment") is not None
    return False


def _required_check_specs(
    uow: UnitOfWork,
    plan: PlanRevision,
    node: PlanNode,
) -> tuple[CheckSpec, ...]:
    specs_by_id = {
        spec.check_id: spec for spec in uow.states.list_check_specs(plan.plan_revision_id)
    }
    required_specs = tuple(
        specs_by_id[check_id] for check_id in node.required_check_ids if check_id in specs_by_id
    )
    if len(required_specs) != len(node.required_check_ids) or any(
        not spec.required for spec in required_specs
    ):
        raise OrchestrationError(
            f"PlanNode {node.plan_node_id} required CheckSpecs are missing or optional"
        )
    return required_specs


def _replace_node(plan: PlanRevision, replacement: PlanNode) -> PlanRevision:
    nodes = tuple(
        replacement if node.plan_node_id == replacement.plan_node_id else node
        for node in plan.nodes
    )
    if nodes == plan.nodes:
        raise OrchestrationError(
            f"PlanNode {replacement.plan_node_id} is not in PlanRevision {plan.plan_revision_id}"
        )
    return _rehydrate_plan(plan, nodes=nodes)


def _rehydrate_plan(
    plan: PlanRevision,
    *,
    nodes: tuple[PlanNode, ...] | None = None,
    branches: tuple[Branch, ...] | None = None,
) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=plan.nodes if nodes is None else nodes,
        edges=plan.edges,
        branches=plan.branches if branches is None else branches,
        phases=plan.phases,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
        design_document=plan.design_document,
    )


def _required_run(uow: UnitOfWork, run_id: ID) -> Run:
    run = uow.states.get_run(run_id)
    if run is None:
        raise OrchestrationError(f"Run {run_id} is not persisted")
    return run


def _required_process_id(uow: UnitOfWork, run_id: ID) -> ID:
    process = uow.states.get_active_process_revision(run_id)
    if process is None:
        raise OrchestrationError(f"Run {run_id} has no active ProcessRevision")
    return process.process_revision_id


def _required_plan(uow: UnitOfWork, run_id: ID) -> PlanRevision:
    plan = uow.states.get_execution_plan(run_id)
    if plan is None:
        raise OrchestrationError(f"Run {run_id} execution PlanRevision is not persisted")
    return plan


def _required_attempt(uow: UnitOfWork, attempt_id: ID) -> Attempt:
    attempt = uow.states.get_attempt(attempt_id)
    if attempt is None:
        raise OrchestrationError(f"Attempt {attempt_id} is not persisted")
    return attempt


def _required_node(plan: PlanRevision, plan_node_id: ID) -> PlanNode:
    for node in plan.nodes:
        if node.plan_node_id == plan_node_id:
            return node
    raise OrchestrationError(
        f"PlanNode {plan_node_id} is not in PlanRevision {plan.plan_revision_id}"
    )
