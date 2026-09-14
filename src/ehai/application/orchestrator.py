"""P1 single-node orchestration and pure ready-node computation."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ehai import ID, JsonValue, json_loads, new_id, normalize_id, utc_now
from ehai.application.checks import CheckContext, CheckRunner
from ehai.application.evaluation import (
    BranchEvaluationContext,
    BranchEvaluator,
    BranchSelection,
    BranchSelectionProtocolError,
    parse_branch_selection,
    validate_branch_selection,
)
from ehai.application.execution_policy import RetrySafety
from ehai.application.goal_budgets import goal_worker_attempts_used, goal_worker_budget
from ehai.application.interventions import (
    WorkerBlocker,
    attempt_process_interventions,
    list_interventions,
    open_intervention,
    reply_intervention,
)
from ehai.application.pause_causes import PauseCause, pause_event_payload
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.process_control import run_contract_is_current
from ehai.application.review import validate_review_submission
from ehai.application.sanitization import bounded_redacted_text
from ehai.application.workers import (
    MAX_ARTIFACT_INPUT_BYTES,
    MAX_ARTIFACT_INPUT_TOTAL_BYTES,
    AdoptedResultInputs,
    ArtifactInputBudgetExceeded,
    ArtifactInputSnapshot,
    WorkerAdapter,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
    WorkerTimedOutError,
)
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import (
    CheckKind,
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    CheckSpec,
    Gate,
    GateDecision,
    HumanCheckEvidence,
    HumanCheckRequest,
)
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
    PlanTransitionError,
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


def ready_nodes(plan_revision: PlanRevision) -> tuple[PlanNode, ...]:
    """Return pending nodes whose dependencies completed, preserving graph order."""
    if plan_revision.status is not PlanRevisionStatus.APPROVED:
        raise OrchestrationError(
            f"PlanRevision {plan_revision.plan_revision_id} must be approved before scheduling"
        )
    node_by_id = {node.plan_node_id: node for node in plan_revision.nodes}
    ready: list[PlanNode] = []
    for node in plan_revision.nodes:
        if node.status is not PlanNodeStatus.PENDING:
            continue
        if not _exploration_dependencies_completed(plan_revision, node):
            continue
        containing_branch = _branch_containing(plan_revision, node.plan_node_id)
        if containing_branch is not None and containing_branch.status is BranchStatus.PRUNED:
            continue
        incoming_branches = tuple(
            branch for branch in plan_revision.branches if branch.merge_node_id == node.plan_node_id
        )
        branch_endpoint_ids = {branch.node_ids[-1] for branch in plan_revision.branches}
        if node.kind is PlanNodeKind.EVALUATOR:
            if incoming_branches and not all(
                _branch_is_terminal(branch, node_by_id) for branch in incoming_branches
            ):
                continue
            dependencies_ready = all(
                node_by_id[dependency_id].status is PlanNodeStatus.COMPLETED
                or (
                    dependency_id in branch_endpoint_ids
                    and node_by_id[dependency_id].status is PlanNodeStatus.FAILED
                )
                or (
                    (branch := _branch_containing(plan_revision, dependency_id)) is not None
                    and _branch_has_failed(branch, node_by_id)
                )
                for dependency_id in node.required_dependency_ids
            )
        else:
            dependencies_ready = all(
                node_by_id[dependency_id].status is PlanNodeStatus.COMPLETED
                for dependency_id in node.required_dependency_ids
            )
        if not dependencies_ready:
            continue
        ready.append(node)
    return tuple(ready)


def _exploration_dependencies_completed(plan: PlanRevision, node: PlanNode) -> bool:
    node_by_id = {item.plan_node_id: item for item in plan.nodes}
    return all(
        node_by_id[edge.source_node_id].status is PlanNodeStatus.COMPLETED
        for edge in plan.edges
        if edge.target_node_id == node.plan_node_id and edge.edge_type is EdgeType.EXPLORATION
    )


def _branch_containing(plan: PlanRevision, plan_node_id: ID) -> Branch | None:
    return next(
        (branch for branch in plan.branches if plan_node_id in branch.node_ids),
        None,
    )


def _branch_is_terminal(branch: Branch, node_by_id: dict[ID, PlanNode]) -> bool:
    if any(
        node_by_id[node_id].status in {PlanNodeStatus.STALLED, PlanNodeStatus.SUSPENDED}
        for node_id in branch.node_ids
    ):
        return False
    return _branch_has_failed(branch, node_by_id) or node_by_id[branch.node_ids[-1]].status in {
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.FAILED,
    }


def _branch_has_failed(branch: Branch, node_by_id: dict[ID, PlanNode]) -> bool:
    if any(
        node_by_id[node_id].status in {PlanNodeStatus.STALLED, PlanNodeStatus.SUSPENDED}
        for node_id in branch.node_ids
    ):
        return False
    return any(node_by_id[node_id].status is PlanNodeStatus.FAILED for node_id in branch.node_ids)


def _exhausted_branch_reason(plan: PlanRevision) -> str | None:
    active = tuple(branch for branch in plan.branches if branch.status is BranchStatus.ACTIVE)
    if not active:
        return None
    node_by_id = {node.plan_node_id: node for node in plan.nodes}
    if not all(_branch_has_failed(branch, node_by_id) for branch in active):
        return None
    return "no viable Branch candidate; all active exploration branches failed"


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


class Orchestrator:
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

    def enable_adoption_execution(
        self,
        workspace_resolver: Callable[[ID], Path],
        input_validator: Callable[[AdoptedResultInputs], bool],
    ) -> None:
        """Bind host-verified independent workspaces for accepted prior results."""
        self._adoption_workspace = workspace_resolver
        self._adoption_inputs = input_validator

    def _verification_workspace(self, scope: _VerificationScope) -> Path:
        if scope.adoption is not None:
            if self._adoption_workspace is None:
                raise OrchestrationError("Adoption verification workspace is not configured")
            return self._adoption_workspace(scope.adoption.adoption_id)
        return (
            self._workspace
            if self._execution_workspace is None
            else self._required_execution_workspace(scope.attempt.attempt_id)
        )

    @property
    def worker(self) -> WorkerAdapter:
        """Return the configured Worker Adapter for local Runtime composition."""
        if self._worker is None:
            raise OrchestrationError("this Orchestrator has no synchronous WorkerAdapter")
        return self._worker

    def execute(self, run_id: ID) -> Run:
        """Execute an approved P1 PlanGraph serially through its final merge Gate."""
        worker = self.worker
        while True:
            adopted_run = self.advance_ready_adoptions(run_id)
            if adopted_run is not None and adopted_run.status is not RunStatus.RUNNING:
                return adopted_run
            if self.waiting_for_human(run_id, only_if_idle=True):
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            if self._evaluate_completed_evaluator(run_id):
                continue
            context = self._prepare(run_id)
            if context is None:
                continue
            branch_local = (
                _branch_containing(context.plan_revision, context.plan_node.plan_node_id)
                is not None
            )
            try:
                artifact_inputs = self._worker_inputs(context)
                worker_context = self._worker_context(context, artifact_inputs)
                if context.plan_node.kind is PlanNodeKind.MERGE:
                    artifact_inputs = _selected_merge_inputs(worker_context, artifact_inputs)
                request = WorkerRequest(
                    run=context.run,
                    attempt=context.attempt,
                    plan_node=context.plan_node,
                    completion_contract=context.completion_contract,
                    required_check_specs=context.check_specs,
                    context=worker_context,
                    artifact_inputs=artifact_inputs,
                )
                result = worker.execute(request)
                self._validate_reviewer_result(context, result)
            except WorkerCancelledError as error:
                try:
                    artifacts = self._store_worker_error_artifacts(context, error)
                except Exception as artifact_error:
                    settled = self._await_controlled_cancellation(context.attempt.attempt_id, error)
                    raise ArtifactPersistenceError(
                        f"attempt {context.attempt.attempt_id} settled as "
                        f"{settled.status.value}, but its cancellation diagnostics could "
                        f"not be persisted: {type(artifact_error).__name__}: {artifact_error}"
                    ) from artifact_error
                self._record_worker_diagnostics(context.attempt.attempt_id, artifacts)
                return self._await_controlled_cancellation(context.attempt.attempt_id, error)
            except WorkerTimedOutError as error:
                try:
                    artifacts = self._store_worker_error_artifacts(context, error)
                except Exception as artifact_error:
                    self._record_worker_failure(
                        context.attempt.attempt_id,
                        error,
                        (),
                        diagnostic_error=artifact_error,
                        fail_run=True,
                    )
                    raise ArtifactPersistenceError(
                        f"attempt {context.attempt.attempt_id} timed out, but its diagnostics "
                        f"could not be persisted: {type(artifact_error).__name__}: "
                        f"{artifact_error}"
                    ) from artifact_error
                self._record_worker_failure(
                    context.attempt.attempt_id,
                    error,
                    artifacts,
                    fail_run=not branch_local,
                )
                if branch_local:
                    continue
                raise WorkerFailedError(
                    f"attempt {context.attempt.attempt_id} timed out: {error.reason}"
                ) from error
            except Exception as error:
                try:
                    artifacts = (
                        self._store_worker_error_artifacts(context, error)
                        if isinstance(error, WorkerExecutionError)
                        else ()
                    )
                except Exception as artifact_error:
                    self._record_worker_failure(
                        context.attempt.attempt_id,
                        error,
                        (),
                        diagnostic_error=artifact_error,
                        fail_run=True,
                    )
                    raise ArtifactPersistenceError(
                        f"attempt {context.attempt.attempt_id} failed, but its diagnostics "
                        f"could not be persisted: {type(artifact_error).__name__}: "
                        f"{artifact_error}"
                    ) from artifact_error
                self._record_worker_failure(
                    context.attempt.attempt_id,
                    error,
                    artifacts,
                    fail_run=not branch_local,
                )
                if branch_local:
                    continue
                raise WorkerFailedError(
                    f"attempt {context.attempt.attempt_id} failed: {type(error).__name__}: {error}"
                ) from error
            try:
                artifacts = self._store_candidate_artifacts(context, result)
            except Exception as error:
                self._record_artifact_failure(context.attempt.attempt_id, result, error)
                raise ArtifactPersistenceError(
                    f"attempt {context.attempt.attempt_id} produced a candidate but Artifact "
                    f"persistence failed: {type(error).__name__}: {error}"
                ) from error

            self._record_candidate(context.attempt.attempt_id, artifacts, result)
            outcome = self._check_and_complete(
                run_id,
                context.attempt.attempt_id,
                artifacts,
                context.check_specs,
                branch_local=branch_local,
            )
            if outcome.status is not RunStatus.RUNNING or self.waiting_for_human(
                run_id, only_if_idle=True
            ):
                return outcome

    def prepare_worker_request(self, run_id: ID) -> WorkerRequest:
        """Persist and return the next runnable Attempt without invoking a Worker."""
        while self._evaluate_completed_evaluator(run_id):
            pass
        context = self._prepare(run_id)
        if context is None:  # pragma: no cover - retained for the existing private contract
            raise OrchestrationError(f"run {run_id} produced no execution context")
        artifact_inputs = self._worker_inputs(context)
        worker_context = self._worker_context(context, artifact_inputs)
        if context.plan_node.kind is PlanNodeKind.MERGE:
            artifact_inputs = _selected_merge_inputs(worker_context, artifact_inputs)
        return WorkerRequest(
            run=context.run,
            attempt=context.attempt,
            plan_node=context.plan_node,
            completion_contract=context.completion_contract,
            required_check_specs=context.check_specs,
            context=worker_context,
            artifact_inputs=artifact_inputs,
        )

    def worker_request_for_attempt(self, attempt_id: ID) -> WorkerRequest:
        """Rebuild one running Attempt request without creating or advancing work."""
        context = self._load_attempt_context(normalize_id(attempt_id))
        artifact_inputs = self._worker_inputs(context)
        worker_context = self._worker_context(context, artifact_inputs)
        if context.plan_node.kind is PlanNodeKind.MERGE:
            artifact_inputs = _selected_merge_inputs(worker_context, artifact_inputs)
        return WorkerRequest(
            run=context.run,
            attempt=context.attempt,
            plan_node=context.plan_node,
            completion_contract=context.completion_contract,
            required_check_specs=context.check_specs,
            context=worker_context,
            artifact_inputs=artifact_inputs,
        )

    def queue_ready_attempts(self, run_id: ID, *, limit: int) -> tuple[Attempt, ...]:
        """Persist ready PlanNodes as queued Attempts without consuming capacity."""
        if type(limit) is not int or limit < 1:
            raise ValueError("queue limit must be a positive integer")
        while self._evaluate_completed_evaluator(run_id):
            pass
        with self._uow_factory() as uow:
            run, plan, _, _ = self._load_run_context(uow, run_id)
            if run.status is RunStatus.PENDING:
                run = run.start(at=self._clock())
                uow.states.put_run(run)
                uow.events.append(
                    self._event(EventType.RUN_STARTED, run, run.run_id, {"run_id": run.run_id})
                )
            if run.status is not RunStatus.RUNNING:
                return ()
            plan = self._recover_stalled_nodes(uow, run, plan)
            if _review_rework_uses_planner(uow, run.run_id):
                attempts = uow.states.list_attempts(run.run_id)
                submitted_nodes = {
                    item.plan_node_id for item in attempts if item.status is AttemptStatus.SUCCEEDED
                }
                rejected_reviewers = tuple(
                    node
                    for node in plan.nodes
                    if node.kind is PlanNodeKind.REVIEWER
                    and node.status is PlanNodeStatus.FAILED
                    and node.plan_node_id in submitted_nodes
                )
                if rejected_reviewers:
                    # Let existing actors settle without scheduling unrelated new
                    # work or turning a scoped review failure into runtime_error.
                    if _review_rework_has_active_work(uow, run.run_id):
                        return ()
                    paused = run.pause()
                    uow.states.put_run(paused)
                    uow.events.append(
                        self._event(
                            EventType.RUN_PAUSED,
                            paused,
                            run.run_id,
                            pause_event_payload(
                                run.run_id,
                                PauseCause.REVIEW_REWORK,
                                reason="Configured Planner must determine the Phase rework scope",
                                reviewer_node_ids=[
                                    str(node.plan_node_id) for node in rejected_reviewers
                                ],
                            ),
                        )
                    )
                    uow.commit()
                    return ()
            exhausted_reason = _exhausted_branch_reason(plan)
            if exhausted_reason is not None:
                failed_run = (
                    run.pause()
                    if self._execution_workspace is not None
                    else run.fail(exhausted_reason, at=self._clock())
                )
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        (
                            EventType.RUN_PAUSED
                            if self._execution_workspace is not None
                            else EventType.RUN_FAILED
                        ),
                        failed_run,
                        failed_run.run_id,
                        (
                            pause_event_payload(
                                failed_run.run_id,
                                PauseCause.BRANCH_EXHAUSTED,
                                reason=exhausted_reason,
                            )
                            if self._execution_workspace is not None
                            else {"run_id": failed_run.run_id, "reason": exhausted_reason}
                        ),
                    )
                )
                uow.commit()
                return ()
            attempts = uow.states.list_attempts(run.run_id)
            active_nodes = {
                attempt.plan_node_id
                for attempt in attempts
                if attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
            }
            candidates = tuple(
                node
                for node in (
                    tuple(
                        node
                        for node in plan.nodes
                        if node.status is PlanNodeStatus.READY
                        and _exploration_dependencies_completed(plan, node)
                    )
                    + ready_nodes(plan)
                )
                if node.plan_node_id not in active_nodes
            )[:limit]
            budget = goal_worker_budget(uow, run.goal_id)
            if budget is not None:
                used = goal_worker_attempts_used(uow, run.goal_id)
                remaining = max(0, budget.max_worker_attempts - used)
                if (
                    candidates
                    and remaining == 0
                    and not _review_rework_has_active_work(uow, run_id)
                ):
                    self._pause_goal_budget(uow, run, used, budget.max_worker_attempts)
                    uow.commit()
                    return ()
                candidates = candidates[:remaining]
            queued: list[Attempt] = []
            for candidate in candidates:
                ready = (
                    candidate.mark_ready()
                    if candidate.status is PlanNodeStatus.PENDING
                    else candidate
                )
                if candidate.status is PlanNodeStatus.PENDING:
                    plan = _replace_node(plan, ready)
                    uow.events.append(
                        self._event(
                            EventType.PLAN_NODE_READIED,
                            run,
                            ready.plan_node_id,
                            {"plan_node_id": ready.plan_node_id},
                        )
                    )
                attempt = Attempt(
                    run_id=run.run_id,
                    process_revision_id=_required_process_id(uow, run.run_id),
                    plan_node_id=ready.plan_node_id,
                    sequence=len(attempts) + len(queued) + 1,
                    attempt_id=self._id_factory(),
                    created_at=self._clock(),
                ).queue("awaiting capacity and dispatch")
                uow.states.put_attempt(attempt)
                uow.events.append(
                    self._event(
                        EventType.ATTEMPT_QUEUED,
                        run,
                        attempt.attempt_id,
                        {
                            "attempt_id": attempt.attempt_id,
                            "plan_node_id": attempt.plan_node_id,
                            "reason": attempt.queue_reason,
                        },
                    )
                )
                queued.append(attempt)
            if queued:
                uow.states.put_execution_plan(run.run_id, plan)
            uow.commit()
            return tuple(queued)

    def start_queued_attempt(self, attempt_id: ID) -> WorkerRequest:
        """Start one queued Attempt after Scheduler capacity is reserved."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run, plan, goal, contract = self._load_run_context(uow, attempt.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            if (
                attempt.status is not AttemptStatus.PENDING
                or node.status is not PlanNodeStatus.READY
            ):
                raise OrchestrationError(f"attempt {attempt.attempt_id} is not queued and ready")
            running_attempt = attempt.start(at=self._clock())
            running_node = node.start()
            plan = _replace_node(plan, running_node)
            check_specs = _required_check_specs(uow, plan, running_node)
            uow.states.put_attempt(running_attempt)
            uow.states.put_execution_plan(run.run_id, plan)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_STARTED,
                    run,
                    running_node.plan_node_id,
                    {"plan_node_id": running_node.plan_node_id},
                )
            )
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_DISPATCHED,
                    run,
                    running_attempt.attempt_id,
                    {"attempt_id": running_attempt.attempt_id},
                )
            )
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_STARTED,
                    run,
                    running_attempt.attempt_id,
                    {
                        "attempt_id": running_attempt.attempt_id,
                        "plan_node_id": running_attempt.plan_node_id,
                    },
                )
            )
            uow.commit()
        context = _ExecutionContext(
            run,
            plan,
            goal,
            contract,
            running_node,
            running_attempt,
            check_specs,
        )
        artifact_inputs = self._worker_inputs(context)
        worker_context = self._worker_context(context, artifact_inputs)
        if running_node.kind is PlanNodeKind.MERGE:
            artifact_inputs = _selected_merge_inputs(worker_context, artifact_inputs)
        return WorkerRequest(
            run=run,
            attempt=running_attempt,
            plan_node=running_node,
            completion_contract=contract,
            required_check_specs=check_specs,
            context=worker_context,
            artifact_inputs=artifact_inputs,
        )

    def cancel_queued_attempt(self, attempt_id: ID) -> Run:
        """Cancel queued work without starting a Connector or consuming capacity."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.PENDING:
                raise OrchestrationError(f"attempt {attempt.attempt_id} is not queued")
            cancelled_attempt = attempt.cancel("cancelled while queued", at=self._clock())
            cancelled_run = run.cancel("queued Attempt cancelled", at=self._clock())
            uow.states.put_attempt(cancelled_attempt)
            uow.states.put_run(cancelled_run)
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_CANCELLED,
                    run,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "reason": "cancelled while queued"},
                )
            )
            uow.events.append(
                self._event(
                    EventType.RUN_CANCELLED,
                    cancelled_run,
                    run.run_id,
                    {"run_id": run.run_id, "reason": "queued Attempt cancelled"},
                )
            )
            uow.commit()
            return cancelled_run

    def pause_after_runtime_error(self, run_id: ID, reason: str) -> Run:
        """Pause a quiesced Run and discard only its pending dispatch attempts."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("runtime error reason must not be blank")
        safe_reason = bounded_redacted_text(reason, max_bytes=2_000)
        if safe_reason is None:
            raise ValueError("runtime error reason must not be blank")
        with self._uow_factory() as uow:
            run = _required_run(uow, normalize_id(run_id))
            _required_plan(uow, run.run_id)
            attempts = uow.states.list_attempts(run.run_id)
            if any(attempt.status is AttemptStatus.RUNNING for attempt in attempts):
                raise OrchestrationError(
                    f"Run {run.run_id} still has a running Attempt; quiescence is incomplete"
                )
            at = self._clock()
            for attempt in attempts:
                if attempt.status is not AttemptStatus.PENDING:
                    continue
                cancelled = attempt.cancel(safe_reason, at=at)
                uow.states.put_attempt(cancelled)
                uow.events.append(
                    self._event(
                        EventType.ATTEMPT_CANCELLED,
                        run,
                        attempt.attempt_id,
                        {"attempt_id": attempt.attempt_id, "reason": safe_reason},
                    )
                )
            # Keep each PlanNode's existing status. A READY node will be queued again
            # only after an explicit Run resume; a PENDING node must retain its
            # dependency barrier.
            if run.status is RunStatus.RUNNING:
                paused = run.pause()
                uow.states.put_run(paused)
                uow.events.append(
                    self._event(
                        EventType.RUN_PAUSED,
                        paused,
                        paused.run_id,
                        pause_event_payload(
                            paused.run_id,
                            PauseCause.RUNTIME_ERROR,
                            reason=safe_reason,
                        ),
                    )
                )
            else:
                paused = run
            uow.commit()
            return paused

    def accept_worker_result(
        self,
        attempt_id: ID,
        result: WorkerResult,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> Run:
        """Persist one Worker candidate and advance existing Check/Gate semantics."""
        context = self._load_attempt_context(attempt_id)
        branch_local = (
            _branch_containing(context.plan_revision, context.plan_node.plan_node_id) is not None
        )
        try:
            self._validate_reviewer_result(context, result)
        except ValueError as error:
            self._record_worker_failure(attempt_id, error, (), fail_run=not branch_local)
            raise WorkerFailedError(f"Reviewer candidate rejected: {error}") from error
        try:
            artifacts = self._store_candidate_artifacts(context, result)
        except Exception as error:
            self._record_artifact_failure(context.attempt.attempt_id, result, error)
            raise ArtifactPersistenceError(
                f"attempt {context.attempt.attempt_id} produced a candidate but Artifact "
                f"persistence failed: {type(error).__name__}: {error}"
            ) from error
        self._record_candidate(
            context.attempt.attempt_id,
            artifacts,
            result,
            worker_event_receipts=worker_event_receipts,
        )
        return self._check_and_complete(
            context.run.run_id,
            context.attempt.attempt_id,
            artifacts,
            context.check_specs,
            branch_local=branch_local,
        )

    def _accept_intermediate(
        self, run_id: ID, attempt_id: ID, *, adoption_id: ID | None = None
    ) -> Run:
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            run, plan, attempt, node = (
                scope.run,
                scope.plan_revision,
                scope.attempt,
                scope.plan_node,
            )
            if run.run_id != run_id:
                raise OrchestrationError("Intermediate result does not match its target Run")
            if not any(edge.source_node_id == node.plan_node_id for edge in plan.edges):
                raise OrchestrationError("A final node must have an approved acceptance Gate")
            if attempt.status is not AttemptStatus.SUCCEEDED or not attempt.artifact_ids:
                raise OrchestrationError(
                    "Intermediate acceptance requires persisted result evidence"
                )
            completed = node.accept_intermediate()
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, completed))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_COMPLETED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "acceptance": "intermediate_result",
                        "attempt_id": attempt_id,
                        **({} if adoption_id is None else {"adoption_id": adoption_id}),
                    },
                )
            )
            uow.commit()
            return run

    def advance_ready_adoptions(self, run_id: ID) -> Run | None:
        """Admit ready retained results before Worker dispatch, outside queue transactions."""
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status is not RunStatus.RUNNING or not uow.states.list_result_adoptions(run_id):
                return None
        # Resolve a just-completed Evaluator before queueing its adopted Merge.
        # Otherwise ordinary dispatch would create an unnecessary target Attempt.
        while self._evaluate_completed_evaluator(run_id):
            pass
        examined: set[ID] = set()
        advanced: Run | None = None
        while True:
            with self._uow_factory() as uow:
                run = _required_run(uow, run_id)
                records = uow.states.list_result_adoptions(run_id)
                if not records or run.status is not RunStatus.RUNNING:
                    return advanced
                plan = _required_plan(uow, run_id)
                if not run_contract_is_current(uow, run, plan):
                    return advanced
                if _review_rework_uses_planner(uow, run_id) and any(
                    node.kind is PlanNodeKind.REVIEWER and node.status is PlanNodeStatus.FAILED
                    for node in plan.nodes
                ):
                    return advanced
                executed = {item.plan_node_id for item in uow.states.list_attempts(run_id)}
                submitted = {
                    stored.event.payload.get("adoption_id")
                    for stored in uow.events.list_events()
                    if stored.event.run_id == run_id
                    and stored.event.type is EventType.PLAN_NODE_CANDIDATE_SUBMITTED
                    and isinstance(stored.event.payload.get("adoption_id"), str)
                }
                ready_ids = {node.plan_node_id for node in ready_nodes(plan)} | {
                    node.plan_node_id
                    for node in plan.nodes
                    if node.status is PlanNodeStatus.READY
                    and _exploration_dependencies_completed(plan, node)
                }
                adoption = next(
                    (
                        item
                        for item in records
                        if item.adoption_id not in examined
                        and item.adoption_id not in submitted
                        and item.target_plan_node_id not in executed
                        and item.target_plan_node_id in ready_ids
                    ),
                    None,
                )
            if adoption is None:
                return advanced
            examined.add(adoption.adoption_id)
            try:
                advanced = self.accept_adopted_result(adoption.adoption_id)
            except AdoptionInputsChanged:
                # Leave the node ready for normal Worker execution. No synthetic
                # Attempt or reused Gate result represents the declined adoption.
                continue

    def accept_adopted_result(self, adoption_id: ID) -> Run:
        """Advance an admitted, ready result without creating a Worker Attempt.

        Host validation establishes actual target input applicability before
        accepting a candidate. This is not a public approval entry point.
        """
        normalized_id = normalize_id(adoption_id)
        with self._uow_factory() as uow:
            adoption = uow.states.get_result_adoption(normalized_id)
            if adoption is None:
                raise OrchestrationError("Result adoption is not persisted")
            observed = self._load_verification_scope(uow, adoption.source_attempt_id, normalized_id)
            if observed.run.status is not RunStatus.RUNNING:
                raise OrchestrationError("Adopted result requires a running target Run")
            if observed.plan_node.status is PlanNodeStatus.COMPLETED:
                return observed.run
        input_context = AdoptedResultInputs(
            observed.run, observed.plan_revision, observed.plan_node, adoption
        )
        inputs = AdoptedResultInputs(
            observed.run,
            observed.plan_revision,
            observed.plan_node,
            adoption,
            self._worker_inputs(input_context),
        )
        if self._adoption_inputs is None:
            raise OrchestrationError("Adoption input applicability validator is not configured")
        if not self._adoption_inputs(inputs):
            raise AdoptionInputsChanged(
                "Adopted result inputs changed; target needs fresh execution"
            )
        with self._uow_factory() as uow:
            adoption = uow.states.get_result_adoption(normalized_id)
            if adoption is None:
                raise OrchestrationError("Result adoption is not persisted")
            scope = self._load_verification_scope(uow, adoption.source_attempt_id, normalized_id)
            if scope != observed:
                raise OrchestrationError("Adoption target changed during input validation")
            run, plan, node = scope.run, scope.plan_revision, scope.plan_node
            if run.status is not RunStatus.RUNNING:
                raise OrchestrationError("Adopted result requires a running target Run")
            if node.status is PlanNodeStatus.COMPLETED:
                return run
            if node.status in {PlanNodeStatus.PENDING, PlanNodeStatus.READY}:
                if any(
                    check.adoption_id == normalized_id
                    for check in uow.states.list_check_runs(run.run_id)
                ):
                    raise OrchestrationError(
                        "Adopted result was already evaluated; rework needs a new result"
                    )
                readiness_plan = (
                    plan
                    if node.status is PlanNodeStatus.PENDING
                    else _replace_node(plan, node.reopen_for_rework())
                )
                if node.plan_node_id not in {
                    item.plan_node_id for item in ready_nodes(readiness_plan)
                }:
                    raise OrchestrationError("Adopted result dependencies are not ready")
                self._verification_workspace(scope)
                if node.status is PlanNodeStatus.PENDING:
                    node = node.mark_ready()
                    plan = _replace_node(plan, node)
                    uow.states.put_execution_plan(run.run_id, plan)
                node = node.submit_adopted_candidate(adoption)
                plan = _replace_node(plan, node)
                uow.states.put_execution_plan(run.run_id, plan)
                uow.events.append(
                    self._event(
                        EventType.PLAN_NODE_CANDIDATE_SUBMITTED,
                        run,
                        node.plan_node_id,
                        {
                            "plan_node_id": node.plan_node_id,
                            "acceptance": "adopted_result",
                            "adoption_id": normalized_id,
                            "source_attempt_id": scope.attempt.attempt_id,
                        },
                    )
                )
            elif node.status not in {PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING}:
                raise OrchestrationError("Adopted target is not ready for acceptance")
            specs = _required_check_specs(uow, plan, node)
            uow.commit()
        return self._check_and_complete(
            run.run_id,
            scope.attempt.attempt_id,
            scope.artifacts,
            specs,
            branch_local=_branch_containing(plan, node.plan_node_id) is not None,
            adoption_id=normalized_id,
        )

    def recover_candidate_results(self, run_id: ID) -> None:
        """Finish persisted candidates whose acceptance transaction was interrupted."""
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status is not RunStatus.RUNNING:
                return
            plan = _required_plan(uow, run.run_id)
            if not run_contract_is_current(uow, run, plan):
                return
            candidates = {
                node.plan_node_id
                for node in plan.nodes
                if node.status in {PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING}
            }
            latest = {
                attempt.plan_node_id: attempt
                for attempt in sorted(
                    uow.states.list_attempts(run_id), key=lambda item: item.sequence
                )
                if attempt.plan_node_id in candidates and attempt.status is AttemptStatus.SUCCEEDED
            }
            artifacts = uow.states.list_artifacts_for_run(run_id)
            specs = {
                node_id: _required_check_specs(uow, plan, _required_node(plan, node_id))
                for node_id in latest
            }
            adopted_candidates = tuple(
                adoption
                for adoption in uow.states.list_result_adoptions(run_id)
                if adoption.target_plan_node_id in candidates
                and adoption.target_plan_node_id not in latest
            )
        for node_id, attempt in latest.items():
            self._check_and_complete(
                run_id,
                attempt.attempt_id,
                tuple(
                    artifact for artifact in artifacts if artifact.attempt_id == attempt.attempt_id
                ),
                specs[node_id],
                branch_local=_branch_containing(plan, node_id) is not None,
            )
        for adoption in adopted_candidates:
            with self._uow_factory() as uow:
                current_run = _required_run(uow, run_id)
                if current_run.status is not RunStatus.RUNNING:
                    return
                current_plan = _required_plan(uow, run_id)
                current_node = _required_node(current_plan, adoption.target_plan_node_id)
                if current_node.status not in {PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING}:
                    continue
            # Only resume an already submitted adopted candidate. This does not
            # admit pending adoptions or replace input-applicability scheduling.
            self.accept_adopted_result(adoption.adoption_id)

    def block_attempt(
        self,
        attempt_id: ID,
        blocker: WorkerBlocker,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> Run:
        """Settle one execution and retain its route while awaiting a user reply."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            intervention = open_intervention(uow, attempt_id, blocker)
            interrupted = self._apply_worker_event_receipts(
                uow,
                attempt.interrupt(blocker.reason, at=self._clock()),
                worker_event_receipts,
            )
            uow.states.put_attempt(interrupted)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, node.suspend()))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_SUSPENDED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "attempt_id": attempt_id,
                        "reason": blocker.reason,
                        "intervention_id": intervention["intervention_id"],
                    },
                )
            )
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_INTERRUPTED,
                    run,
                    attempt_id,
                    {
                        "attempt_id": attempt_id,
                        "reason": blocker.reason,
                        "intervention_id": intervention["intervention_id"],
                    },
                )
            )
            uow.commit()
            return run

    def record_intervention_reply(
        self,
        uow: UnitOfWork,
        intervention_id: ID,
        request_token: str,
        *,
        actor: str,
        message: str,
    ) -> Run:
        """Release only the explicitly answered node; retain all approved boundaries."""
        reply = reply_intervention(uow, intervention_id, request_token, actor, message)
        run_id, node_id = reply.get("run_id"), reply.get("plan_node_id")
        if not isinstance(run_id, str) or not isinstance(node_id, str):
            raise OrchestrationError("Intervention reply is missing its execution scope")
        run = _required_run(uow, normalize_id(run_id))
        plan = _required_plan(uow, run.run_id)
        node = _required_node(plan, normalize_id(node_id))
        if node.status is PlanNodeStatus.SUSPENDED:
            attempts = [
                item
                for item in uow.states.list_attempts(run.run_id)
                if item.plan_node_id == node.plan_node_id
            ]
            if not attempts or attempts[-1].attempt_id != reply.get("attempt_id"):
                raise OrchestrationError("Reply does not address the node's current blocker")
            if any(
                item.get("plan_node_id") == node.plan_node_id and item.get("status") == "open"
                for item in list_interventions(uow.events, run.run_id)
            ):
                raise OrchestrationError("The node still has an unanswered intervention")
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, node.resume()))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_RECOVERED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "intervention_id": intervention_id,
                        "recovery": "human_reply",
                    },
                )
            )
        return run

    def waiting_for_intervention(self, run_id: ID, *, only_if_idle: bool = False) -> bool:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                return False
            plan = _required_plan(uow, run.run_id)
            if only_if_idle and (
                ready_nodes(plan)
                or any(
                    node.status in {PlanNodeStatus.READY, PlanNodeStatus.STALLED}
                    for node in plan.nodes
                )
            ):
                return False
            return any(node.status is PlanNodeStatus.SUSPENDED for node in plan.nodes)

    def waiting_for_input(self, run_id: ID, *, only_if_idle: bool = False) -> bool:
        return self.waiting_for_intervention(
            run_id, only_if_idle=only_if_idle
        ) or self.waiting_for_human(run_id, only_if_idle=only_if_idle)

    def interrupt_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> Run:
        """Fail closed when Runtime recovery cannot find the original execution."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            branch_local = _branch_containing(plan, node.plan_node_id) is not None
            interrupted = self._apply_worker_event_receipts(
                uow,
                attempt.interrupt(reason, at=self._clock()),
                worker_event_receipts,
            )
            failed_node = node.fail()
            uow.states.put_attempt(interrupted)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, failed_node))
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_INTERRUPTED,
                    run,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "reason": reason},
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": reason},
                )
            )
            result = run
            if not branch_local and run.status is RunStatus.RUNNING:
                result = run.pause()
                uow.states.put_run(result)
                uow.events.append(
                    self._event(
                        EventType.RUN_PAUSED,
                        result,
                        run.run_id,
                        pause_event_payload(
                            run.run_id,
                            PauseCause.UNKNOWN_EXECUTION,
                            reason=reason,
                        ),
                    )
                )
            uow.commit()
            return result

    def retry_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        retry_safety: RetrySafety,
        timed_out: bool = False,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> Run:
        """Schedule replacement work only when the provider outcome is known safe."""
        safety = RetrySafety(retry_safety)
        if not safety.allows_retry:
            return self.interrupt_attempt(attempt_id, reason)
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            attempts = uow.states.list_attempts(run.run_id)
            if timed_out:
                terminal_attempt = attempt.time_out(reason, at=self._clock())
                terminal_event = EventType.ATTEMPT_TIMED_OUT
            elif safety in {RetrySafety.EXECUTION_NOT_FOUND, RetrySafety.LOCAL_ROLLBACK}:
                terminal_attempt = attempt.interrupt(reason, at=self._clock())
                terminal_event = EventType.ATTEMPT_INTERRUPTED
            else:
                terminal_attempt = attempt.fail(reason, at=self._clock())
                terminal_event = EventType.ATTEMPT_FAILED
            terminal_attempt = self._apply_worker_event_receipts(
                uow,
                terminal_attempt,
                worker_event_receipts,
            )
            retry_count = (
                sum(item.plan_node_id == node.plan_node_id for item in attempts)
                if self._execution_workspace is not None
                else len(attempts)
            )
            intervention = None
            if retry_count >= self._attempt_budget:
                intervention = open_intervention(
                    uow,
                    attempt_id,
                    WorkerBlocker(
                        reason="Autonomous recovery Attempt budget exhausted",
                        evidence=(
                            f"consumed={retry_count}, limit={self._attempt_budget}; "
                            f"retry_safety={safety.value}; {reason}"
                        ),
                        needed=(
                            "Resolve the failure or adjust the process within the approved "
                            "boundary, then explicitly confirm continuation. Replying does "
                            "not reset the consumed Attempt or Goal budgets."
                        ),
                    ),
                )
            stopped_node = node.suspend() if intervention is not None else node.stall()
            uow.states.put_attempt(terminal_attempt)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, stopped_node))
            uow.events.append(
                self._event(
                    terminal_event,
                    run,
                    attempt.attempt_id,
                    {
                        "attempt_id": attempt.attempt_id,
                        "reason": reason,
                        "retry_safety": safety.value,
                    },
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_SUSPENDED
                    if intervention is not None
                    else EventType.PLAN_NODE_STALLED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "attempt_id": attempt_id,
                        "reason": reason,
                        "retry_safety": safety.value,
                        "budget_consumed": retry_count,
                        "budget_limit": self._attempt_budget,
                        "intervention_id": None
                        if intervention is None
                        else intervention["intervention_id"],
                    },
                )
            )
            if intervention is not None:
                uow.commit()
                return run
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_RETRY_SCHEDULED,
                    run,
                    attempt.attempt_id,
                    {
                        "attempt_id": attempt.attempt_id,
                        "plan_node_id": attempt.plan_node_id,
                        "reason": reason,
                        "retry_safety": safety.value,
                    },
                )
            )
            uow.commit()
            return run

    def _recover_stalled_nodes(self, uow: UnitOfWork, run: Run, plan: PlanRevision) -> PlanRevision:
        """Release stopped safe retries on admission, never human suspensions."""
        stalled = tuple(node for node in plan.nodes if node.status is PlanNodeStatus.STALLED)
        if not stalled:
            return plan
        attempts = uow.states.list_attempts(run.run_id)
        retry_events = {
            stored.event.payload.get("attempt_id"): stored.event.payload
            for stored in uow.events.list_events()
            if stored.event.run_id == run.run_id
            and stored.event.type is EventType.ATTEMPT_RETRY_SCHEDULED
        }
        for node in stalled:
            node_attempts = tuple(
                item for item in attempts if item.plan_node_id == node.plan_node_id
            )
            latest = max(node_attempts, key=lambda item: item.sequence, default=None)
            fact = None if latest is None else retry_events.get(latest.attempt_id)
            if (
                latest is None
                or latest.status
                not in {AttemptStatus.FAILED, AttemptStatus.INTERRUPTED, AttemptStatus.TIMED_OUT}
                or fact is None
                or fact.get("plan_node_id") != node.plan_node_id
                or not RetrySafety(str(fact.get("retry_safety"))).allows_retry
            ):
                raise OrchestrationError("Stalled node lacks a settled, host-approved safe retry")
            plan = _replace_node(plan, node.recover())
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_RECOVERED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "attempt_id": latest.attempt_id,
                        "recovery": "safe_retry",
                    },
                )
            )
        uow.states.put_execution_plan(run.run_id, plan)
        return plan

    def time_out_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> Run:
        """Time out unknown in-flight work without creating replacement side effects."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            branch_local = _branch_containing(plan, node.plan_node_id) is not None
            timed_out = self._apply_worker_event_receipts(
                uow,
                attempt.time_out(reason, at=self._clock()),
                worker_event_receipts,
            )
            failed_node = node.fail()
            uow.states.put_attempt(timed_out)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, failed_node))
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_TIMED_OUT,
                    run,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "reason": reason},
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": reason},
                )
            )
            if not branch_local:
                paused = run.pause()
                uow.states.put_run(paused)
                uow.events.append(
                    self._event(
                        EventType.RUN_PAUSED,
                        paused,
                        run.run_id,
                        pause_event_payload(
                            run.run_id,
                            PauseCause.UNKNOWN_EXECUTION,
                            reason=reason,
                        ),
                    )
                )
            uow.commit()
            return run if branch_local else paused

    def fail_attempt(self, attempt_id: ID, reason: str) -> Run:
        """Fail an Attempt and its Run when an explicit resource budget is exhausted."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            failed_attempt = attempt.fail(reason, at=self._clock())
            failed_node = node.fail()
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_attempt(failed_attempt)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, failed_node))
            uow.states.put_run(failed_run)
            events: tuple[tuple[EventType, ID, dict[str, JsonValue]], ...] = (
                (
                    EventType.ATTEMPT_FAILED,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "reason": reason},
                ),
                (
                    EventType.PLAN_NODE_FAILED,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": reason},
                ),
                (
                    EventType.RUN_FAILED,
                    run.run_id,
                    {"run_id": run.run_id, "reason": reason},
                ),
            )
            for event_type, correlation_id, payload in events:
                uow.events.append(self._event(event_type, failed_run, correlation_id, payload))
            uow.commit()
            return failed_run

    def _load_attempt_context(self, attempt_id: ID) -> _ExecutionContext:
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run, plan, goal, contract = self._load_run_context(uow, attempt.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            if attempt.status is not AttemptStatus.RUNNING:
                raise OrchestrationError(f"attempt {attempt.attempt_id} is not running")
            if node.status is not PlanNodeStatus.RUNNING:
                raise OrchestrationError(f"PlanNode {node.plan_node_id} is not running")
            check_specs = _required_check_specs(uow, plan, node)
        return _ExecutionContext(run, plan, goal, contract, node, attempt, check_specs)

    def _pause_goal_budget(self, uow: UnitOfWork, run: Run, used: int, limit: int) -> Run:
        paused = run.pause()
        uow.states.put_run(paused)
        uow.events.append(
            self._event(
                EventType.RUN_PAUSED,
                paused,
                run.run_id,
                pause_event_payload(
                    run.run_id,
                    PauseCause.GOAL_BUDGET_EXHAUSTED,
                    reason=f"Goal Worker Attempt budget exhausted: consumed={used}, limit={limit}",
                    goal_id=run.goal_id,
                    budget_consumed=used,
                    budget_limit=limit,
                ),
            )
        )
        return paused

    def _prepare(self, run_id: ID) -> _ExecutionContext | None:
        with self._uow_factory() as uow:
            run, plan, goal, contract = self._load_run_context(uow, run_id)
            if run.status is RunStatus.PENDING:
                run = run.start(at=self._clock())
                uow.states.put_run(run)
                uow.events.append(
                    self._event(EventType.RUN_STARTED, run, run.run_id, {"run_id": run.run_id})
                )
            elif run.status is not RunStatus.RUNNING:
                raise OrchestrationError(
                    f"run {run.run_id} must be pending or running, not {run.status.value}"
                )

            plan = self._recover_stalled_nodes(uow, run, plan)
            exhausted_reason = _exhausted_branch_reason(plan)
            if exhausted_reason is not None:
                failed_run = run.fail(exhausted_reason, at=self._clock())
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        EventType.RUN_FAILED,
                        failed_run,
                        failed_run.run_id,
                        {"run_id": failed_run.run_id, "reason": exhausted_reason},
                    )
                )
                uow.commit()
                raise WorkerFailedError(exhausted_reason)

            already_ready = tuple(
                node for node in plan.nodes if node.status is PlanNodeStatus.READY
            )
            candidates = already_ready or ready_nodes(plan)
            if not candidates:
                raise OrchestrationError(
                    f"run {run.run_id} has no schedulable PlanNode while still running"
                )
            ready_node = candidates[0]
            attempts = uow.states.list_attempts(run.run_id)
            if any(attempt.status is AttemptStatus.RUNNING for attempt in attempts):
                raise OrchestrationError(f"run {run.run_id} already has a running Attempt")
            budget = goal_worker_budget(uow, run.goal_id)
            if budget is not None:
                used = goal_worker_attempts_used(uow, run.goal_id)
                if used >= budget.max_worker_attempts:
                    self._pause_goal_budget(uow, run, used, budget.max_worker_attempts)
                    uow.commit()
                    raise AttemptBudgetExceededError("Goal Worker Attempt budget exhausted")
            if len(attempts) >= self._attempt_budget:
                reason = (
                    f"attempt budget exhausted before PlanNode {ready_node.plan_node_id}: "
                    f"consumed={len(attempts)}, limit={self._attempt_budget}"
                )
                failed_run = run.fail(reason, at=self._clock())
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        EventType.RUN_FAILED,
                        failed_run,
                        failed_run.run_id,
                        {"run_id": failed_run.run_id, "reason": reason},
                    )
                )
                uow.commit()
                raise AttemptBudgetExceededError(reason)

            if ready_node.status is PlanNodeStatus.PENDING:
                ready_node = ready_node.mark_ready()
                plan = _replace_node(plan, ready_node)
                uow.states.put_execution_plan(run.run_id, plan)
                uow.events.append(
                    self._event(
                        EventType.PLAN_NODE_READIED,
                        run,
                        ready_node.plan_node_id,
                        {"plan_node_id": ready_node.plan_node_id},
                    )
                )
            running_node = ready_node.start()
            plan = _replace_node(plan, running_node)
            required_specs = _required_check_specs(uow, plan, running_node)
            attempt = Attempt(
                run_id=run.run_id,
                process_revision_id=_required_process_id(uow, run.run_id),
                plan_node_id=running_node.plan_node_id,
                sequence=len(attempts) + 1,
                attempt_id=self._id_factory(),
                created_at=self._clock(),
            ).start(at=self._clock())
            uow.states.put_execution_plan(run.run_id, plan)
            uow.states.put_attempt(attempt)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_STARTED,
                    run,
                    running_node.plan_node_id,
                    {"plan_node_id": running_node.plan_node_id},
                )
            )
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_STARTED,
                    run,
                    attempt.attempt_id,
                    {
                        "attempt_id": attempt.attempt_id,
                        "plan_node_id": attempt.plan_node_id,
                        "attempt_budget_consumed": len(attempts) + 1,
                        "attempt_budget_limit": self._attempt_budget,
                    },
                )
            )
            uow.commit()
        return _ExecutionContext(
            run,
            plan,
            goal,
            contract,
            running_node,
            attempt,
            required_specs,
        )

    def _evaluate_completed_evaluator(self, run_id: ID) -> bool:
        with self._uow_factory() as uow:
            run, plan, _, _ = self._load_run_context(uow, run_id)
            if run.status is not RunStatus.RUNNING:
                return False
            completed_evaluator = None
            evaluator_branches: tuple[Branch, ...] = ()
            for node in plan.nodes:
                if node.kind is not PlanNodeKind.EVALUATOR:
                    continue
                if node.status is not PlanNodeStatus.COMPLETED:
                    continue
                branches = _evaluator_branches(plan, node)
                if all(branch.status is BranchStatus.ACTIVE for branch in branches):
                    completed_evaluator = node
                    evaluator_branches = branches
                    break
            if completed_evaluator is None:
                return False
            context = _branch_evaluation_context(
                uow,
                plan,
                run,
                branches=evaluator_branches,
                artifact_store=self._artifact_store,
            )
            evaluator_attempts = tuple(
                attempt
                for attempt in sorted(
                    uow.states.list_attempts(run.run_id), key=lambda item: item.sequence
                )
                if attempt.plan_node_id == completed_evaluator.plan_node_id
                and attempt.status is AttemptStatus.SUCCEEDED
            )
            evaluator_attempt = evaluator_attempts[-1] if evaluator_attempts else None
            evaluator_artifact_ids = (
                set() if evaluator_attempt is None else set(evaluator_attempt.artifact_ids)
            )
            evaluator_artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(run.run_id)
                if artifact.artifact_id in evaluator_artifact_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
        try:
            if self._branch_evaluator is None:
                if len(evaluator_artifacts) != 1:
                    raise BranchSelectionProtocolError(
                        "Evaluator must persist exactly one candidate BranchSelection Artifact"
                    )
                try:
                    document = self._artifact_store.read(evaluator_artifacts[0].artifact_id).decode(
                        "utf-8"
                    )
                except UnicodeDecodeError as error:
                    raise BranchSelectionProtocolError(
                        "Evaluator BranchSelection Artifact must be UTF-8 JSON"
                    ) from error
                selection = parse_branch_selection(document, context)
            else:
                selection = self._branch_evaluator.evaluate(context)
            self._record_branch_selection(context, selection)
        except Exception as error:
            self._record_evaluation_failure(run.run_id, error)
            raise BranchEvaluationError(
                f"run {run.run_id} branch evaluation failed: {type(error).__name__}: {error}"
            ) from error
        return True

    def validate_evaluator_candidate(self, attempt_id: ID, document: str) -> None:
        """Validate selection before a Worker finishes, so it can repair its proposal."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            evaluator = _required_node(plan, attempt.plan_node_id)
            if evaluator.kind is not PlanNodeKind.EVALUATOR:
                raise OrchestrationError("Only evaluator nodes submit branch selections")
            context = _branch_evaluation_context(
                uow,
                plan,
                run,
                branches=_evaluator_branches(plan, evaluator),
                artifact_store=self._artifact_store,
            )
        parse_branch_selection(document, context)

    def _record_branch_selection(
        self,
        context: BranchEvaluationContext,
        selection: BranchSelection,
    ) -> None:
        selection = validate_branch_selection(selection, context)
        branch_by_id = {branch.branch_id: branch for branch in context.plan.branches}
        selected = branch_by_id.get(selection.selected_branch_id)
        if selected is None or selected.status is not BranchStatus.ACTIVE:
            raise OrchestrationError(
                f"BranchEvaluator selected unavailable Branch {selection.selected_branch_id}"
            )
        node_by_id = {node.plan_node_id: node for node in context.plan.nodes}
        if any(
            node_by_id[node_id].status is not PlanNodeStatus.COMPLETED
            for node_id in selected.node_ids
        ):
            raise OrchestrationError(
                f"BranchEvaluator selected non-viable Branch {selected.branch_id}"
            )
        active_sibling_ids = {
            branch.branch_id
            for branch in context.plan.branches
            if branch.branch_id != selected.branch_id
            and branch.status is BranchStatus.ACTIVE
            and branch.fork_node_id == selected.fork_node_id
            and branch.merge_node_id == selected.merge_node_id
        }
        if set(selection.pruned_branch_ids) != active_sibling_ids:
            raise OrchestrationError(
                "BranchEvaluator pruned Branches do not match the active sibling set"
            )
        artifact_by_id = {artifact.artifact_id: artifact for artifact in context.artifacts}
        attempt_by_id = {attempt.attempt_id: attempt for attempt in context.attempts}
        active_sibling_group = {selected.branch_id, *active_sibling_ids}
        viable_sibling_group = {
            branch_id
            for branch_id in active_sibling_group
            if all(
                node_by_id[node_id].status is PlanNodeStatus.COMPLETED
                for node_id in branch_by_id[branch_id].node_ids
            )
        }
        compared_branch_ids: set[ID] = set()
        for artifact_id in selection.evidence_artifact_ids:
            artifact = artifact_by_id.get(artifact_id)
            artifact_plan_node_id = (
                None if artifact is None else context.effective_plan_node_id(artifact)
            )
            attempt = (
                None
                if artifact is None or artifact.attempt_id is None
                else attempt_by_id.get(artifact.attempt_id)
            )
            artifact_branch = (
                None
                if artifact_plan_node_id is None
                else _branch_containing(context.plan, artifact_plan_node_id)
            )
            if (
                artifact is None
                or artifact_branch is None
                or artifact_branch.branch_id not in active_sibling_group
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or attempt is None
                or attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise OrchestrationError(
                    "BranchEvaluator selection evidence is outside active Branch candidates"
                )
            compared_branch_ids.add(artifact_branch.branch_id)
        if not viable_sibling_group.issubset(compared_branch_ids):
            raise OrchestrationError("BranchEvaluator did not compare every viable Branch")
        for artifact_id in selection.selected_artifact_ids:
            artifact = artifact_by_id.get(artifact_id)
            attempt = (
                None
                if artifact is None or artifact.attempt_id is None
                else attempt_by_id.get(artifact.attempt_id)
            )
            if (
                artifact is None
                or context.effective_plan_node_id(artifact) not in selected.node_ids
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or attempt is None
                or attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise OrchestrationError(
                    "BranchEvaluator selected Artifacts do not belong to the selected Branch"
                )

        with self._uow_factory() as uow:
            run = _required_run(uow, context.run.run_id)
            plan = _required_plan(uow, run.run_id)
            if run != context.run or plan != context.plan:
                raise OrchestrationError(
                    f"run {run.run_id} changed while BranchEvaluator was deciding"
                )
            current_context = _branch_evaluation_context(
                uow,
                plan,
                run,
                branches=tuple(
                    branch
                    for branch in plan.branches
                    if (branch.fork_node_id, branch.merge_node_id)
                    == (selected.fork_node_id, selected.merge_node_id)
                ),
                artifact_store=self._artifact_store,
            )
            if current_context != context:
                raise OrchestrationError("Branch candidate evidence changed during evaluation")
            replacements = {
                selected.branch_id: selected.select(),
                **{
                    branch_id: branch_by_id[branch_id].prune()
                    for branch_id in selection.pruned_branch_ids
                },
            }
            pruned_nodes: list[PlanNode] = []
            nodes = list(plan.nodes)
            for branch_id in selection.pruned_branch_ids:
                branch = branch_by_id[branch_id]
                for index, node in enumerate(nodes):
                    if node.plan_node_id in branch.node_ids and node.status in {
                        PlanNodeStatus.PENDING,
                        PlanNodeStatus.READY,
                    }:
                        pruned = node.prune()
                        nodes[index] = pruned
                        pruned_nodes.append(pruned)
            plan = _rehydrate_plan(
                plan,
                nodes=tuple(nodes),
                branches=tuple(
                    replacements.get(branch.branch_id, branch) for branch in plan.branches
                ),
            )
            uow.states.put_execution_plan(run.run_id, plan)
            evidence: list[JsonValue] = list(selection.evidence_artifact_ids)
            selected_artifact_ids: list[JsonValue] = list(selection.selected_artifact_ids)
            uow.events.append(
                self._event(
                    EventType.BRANCH_SELECTED,
                    run,
                    selected.branch_id,
                    {
                        "branch_id": selected.branch_id,
                        "fork_node_id": selected.fork_node_id,
                        "criterion": selection.criterion,
                        "evidence_artifact_ids": evidence,
                        "compared_artifact_ids": evidence,
                        "selected_artifact_ids": selected_artifact_ids,
                        "explanation": selection.explanation,
                    },
                )
            )
            for branch_id in selection.pruned_branch_ids:
                uow.events.append(
                    self._event(
                        EventType.BRANCH_PRUNED,
                        run,
                        branch_id,
                        {
                            "branch_id": branch_id,
                            "selected_branch_id": selected.branch_id,
                            "criterion": selection.criterion,
                            "evidence_artifact_ids": evidence,
                            "compared_artifact_ids": evidence,
                            "selected_artifact_ids": selected_artifact_ids,
                            "explanation": selection.explanation,
                        },
                    )
                )
            for node in pruned_nodes:
                uow.events.append(
                    self._event(
                        EventType.PLAN_NODE_PRUNED,
                        run,
                        node.plan_node_id,
                        {"plan_node_id": node.plan_node_id},
                    )
                )
            uow.commit()

    def _record_evaluation_failure(self, run_id: ID, error: Exception) -> None:
        reason = f"Branch evaluation failed: {type(error).__name__}: {error}"
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_run(failed_run)
            uow.events.append(
                self._event(
                    EventType.RUN_FAILED,
                    failed_run,
                    failed_run.run_id,
                    {"run_id": failed_run.run_id, "reason": reason},
                )
            )
            uow.commit()

    def _worker_inputs(
        self, context: _ExecutionContext | AdoptedResultInputs
    ) -> tuple[ArtifactInputSnapshot, ...]:
        plan = context.plan_revision
        node_by_id = {node.plan_node_id: node for node in plan.nodes}
        selected_branch_sources: set[ID] = set()
        selected_branches: dict[ID, Branch] = {}
        if context.plan_node.kind is PlanNodeKind.EVALUATOR:
            evaluator_branches = _evaluator_branches(plan, context.plan_node)
            source_node_ids = {
                node_id
                for branch in evaluator_branches
                if branch.status is BranchStatus.ACTIVE
                for node_id in branch.node_ids
            }
        elif context.plan_node.kind is PlanNodeKind.MERGE:
            selected = _required_selected_branch(plan, context.plan_node.plan_node_id)
            source_node_ids = set(selected.node_ids)
        else:
            source_node_ids = set()
            dependencies = set(context.plan_node.required_dependency_ids)
            dependencies.update(
                edge.source_node_id
                for edge in plan.edges
                if edge.target_node_id == context.plan_node.plan_node_id
                and edge.edge_type is EdgeType.EXPLORATION
            )

            def resolve(source_id: ID, trail: frozenset[ID] = frozenset()) -> None:
                source = node_by_id.get(source_id)
                if source is None:
                    raise OrchestrationError(
                        f"Upstream PlanNode {source_id} is missing from the execution plan"
                    )
                if source.kind is not PlanNodeKind.EVALUATOR:
                    source_node_ids.add(source_id)
                    return
                if source_id in trail:
                    raise OrchestrationError(
                        f"Evaluator dependency cycle reaches PlanNode {source_id}"
                    )
                branches = _evaluator_branches(plan, source)
                selected = tuple(
                    branch for branch in branches if branch.status is BranchStatus.SELECTED
                )
                if len(selected) != 1:
                    raise OrchestrationError(
                        f"Evaluator {source_id} requires exactly one selected Branch before input"
                    )
                selected_branch = selected[0]
                if any(
                    node_by_id[node_id].status is not PlanNodeStatus.COMPLETED
                    for node_id in selected_branch.node_ids
                ):
                    raise OrchestrationError(
                        f"Selected Branch {selected_branch.branch_id} has incomplete inputs"
                    )
                selected_branches[selected_branch.branch_id] = selected_branch
                selected_branch_sources.update(selected_branch.node_ids)
                next_trail = trail | {source_id}
                for node_id in selected_branch.node_ids:
                    resolve(node_id, next_trail)

            for source_id in sorted(dependencies):
                resolve(source_id)
        if not source_node_ids:
            return ()
        adopted_inputs: dict[ID, ResultAdoption] = {}
        with self._uow_factory() as uow:
            latest = {
                attempt.plan_node_id: attempt.attempt_id
                for attempt in sorted(
                    uow.states.list_attempts(context.run.run_id), key=lambda item: item.sequence
                )
                if attempt.status is AttemptStatus.SUCCEEDED
                and attempt.plan_node_id in source_node_ids
            }
            artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(context.run.run_id)
                if artifact.plan_node_id in source_node_ids
                and artifact.attempt_id == latest.get(artifact.plan_node_id)
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
            input_nodes = {artifact.artifact_id: artifact.plan_node_id for artifact in artifacts}
            for adoption in uow.states.list_result_adoptions(context.run.run_id):
                target_id = adoption.target_plan_node_id
                if target_id not in source_node_ids or target_id in latest:
                    continue
                target_node = node_by_id[target_id]
                if target_node.status is not PlanNodeStatus.COMPLETED:
                    continue
                producer = _required_attempt(uow, adoption.source_attempt_id)
                self._validate_adopted_scope(
                    uow, context.run, plan, target_node, producer, adoption
                )
                retained = self._adopted_artifacts(uow, producer, adoption)
                for artifact in retained:
                    if artifact.artifact_id in input_nodes:
                        raise OrchestrationError(
                            "One Artifact has ambiguous target input bindings; consolidate its use"
                        )
                    adopted_inputs[artifact.artifact_id] = adoption
                    input_nodes[artifact.artifact_id] = target_id
                artifacts = (*artifacts, *retained)
            if selected_branches:
                selected_artifact_ids: set[ID] = set()
                for branch in selected_branches.values():
                    selected_event = next(
                        (
                            stored.event
                            for stored in reversed(uow.events.list_events())
                            if stored.event.run_id == context.run.run_id
                            and stored.event.type is EventType.BRANCH_SELECTED
                            and stored.event.correlation_id == branch.branch_id
                        ),
                        None,
                    )
                    if selected_event is None:
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has no persisted selection Event"
                        )
                    payload = selected_event.payload
                    if (
                        payload.get("branch_id") != branch.branch_id
                        or payload.get("fork_node_id") != branch.fork_node_id
                    ):
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has an invalid selection Event"
                        )
                    values = payload.get("selected_artifact_ids")
                    if not isinstance(values, list) or not values:
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has no selected Artifact IDs"
                        )
                    try:
                        normalized = tuple(
                            normalize_id(value) for value in values if isinstance(value, str)
                        )
                    except ValueError as error:
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has invalid selected Artifact IDs"
                        ) from error
                    if len(normalized) != len(values) or len(set(normalized)) != len(normalized):
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has invalid selected Artifact IDs"
                        )
                    branch_artifact_ids = {
                        artifact.artifact_id
                        for artifact in artifacts
                        if input_nodes[artifact.artifact_id] in branch.node_ids
                    }
                    if not set(normalized).issubset(branch_artifact_ids):
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} Artifact inputs are stale, "
                            "incomplete, or outside that Branch"
                        )
                    selected_artifact_ids.update(normalized)
                artifacts = tuple(
                    artifact
                    for artifact in artifacts
                    if input_nodes[artifact.artifact_id] not in selected_branch_sources
                    or artifact.artifact_id in selected_artifact_ids
                )
        return self._artifact_input_snapshots(artifacts, adoptions=adopted_inputs)

    def _worker_context(
        self,
        context: _ExecutionContext,
        artifact_inputs: tuple[ArtifactInputSnapshot, ...],
    ) -> dict[str, JsonValue]:
        if context.plan_node.kind is PlanNodeKind.EVALUATOR:
            evaluator_branches = _evaluator_branches(context.plan_revision, context.plan_node)
            return {
                "process_revision_id": context.attempt.process_revision_id,
                **self._code_task_context(context),
                **self._rework_context(context),
                "approved_design_document": context.plan_revision.design_document,
                "candidate_branches": _candidate_branch_context(
                    context.plan_revision,
                    artifact_inputs,
                    branch_group=evaluator_branches,
                ),
            }
        if context.plan_node.kind is not PlanNodeKind.MERGE:
            return {
                "process_revision_id": context.attempt.process_revision_id,
                **self._code_task_context(context),
                **self._rework_context(context),
                "approved_design_document": context.plan_revision.design_document,
            }
        selected = _required_selected_branch(
            context.plan_revision,
            context.plan_node.plan_node_id,
        )
        with self._uow_factory() as uow:
            selected_event = next(
                (
                    stored.event
                    for stored in reversed(uow.events.list_events())
                    if stored.event.run_id == context.run.run_id
                    and stored.event.type is EventType.BRANCH_SELECTED
                    and stored.event.correlation_id == selected.branch_id
                ),
                None,
            )
        selected_artifacts = tuple(
            artifact
            for artifact in artifact_inputs
            if artifact.effective_plan_node_id in selected.node_ids
        )
        if selected_event is None:
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has no persisted selection Event"
            )
        selection = selected_event.payload
        evidence_ids = selection.get("evidence_artifact_ids")
        selected_ids = selection.get("selected_artifact_ids")
        criterion = selection.get("criterion")
        explanation = selection.get("explanation")
        if (
            selection.get("branch_id") != selected.branch_id
            or selection.get("fork_node_id") != selected.fork_node_id
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or not isinstance(selected_ids, list)
            or not selected_ids
            or not isinstance(criterion, str)
            or not criterion.strip()
            or not isinstance(explanation, str)
            or not explanation.strip()
        ):
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has invalid persisted selection evidence"
            )
        try:
            evidence_artifact_ids = [
                normalize_id(artifact_id)
                for artifact_id in evidence_ids
                if isinstance(artifact_id, str)
            ]
            selected_artifact_ids = [
                normalize_id(artifact_id)
                for artifact_id in selected_ids
                if isinstance(artifact_id, str)
            ]
        except ValueError as error:
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has invalid selection Artifact IDs"
            ) from error
        if len(evidence_artifact_ids) != len(evidence_ids) or len(selected_artifact_ids) != len(
            selected_ids
        ):
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has invalid selection Artifact IDs"
            )
        selected_payload_ids = set(selected_artifact_ids)
        selected_artifact_id_set = {artifact.artifact_id for artifact in selected_artifacts}
        if not selected_payload_ids.issubset(selected_artifact_id_set):
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} payload is outside its candidate Artifacts"
            )
        evidence_json: list[JsonValue] = [str(artifact_id) for artifact_id in evidence_artifact_ids]
        selected_json: list[JsonValue] = [str(artifact_id) for artifact_id in selected_artifact_ids]
        contents: list[JsonValue] = [
            artifact.to_prompt_dict()
            for artifact in selected_artifacts
            if artifact.artifact_id in selected_payload_ids
        ]
        if not contents:
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has no selected Artifact content"
            )
        return {
            "process_revision_id": context.attempt.process_revision_id,
            **self._code_task_context(context),
            **self._rework_context(context),
            "approved_design_document": context.plan_revision.design_document,
            "branch_selection": {
                "selected_branch_id": selected.branch_id,
                "fork_node_id": selected.fork_node_id,
                "criterion": criterion,
                "explanation": explanation,
                "evidence_artifact_ids": evidence_json,
                "compared_artifact_ids": evidence_json,
                "selected_artifact_ids": selected_json,
            },
            "selected_artifacts": contents,
        }

    def _rework_context(self, context: _ExecutionContext) -> dict[str, JsonValue]:
        with self._uow_factory() as uow:
            replies = [
                item
                for item in list_interventions(uow.events, context.run.run_id)
                if item.get("plan_node_id") == context.plan_node.plan_node_id
                and item.get("status") == "replied"
            ]
            reply_context: dict[str, JsonValue] = (
                {"intervention_reply": replies[-1]} if replies else {}
            )
            process_notices = attempt_process_interventions(uow, context.attempt)
            if process_notices:
                reply_context["process_interventions"] = list(process_notices)
                reply_context["process_interventions_instruction"] = (
                    "These are questions and replies pinned when this process was proposed. "
                    "Original node, Attempt and process IDs identify their source, not this task. "
                    "Use relevant replies as continuation facts within the approved boundary; "
                    "they do not grant permissions, change Gates, or resolve open questions. "
                    "A later intervention_reply for this task remains applicable. "
                    "Never assume a replacement route resolves unknown external effects."
                )
            reopening = next(
                (
                    stored.event
                    for stored in reversed(uow.events.list_events())
                    if stored.event.run_id == context.run.run_id
                    and stored.event.type is EventType.PLAN_NODE_REOPENED
                    and stored.event.correlation_id == context.plan_node.plan_node_id
                ),
                None,
            )
            if reopening is None:
                return reply_context
            review_attempt_id = reopening.payload.get("review_attempt_id")
            checks = tuple(
                check
                for check in uow.states.list_check_runs(context.run.run_id)
                if check.attempt_id == review_attempt_id
                and (check.result is None or not check.result.passed)
            )
            reviews = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(context.run.run_id)
                if artifact.attempt_id == review_attempt_id
                and artifact.kind is ArtifactKind.CANDIDATE
                and artifact.name == "review.json"
            )
        reports: list[JsonValue] = []
        for artifact in reviews:
            content = self._artifact_store.read(artifact.artifact_id).decode("utf-8")
            reports.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "content": content[:32000],
                    "truncated": len(content) > 32000,
                }
            )
        failures: list[JsonValue] = [
            {
                "check_id": check.check_id,
                "check_run_id": check.check_run_id,
                "reason": check.failure_reason
                if check.result is None
                else check.result.failure_reason,
                "output": None if check.result is None else (check.result.output or "")[-16000:],
            }
            for check in checks
        ]
        return {
            **reply_context,
            "phase_rework": {
                **reopening.payload,
                "failed_checks": failures,
                "review_reports": reports,
                "instruction": (
                    "Repair the assigned implementation using this review and Gate evidence. "
                    "Keep the approved requirements, interfaces, Gate conditions and permissions."
                ),
            },
        }

    def _code_task_context(self, context: _ExecutionContext) -> dict[str, JsonValue]:
        if self._execution_workspace is None:
            return {}
        with self._uow_factory() as uow:
            checks = tuple(
                check
                for check in uow.states.list_check_runs(context.run.run_id)
                if check.plan_node_id == context.plan_node.plan_node_id
                and (check.result is None or not check.result.passed)
            )[-3:]
        failures: list[JsonValue] = [
            {
                "check_id": check.check_id,
                "attempt_id": check.attempt_id,
                "adoption_id": check.adoption_id,
                "reason": (
                    check.failure_reason if check.result is None else check.result.failure_reason
                ),
                "output": None if check.result is None else (check.result.output or "")[-16000:],
            }
            for check in checks
        ]
        return {
            "code_execution": True,
            "gate_failures": failures,
            "workspace_semantics": (
                "The host prepares an isolated worktree containing applicable predecessor code. "
                "Continue from its files, resolve merge conflicts if present, and submit a concise "
                "candidate report. The host captures the actual code, not just the report. "
                "Nodes with approved Checks run their own Gate; the final Gate alone "
                "can complete the Run. A Reviewer submits evidence and recommendations, "
                "not code changes or a Gate decision."
            ),
        }

    def _artifact_input_snapshots(
        self,
        artifacts: tuple[Artifact, ...],
        *,
        adoptions: Mapping[ID, ResultAdoption] | None = None,
    ) -> tuple[ArtifactInputSnapshot, ...]:
        snapshots: list[ArtifactInputSnapshot] = []
        total_bytes = 0
        for artifact in artifacts:
            content = self._artifact_store.read(artifact.artifact_id)
            if len(content) > MAX_ARTIFACT_INPUT_BYTES:
                raise ArtifactInputBudgetExceeded(
                    f"Artifact {artifact.artifact_id} exceeds input limit "
                    f"{MAX_ARTIFACT_INPUT_BYTES} bytes"
                )
            total_bytes += len(content)
            if total_bytes > MAX_ARTIFACT_INPUT_TOTAL_BYTES:
                raise ArtifactInputBudgetExceeded(
                    f"Artifact inputs exceed total limit {MAX_ARTIFACT_INPUT_TOTAL_BYTES} bytes"
                )
            snapshots.append(
                ArtifactInputSnapshot.from_artifact(
                    artifact,
                    content,
                    adoption=None if adoptions is None else adoptions.get(artifact.artifact_id),
                )
            )
        return tuple(snapshots)

    def _validate_reviewer_result(self, context: _ExecutionContext, result: WorkerResult) -> None:
        if context.plan_node.kind is not PlanNodeKind.REVIEWER:
            return
        candidates = tuple(
            artifact for artifact in result.artifacts if artifact.kind is ArtifactKind.CANDIDATE
        )
        if len(candidates) != 1:
            raise ValueError("Reviewer must submit exactly one candidate review.json Artifact")
        review = candidates[0]
        validate_review_submission(
            {
                "name": review.name,
                "media_type": review.media_type,
                "content": review.content.decode("utf-8"),
            },
            (artifact.artifact_id for artifact in self._worker_inputs(context)),
        )

    def _store_candidate_artifacts(
        self,
        context: _ExecutionContext,
        result: WorkerResult,
    ) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        for candidate in result.artifacts:
            artifacts.append(
                self._store_artifact(
                    context,
                    kind=candidate.kind,
                    name=candidate.name,
                    media_type=candidate.media_type,
                    content=candidate.content,
                )
            )
        return tuple(artifacts)

    def _store_worker_error_artifacts(
        self,
        context: _ExecutionContext,
        error: WorkerExecutionError,
    ) -> tuple[Artifact, ...]:
        payloads = (
            (
                ArtifactKind.WORKER_OUTPUT,
                "worker-error-output.txt",
                error.raw_output,
            ),
            (ArtifactKind.LOG, "worker-error-stderr.log", error.log_output),
            (
                ArtifactKind.LOG,
                "worker-error-diagnostics.log",
                "\n".join(error.diagnostics).encode("utf-8"),
            ),
        )
        return tuple(
            self._store_artifact(
                context,
                kind=kind,
                name=name,
                media_type="text/plain; charset=utf-8",
                content=content,
            )
            for kind, name, content in payloads
            if content
        )

    def _store_artifact(
        self,
        context: _ExecutionContext,
        *,
        kind: ArtifactKind,
        name: str,
        media_type: str,
        content: bytes,
    ) -> Artifact:
        artifact_id = self._id_factory()
        artifact = Artifact(
            artifact_id=artifact_id,
            kind=kind,
            name=name,
            media_type=media_type,
            size_bytes=len(content),
            sha256=sha256(content).hexdigest(),
            relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
            created_at=self._clock(),
            run_id=context.run.run_id,
            plan_node_id=context.plan_node.plan_node_id,
            attempt_id=context.attempt.attempt_id,
        )
        self._artifact_store.put(artifact, content)
        return artifact

    def _record_candidate(
        self,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        result: WorkerResult,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> None:
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            evidence_artifacts = tuple(
                artifact
                for artifact in artifacts
                if artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
            succeeded = self._apply_worker_event_receipts(
                uow,
                attempt.succeed(
                    tuple(artifact.artifact_id for artifact in evidence_artifacts),
                    at=self._clock(),
                ),
                worker_event_receipts,
            )
            candidate_node = node.submit_candidate()
            plan = _replace_node(plan, candidate_node)
            for artifact in artifacts:
                uow.states.put_artifact(artifact)
                uow.events.append(
                    self._event(
                        EventType.ARTIFACT_CREATED,
                        run,
                        artifact.artifact_id,
                        {
                            "artifact_id": artifact.artifact_id,
                            "attempt_id": attempt.attempt_id,
                        },
                    )
                )
            uow.states.put_attempt(succeeded)
            uow.states.put_execution_plan(run.run_id, plan)
            outcome: dict[str, JsonValue] = {
                "attempt_id": attempt.attempt_id,
                "summary": result.summary,
            }
            if self._execution_workspace is not None:
                for candidate in result.artifacts:
                    if candidate.media_type != "application/vnd.ehai.code-snapshot+json":
                        continue
                    delivery = json_loads(candidate.content.decode("utf-8"))
                    if not isinstance(delivery, dict):
                        raise OrchestrationError("Host code snapshot must be an object")
                    delivery["diff_artifact_ids"] = [
                        artifact.artifact_id
                        for artifact in artifacts
                        if artifact.name == "solution.patch"
                    ]
                    outcome["code_delivery"] = delivery
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_SUCCEEDED,
                    run,
                    attempt.attempt_id,
                    outcome,
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_CANDIDATE_SUBMITTED,
                    run,
                    candidate_node.plan_node_id,
                    {"plan_node_id": candidate_node.plan_node_id},
                )
            )
            uow.commit()

    def _apply_worker_event_receipts(
        self,
        uow: UnitOfWork,
        attempt: Attempt,
        receipts: tuple[WorkerEventReceipt, ...],
    ) -> Attempt:
        if not receipts:
            return attempt
        for receipt in receipts:
            is_new = uow.states.record_worker_event(
                attempt.attempt_id,
                receipt.worker_event_id,
                at=receipt.occurred_at,
            )
            if is_new and receipt.records_usage:
                uow.events.append(
                    Event(
                        type=EventType.PROVIDER_USAGE_RECORDED,
                        correlation_id=attempt.attempt_id,
                        run_id=attempt.run_id,
                        payload={
                            "attempt_id": attempt.attempt_id,
                            "provider_cost": receipt.provider_cost,
                            "provider_cost_available": receipt.provider_cost is not None,
                        },
                        occurred_at=receipt.occurred_at,
                    )
                )
        return attempt.record_terminal_cursor(receipts[-1].cursor)

    def _check_and_complete(
        self,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        branch_local: bool,
        adoption_id: ID | None = None,
    ) -> Run:
        if not check_specs:
            return self._accept_intermediate(run_id, attempt_id, adoption_id=adoption_id)
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            if scope.run.run_id != run_id:
                raise OrchestrationError("Verification result does not match its target Run")
            verifying = scope.plan_node.status is PlanNodeStatus.VERIFYING
        if verifying:
            return self.finish_pending_gate(
                attempt_id, adoption_id=adoption_id, recover_interrupted_checks=True
            )
        context, prepared = self._start_checks(
            run_id, attempt_id, artifacts, check_specs, adoption_id=adoption_id
        )
        for spec, running_check in prepared:
            if spec.kind is CheckKind.HUMAN:
                continue
            assert context is not None
            try:
                executed = self._check_runner.run(spec, context)
                terminal = _rebind_check_run(running_check, executed)
            except Exception as error:
                terminal = running_check.fail(
                    f"CheckRunner failed: {type(error).__name__}: {error}",
                    at=self._clock(),
                )
            with self._uow_factory() as uow:
                self._persist_terminal_check(uow, _required_run(uow, run_id), terminal)
                uow.commit()
        return self.finish_pending_gate(attempt_id, adoption_id=adoption_id)

    def waiting_for_human(self, run_id: ID, *, only_if_idle: bool = False) -> bool:
        """Read durable open human requests without confusing them with operator pause."""
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                return False
            plan = _required_plan(uow, run.run_id)
            if only_if_idle and (
                ready_nodes(plan)
                or any(
                    node.status in {PlanNodeStatus.READY, PlanNodeStatus.STALLED}
                    for node in plan.nodes
                )
            ):
                return False
            verifying = {
                node.plan_node_id for node in plan.nodes if node.status is PlanNodeStatus.VERIFYING
            }
            return any(
                check.status is CheckRunStatus.RUNNING
                and check.human_request is not None
                and check.plan_node_id in verifying
                for check in uow.states.list_check_runs(run_id)
            )

    def record_human_decision(
        self,
        uow: UnitOfWork,
        check_run_id: ID,
        request_token: str,
        *,
        passed: bool,
        actor: str,
        comment: str,
    ) -> tuple[Run, ID]:
        """Validate and record a human result in the caller's receipt transaction."""
        check = uow.states.get_check_run(check_run_id)
        if check is None or check.human_request is None:
            raise OrchestrationError("Human Check request does not exist")
        scope = self._load_verification_scope(uow, check.attempt_id, check.adoption_id)
        run = scope.run
        plan = scope.plan_revision
        contract = scope.completion_contract
        attempt = scope.attempt
        node = scope.plan_node
        if (
            check.run_id != run.run_id
            or check.plan_node_id != node.plan_node_id
            or check.adoption_id != (None if scope.adoption is None else scope.adoption.adoption_id)
        ):
            raise OrchestrationError("Human Check request does not match its verification subject")
        latest = (
            tuple(
                item
                for item in uow.states.list_attempts(run.run_id)
                if item.plan_node_id == node.plan_node_id
            )
            if scope.adoption is None
            else (attempt,)
        )
        specs = _required_check_specs(uow, plan, node)
        spec = next((item for item in specs if item.check_id == check.check_id), None)
        if (
            run.status is not RunStatus.RUNNING
            or node.status is not PlanNodeStatus.VERIFYING
            or attempt.status is not AttemptStatus.SUCCEEDED
            or not latest
            or latest[-1].attempt_id != attempt.attempt_id
            or check.status is not CheckRunStatus.RUNNING
            or spec is None
            or spec.kind is not CheckKind.HUMAN
        ):
            raise OrchestrationError("Human Check request is no longer open for this execution")
        artifacts = scope.artifacts
        current_request = self._human_request(plan, contract, spec, artifacts)
        if check.human_request != current_request or request_token != current_request.request_token:
            raise OrchestrationError("Human Check request or approved evidence version changed")
        for artifact in artifacts:
            if (
                sha256(self._artifact_store.read(artifact.artifact_id)).hexdigest()
                != artifact.sha256
            ):
                raise OrchestrationError(
                    "Human Check Artifact content no longer matches its snapshot"
                )
        at = self._clock()
        result = CheckResult(
            check_id=check.check_id,
            check_run_id=check.check_run_id,
            run_id=check.run_id,
            plan_node_id=check.plan_node_id,
            attempt_id=check.attempt_id,
            adoption_id=check.adoption_id,
            passed=passed,
            evaluated_at=at,
            evidence_artifact_ids=tuple(item.artifact_id for item in artifacts),
            output=comment,
            failure_reason=None if passed else comment,
        )
        self._persist_terminal_check(
            uow, run, check.decide_human(result, actor=actor, comment=comment, at=at)
        )
        return run, attempt.attempt_id

    @staticmethod
    def _human_request(
        plan: PlanRevision,
        contract: CompletionContract,
        spec: CheckSpec,
        artifacts: tuple[Artifact, ...],
    ) -> HumanCheckRequest:
        return HumanCheckRequest(
            plan_revision_id=plan.plan_revision_id,
            completion_contract_id=contract.completion_contract_id,
            completion_contract_version=contract.version,
            question=spec.description,
            evidence=tuple(
                HumanCheckEvidence(artifact.artifact_id, artifact.sha256)
                for artifact in sorted(artifacts, key=lambda item: item.artifact_id)
            ),
        )

    def _load_verification_scope(
        self,
        uow: UnitOfWork,
        attempt_id: ID,
        adoption_id: ID | None,
    ) -> _VerificationScope:
        attempt = _required_attempt(uow, normalize_id(attempt_id))
        normalized_adoption_id = None if adoption_id is None else normalize_id(adoption_id)
        if normalized_adoption_id is None:
            run, plan, _, contract = self._load_run_context(uow, attempt.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(run.run_id)
                if artifact.artifact_id in attempt.artifact_ids
            )
            return _VerificationScope(run, plan, contract, node, attempt, None, artifacts)

        adoption = uow.states.get_result_adoption(normalized_adoption_id)
        if adoption is None:
            raise OrchestrationError(f"ResultAdoption {normalized_adoption_id} is not persisted")
        if adoption.adoption_id != normalized_adoption_id:
            raise OrchestrationError(
                f"ResultAdoption lookup returned another adoption for {normalized_adoption_id}"
            )
        if adoption.source_attempt_id != attempt.attempt_id:
            raise OrchestrationError(
                "Verification Attempt does not match the ResultAdoption source Attempt"
            )
        run, plan, _, contract = self._load_run_context(uow, adoption.target_run_id)
        node = _required_node(plan, adoption.target_plan_node_id)
        self._validate_adopted_scope(uow, run, plan, node, attempt, adoption)
        artifacts = self._adopted_artifacts(uow, attempt, adoption)
        return _VerificationScope(run, plan, contract, node, attempt, adoption, artifacts)

    @staticmethod
    def _validate_adopted_scope(
        uow: UnitOfWork,
        run: Run,
        plan: PlanRevision,
        node: PlanNode,
        attempt: Attempt,
        adoption: ResultAdoption,
    ) -> None:
        if (
            adoption.target_run_id != run.run_id
            or adoption.target_plan_revision_id != run.plan_revision_id
            or adoption.target_plan_node_id != node.plan_node_id
            or run.predecessor_run_id != adoption.source_run_id
            or attempt.run_id != adoption.source_run_id
            or attempt.plan_node_id != adoption.source_plan_node_id
            or attempt.attempt_id != adoption.source_attempt_id
            or attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise OrchestrationError("Verification has no matching adopted producer")
        if plan.status is not PlanRevisionStatus.APPROVED:
            raise OrchestrationError("Adopted target PlanRevision is not approved")
        if any(
            item.plan_node_id == node.plan_node_id for item in uow.states.list_attempts(run.run_id)
        ):
            raise OrchestrationError("Target execution has superseded the adopted result")

        approved = uow.states.get_plan_revision(run.plan_revision_id)
        if approved is None:
            raise OrchestrationError(
                f"Adopted target PlanRevision {run.plan_revision_id} is not persisted"
            )
        approved_node = _required_node(approved, node.plan_node_id)
        if _node_definition(node) != _node_definition(approved_node):
            raise OrchestrationError("Adopted target node changed after its acceptance")

        source_run = _required_run(uow, adoption.source_run_id)
        if (
            source_run.goal_id != run.goal_id
            or source_run.plan_revision_id != adoption.source_plan_revision_id
        ):
            raise OrchestrationError("Adopted source Run identity is no longer valid")
        source_plan = uow.states.get_plan_revision(adoption.source_plan_revision_id)
        if source_plan is None or (
            source_plan.status is not PlanRevisionStatus.APPROVED
            or source_plan.goal_id != source_run.goal_id
        ):
            raise OrchestrationError("Adopted source PlanRevision is no longer valid")
        process = uow.states.get_process_revision(adoption.source_process_revision_id)
        if process is None or (
            process.run_id != source_run.run_id
            or process.graph.plan_revision_id != adoption.source_plan_revision_id
            or process.graph.goal_id != source_run.goal_id
        ):
            raise OrchestrationError("Adopted source ProcessRevision is no longer valid")
        _required_node(process.graph, adoption.source_plan_node_id)

    @staticmethod
    def _adopted_artifacts(
        uow: UnitOfWork,
        attempt: Attempt,
        adoption: ResultAdoption,
    ) -> tuple[Artifact, ...]:
        expected = {item.artifact_id: item.sha256 for item in adoption.evidence}
        if set(attempt.artifact_ids) != set(expected):
            raise OrchestrationError("Adopted evidence no longer matches the source Attempt")
        artifacts_by_id = {
            artifact.artifact_id: artifact
            for artifact in uow.states.list_artifacts_for_run(adoption.source_run_id)
        }
        artifacts: list[Artifact] = []
        for artifact_id in attempt.artifact_ids:
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None or (
                artifact.run_id != adoption.source_run_id
                or artifact.plan_node_id != adoption.source_plan_node_id
                or artifact.attempt_id != adoption.source_attempt_id
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or artifact.sha256 != expected.get(artifact_id)
            ):
                raise OrchestrationError("Adopted verification evidence is inconsistent")
            artifacts.append(artifact)
        return tuple(artifacts)

    def finish_pending_gate(
        self,
        attempt_id: ID,
        *,
        adoption_id: ID | None = None,
        recover_interrupted_checks: bool = False,
    ) -> Run:
        """Finish persisted verification without rerunning completed automatic checks."""
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            attempt = scope.attempt
            run = scope.run
            plan = scope.plan_revision
            contract = scope.completion_contract
            node = scope.plan_node
            if run.status is not RunStatus.RUNNING or node.status is not PlanNodeStatus.VERIFYING:
                return run
            specs = _required_check_specs(uow, plan, node)
            expected_adoption_id = None if scope.adoption is None else scope.adoption.adoption_id
            all_checks = tuple(
                item
                for item in uow.states.list_check_runs(run.run_id)
                if (
                    item.attempt_id == attempt.attempt_id
                    and item.plan_node_id == node.plan_node_id
                    and item.adoption_id == expected_adoption_id
                )
            )
            checks = {item.check_id: item for item in all_checks}
            if any(
                check.run_id != run.run_id
                or check.plan_node_id != node.plan_node_id
                or check.attempt_id != attempt.attempt_id
                or check.adoption_id != expected_adoption_id
                for check in all_checks
            ):
                raise OrchestrationError(
                    "Persisted verification Check set belongs to another execution subject"
                )
            if len(checks) != len(all_checks) or set(checks) != set(node.required_check_ids):
                raise OrchestrationError(
                    "Persisted verification Check set is incomplete or ambiguous"
                )
            artifacts = scope.artifacts
            for spec in specs:
                check = checks[spec.check_id]
                if spec.kind is not CheckKind.HUMAN and check.status is CheckRunStatus.RUNNING:
                    if not recover_interrupted_checks:
                        return run
                    checks[spec.check_id] = check.interrupt(
                        "Automatic Check outcome was not persisted before recovery; not replayed",
                        at=self._clock(),
                    )
                    self._persist_terminal_check(uow, run, checks[spec.check_id])
            auto_passed = all(
                result is not None and result.passed
                for spec in specs
                if spec.kind is not CheckKind.HUMAN
                for result in (checks[spec.check_id].result,)
            )
            human_failed = any(
                check.human_request is not None
                and check.result is not None
                and not check.result.passed
                for check in checks.values()
            )
            waiting = False
            for spec in specs:
                check = checks[spec.check_id]
                if spec.kind is not CheckKind.HUMAN or check.status is not CheckRunStatus.RUNNING:
                    continue
                if not auto_passed or human_failed:
                    checks[spec.check_id] = check.cancel(
                        "Another required Check rejected this candidate", at=self._clock()
                    )
                    self._persist_terminal_check(uow, run, checks[spec.check_id])
                    continue
                if check.human_request is None:
                    request = self._human_request(plan, contract, spec, artifacts)
                    check = check.request_human(request)
                    checks[spec.check_id] = check
                    uow.states.put_check_run(check)
                    uow.events.append(
                        self._event(
                            EventType.CHECK_STARTED,
                            run,
                            check.check_run_id,
                            {
                                "check_id": check.check_id,
                                "check_run_id": check.check_run_id,
                                "awaiting_human": True,
                            },
                        )
                    )
                elif check.human_request != self._human_request(plan, contract, spec, artifacts):
                    raise OrchestrationError(
                        "Persisted Human Check request or accepted evidence version changed"
                    )
                waiting = True
            uow.commit()
            if waiting:
                return run
            terminal_checks = tuple(checks[spec.check_id] for spec in specs)
            run_id = run.run_id
            branch_local = _branch_containing(plan, node.plan_node_id) is not None
        results = tuple(
            check_run.result
            for check_run in terminal_checks
            if check_run.status is CheckRunStatus.COMPLETED and check_run.result is not None
        )
        gate = Gate(
            required_check_ids=tuple(check_run.check_id for check_run in terminal_checks),
            gate_id=self._id_factory(),
        )
        decision = gate.evaluate(
            results,
            run_id=run_id,
            plan_node_id=terminal_checks[0].plan_node_id,
            attempt_id=attempt_id,
            at=self._clock(),
            adoption_id=(None if scope.adoption is None else scope.adoption.adoption_id),
        )
        if not decision.passed:
            repair = self._execution_workspace is not None or any(
                phase.reviewer_node_id == decision.plan_node_id for phase in plan.phases
            )
            recorded = self._record_gate_failure(
                run_id,
                terminal_checks,
                decision,
                fail_run=not branch_local and not repair,
                repair=repair,
                adoption_id=(None if scope.adoption is None else scope.adoption.adoption_id),
            )
            if not recorded:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            if repair:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            if branch_local:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            raise GateRejectedError(
                f"run {run_id} failed Gate {decision.gate_id}: {decision.reason}"
            )
        return self._record_completion(
            run_id,
            terminal_checks,
            decision,
            artifacts,
            adoption_id=(None if scope.adoption is None else scope.adoption.adoption_id),
        )

    def _start_checks(
        self,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        adoption_id: ID | None = None,
    ) -> tuple[CheckContext | None, tuple[tuple[CheckSpec, CheckRun], ...]]:
        with self._uow_factory() as uow:
            scope = self._load_verification_scope(uow, attempt_id, adoption_id)
            run, plan, attempt, node = (
                scope.run,
                scope.plan_revision,
                scope.attempt,
                scope.plan_node,
            )
            if run.run_id != run_id:
                raise OrchestrationError("Check start does not match its target Run")
            if tuple(spec.check_id for spec in check_specs) != node.required_check_ids or any(
                not spec.required for spec in check_specs
            ):
                raise OrchestrationError(
                    f"PlanNode {node.plan_node_id} required CheckSpecs are missing or optional"
                )
            verifying_node = node.begin_verification()
            prepared_workspace = (
                None if scope.adoption is None else self._verification_workspace(scope)
            )
            plan = _replace_node(plan, verifying_node)
            check_runs = tuple(
                CheckRun(
                    run_id=run.run_id,
                    plan_node_id=node.plan_node_id,
                    attempt_id=attempt.attempt_id,
                    adoption_id=adoption_id,
                    check_id=check_id,
                    check_run_id=self._id_factory(),
                    created_at=self._clock(),
                ).start(at=self._clock())
                for check_id in node.required_check_ids
            )
            if not check_runs:
                raise OrchestrationError(f"PlanNode {node.plan_node_id} has no required Checks")
            uow.states.put_execution_plan(run.run_id, plan)
            for spec, check_run in zip(check_specs, check_runs, strict=True):
                uow.states.put_check_run(check_run)
                if spec.kind is CheckKind.HUMAN:
                    continue
                uow.events.append(
                    self._event(
                        EventType.CHECK_STARTED,
                        run,
                        check_run.check_run_id,
                        {
                            "check_id": check_run.check_id,
                            "check_run_id": check_run.check_run_id,
                        },
                    )
                )
            uow.commit()
        if all(spec.kind is CheckKind.HUMAN for spec in check_specs):
            return None, tuple(zip(check_specs, check_runs, strict=True))
        evidence_artifacts = tuple(
            artifact
            for artifact in artifacts
            if artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
        )
        context = CheckContext(
            run=run,
            attempt=attempt,
            plan_node=verifying_node,
            artifacts=evidence_artifacts,
            workspace=(prepared_workspace or self._verification_workspace(scope)),
            code_workspace=self._execution_workspace is not None or scope.adoption is not None,
            adoption=scope.adoption,
        )
        return context, tuple(zip(check_specs, check_runs, strict=True))

    def _required_execution_workspace(self, attempt_id: ID) -> Path:
        if self._execution_workspace is None:
            raise OrchestrationError("Code execution workspace resolver is missing")
        workspace = self._execution_workspace(attempt_id)
        if workspace is None:
            raise OrchestrationError(f"Code workspace for Attempt {attempt_id} is missing")
        return workspace

    def _persist_terminal_check(self, uow: UnitOfWork, run: Run, check: CheckRun) -> None:
        if check.ended_at is None:
            raise OrchestrationError("Only terminal Checks can record an outcome")
        if uow.states.get_check_run(check.check_run_id) == check:
            return
        uow.states.put_check_run(check)
        passed = check.result is not None and check.result.passed
        reason = (
            None
            if passed
            else (check.failure_reason if check.result is None else check.result.failure_reason)
        )
        uow.events.append(
            self._event(
                EventType.CHECK_PASSED if passed else EventType.CHECK_FAILED,
                run,
                check.check_run_id,
                {
                    "check_id": check.check_id,
                    "check_run_status": check.status.value,
                    "reason": reason,
                },
            )
        )

    def _record_completion(
        self,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        artifacts: tuple[Artifact, ...],
        *,
        adoption_id: ID | None = None,
    ) -> Run:
        with self._uow_factory() as uow:
            run, plan, goal, _ = self._load_run_context(uow, run_id)
            node = _required_node(plan, decision.plan_node_id)
            expected_adoption_id = None if adoption_id is None else normalize_id(adoption_id)
            if decision.adoption_id != expected_adoption_id:
                raise OrchestrationError(
                    "Gate decision adoption does not match its verification subject"
                )
            if any(
                check.run_id != run.run_id
                or check.plan_node_id != node.plan_node_id
                or check.attempt_id != decision.attempt_id
                or check.adoption_id != expected_adoption_id
                for check in check_runs
            ):
                raise OrchestrationError("Gate checks belong to another verification subject")
            if not self._current_verification(
                uow, run, node, decision.attempt_id, expected_adoption_id
            ):
                return run
            for check_run in check_runs:
                self._persist_terminal_check(uow, run, check_run)
            gate_payload: dict[str, JsonValue] = {"gate_id": decision.gate_id}
            if expected_adoption_id is not None:
                gate_payload.update(
                    {
                        "adoption_id": expected_adoption_id,
                        "target_plan_node_id": node.plan_node_id,
                        "source_attempt_id": decision.attempt_id,
                    }
                )
            gate_event = uow.events.append(
                self._event(
                    EventType.GATE_PASSED,
                    run,
                    decision.gate_id,
                    gate_payload,
                )
            )
            completed_node = node.complete(decision)
            plan = _replace_node(plan, completed_node)
            uow.states.put_execution_plan(run.run_id, plan)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_COMPLETED,
                    run,
                    completed_node.plan_node_id,
                    {"plan_node_id": completed_node.plan_node_id},
                )
            )
            checkpoint = Checkpoint(
                plan_revision=plan,
                process_revision_id=_required_process_id(uow, run.run_id),
                run=run,
                event_offset=gate_event.offset,
                gate_decision=decision,
                branch_selections=_selected_branch_mapping(plan),
                artifact_refs=tuple(artifact.artifact_id for artifact in artifacts),
                checkpoint_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_checkpoint(checkpoint)
            uow.events.append(
                self._event(
                    EventType.CHECKPOINT_CREATED,
                    run,
                    checkpoint.checkpoint_id,
                    {"checkpoint_id": checkpoint.checkpoint_id},
                )
            )
            if not _is_final_completion(plan, completed_node):
                uow.commit()
                return run
            completed_run = run.complete(decision, at=self._clock())
            satisfied_goal = goal.satisfy(decision)
            uow.states.put_run(completed_run)
            uow.states.put_goal(satisfied_goal)
            uow.events.append(
                self._event(
                    EventType.RUN_COMPLETED,
                    completed_run,
                    completed_run.run_id,
                    {"run_id": completed_run.run_id},
                )
            )
            uow.commit()
        return completed_run

    def _record_gate_failure(
        self,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        *,
        fail_run: bool,
        repair: bool = False,
        adoption_id: ID | None = None,
    ) -> bool:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, decision.plan_node_id)
            expected_adoption_id = None if adoption_id is None else normalize_id(adoption_id)
            if decision.adoption_id != expected_adoption_id:
                raise OrchestrationError(
                    "Gate decision adoption does not match its verification subject"
                )
            if not self._current_verification(
                uow, run, node, decision.attempt_id, expected_adoption_id
            ):
                return False
            if any(
                check.run_id != run.run_id
                or check.plan_node_id != node.plan_node_id
                or check.attempt_id != decision.attempt_id
                or check.adoption_id != expected_adoption_id
                for check in check_runs
            ):
                raise OrchestrationError("Gate checks belong to another verification subject")
            for check_run in check_runs:
                self._persist_terminal_check(uow, run, check_run)
            gate_payload: dict[str, JsonValue] = {
                "gate_id": decision.gate_id,
                "reason": decision.reason,
            }
            if expected_adoption_id is not None:
                gate_payload.update(
                    {
                        "adoption_id": expected_adoption_id,
                        "target_plan_node_id": node.plan_node_id,
                        "source_attempt_id": decision.attempt_id,
                    }
                )
            uow.events.append(
                self._event(
                    EventType.GATE_FAILED,
                    run,
                    decision.gate_id,
                    gate_payload,
                )
            )
            failed_node = node.fail()
            failed_plan = _replace_node(plan, failed_node)
            uow.states.put_execution_plan(run.run_id, failed_plan)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": decision.reason},
                )
            )
            if repair:
                if node.kind is PlanNodeKind.REVIEWER:
                    try:
                        self._reopen_reviewed_phase(uow, run, failed_plan, decision)
                    except (OrchestrationError, PlanTransitionError) as error:
                        if any(
                            attempt.status in {AttemptStatus.RUNNING, AttemptStatus.PENDING}
                            for attempt in uow.states.list_attempts(run.run_id)
                        ):
                            # Let the Runtime convergence path cancel pending work and
                            # quiesce live executions before persisting a paused Run.
                            raise
                        paused_reason = bounded_redacted_text(str(error), max_bytes=2_000)
                        if paused_reason is None:
                            paused_reason = "Reviewer phase rework could not be applied"
                        paused = run.pause()
                        uow.states.put_run(paused)
                        uow.events.append(
                            self._event(
                                EventType.RUN_PAUSED,
                                paused,
                                run.run_id,
                                pause_event_payload(
                                    run.run_id,
                                    PauseCause.REVIEW_REWORK,
                                    reason=paused_reason,
                                    gate_id=decision.gate_id,
                                ),
                            )
                        )
                else:
                    uow.states.put_execution_plan(
                        run.run_id, _replace_node(failed_plan, failed_node.retry())
                    )
                    uow.events.append(
                        self._event(
                            EventType.PLAN_NODE_READIED,
                            run,
                            node.plan_node_id,
                            {
                                "plan_node_id": node.plan_node_id,
                                "reason": "repair the candidate against the unchanged Gate",
                                "gate_id": decision.gate_id,
                            },
                        )
                    )
            if fail_run:
                failed_run = run.fail(
                    decision.reason or "Gate rejected candidate",
                    at=self._clock(),
                )
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        EventType.RUN_FAILED,
                        failed_run,
                        run.run_id,
                        {"run_id": run.run_id, "reason": decision.reason},
                    )
                )
            uow.commit()
            return True

    def _reopen_reviewed_phase(
        self,
        uow: UnitOfWork,
        run: Run,
        plan: PlanRevision,
        decision: GateDecision,
    ) -> None:
        if _review_rework_uses_planner(uow, run.run_id):
            if _review_rework_has_active_work(uow, run.run_id):
                # queue_ready_attempts will persist the Planner pause after
                # existing actors settle; do not reopen or roll back the failure.
                return
            # The existing convergence/pause handler retains completed inputs.
            # Policy validation and its cumulative budget belong to the
            # coordinator, not to a blanket Phase-node reopening operation.
            raise OrchestrationError(
                "Reviewer Gate rejected the candidate; configured Planner adjustment "
                "must determine the affected rework scope"
            )
        phase = next(
            (item for item in plan.phases if item.reviewer_node_id == decision.plan_node_id), None
        )
        if phase is None:
            raise OrchestrationError(
                "Rejected Reviewer has no Phase rework route; Planner input is required"
            )
        phase_nodes = set(phase.node_ids)
        affected = {
            node_id
            for node_id in phase.rework_node_ids
            if (branch := _branch_containing(plan, node_id)) is None
            or branch.status is not BranchStatus.PRUNED
        }
        if not affected:
            raise OrchestrationError("The Phase rework route has no retained implementation target")
        pending = list(affected)
        while pending:
            source = pending.pop()
            for edge in plan.edges:
                if (
                    edge.source_node_id == source
                    and edge.target_node_id in phase_nodes
                    and edge.target_node_id not in affected
                ):
                    affected.add(edge.target_node_id)
                    pending.append(edge.target_node_id)
        if phase.reviewer_node_id not in affected:
            raise OrchestrationError("Phase rework targets do not lead to the rejected Reviewer")
        if any(
            attempt.plan_node_id in affected
            and attempt.status in {AttemptStatus.RUNNING, AttemptStatus.PENDING}
            for attempt in uow.states.list_attempts(run.run_id)
        ):
            raise OrchestrationError("Phase rework must first quiesce affected live Attempts")
        evaluators = tuple(
            node
            for node in plan.nodes
            if node.plan_node_id in affected and node.kind is PlanNodeKind.EVALUATOR
        )
        invalid_groups = {
            (branch.fork_node_id, branch.merge_node_id)
            for evaluator in evaluators
            for branch in _evaluator_branches(plan, evaluator)
        }
        reopened = tuple(
            node.reopen_for_rework() for node in plan.nodes if node.plan_node_id in affected
        )
        node_replacements = {node.plan_node_id: node for node in reopened}
        invalidated = tuple(
            branch
            for branch in plan.branches
            if (branch.fork_node_id, branch.merge_node_id) in invalid_groups
            and branch.status is not BranchStatus.ACTIVE
        )
        branch_replacements = {
            branch.branch_id: branch.reopen_selection() for branch in invalidated
        }
        revised = _rehydrate_plan(
            plan,
            nodes=tuple(node_replacements.get(node.plan_node_id, node) for node in plan.nodes),
            branches=tuple(
                branch_replacements.get(branch.branch_id, branch) for branch in plan.branches
            ),
        )
        # Compute the whole transition before writing any rework facts.
        uow.states.put_execution_plan(run.run_id, revised)
        for branch in invalidated:
            uow.events.append(
                self._event(
                    EventType.BRANCH_SELECTION_INVALIDATED,
                    run,
                    branch.branch_id,
                    {
                        "branch_id": branch.branch_id,
                        "fork_node_id": branch.fork_node_id,
                        "merge_node_id": branch.merge_node_id,
                        "phase_id": phase.phase_id,
                        "gate_id": decision.gate_id,
                        "review_attempt_id": decision.attempt_id,
                        "reason": "candidate dependencies require rework",
                    },
                )
            )
        for node in reopened:
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_REOPENED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "phase_id": phase.phase_id,
                        "reviewer_node_id": phase.reviewer_node_id,
                        "gate_id": decision.gate_id,
                        "review_attempt_id": decision.attempt_id,
                        "reason": "repair the implementation against the unchanged Phase Gate",
                    },
                )
            )

    @staticmethod
    def _current_verification(
        uow: UnitOfWork,
        run: Run,
        node: PlanNode,
        attempt_id: ID,
        adoption_id: ID | None = None,
    ) -> bool:
        attempts = tuple(
            item
            for item in uow.states.list_attempts(run.run_id)
            if item.plan_node_id == node.plan_node_id
        )
        if adoption_id is not None:
            try:
                normalized_adoption_id = normalize_id(adoption_id)
                adoption = uow.states.get_result_adoption(normalized_adoption_id)
                attempt = uow.states.get_attempt(normalize_id(attempt_id))
                plan = uow.states.get_execution_plan(run.run_id)
                if adoption is None or attempt is None or plan is None:
                    return False
                if adoption.adoption_id != normalized_adoption_id:
                    return False
                current_node = _required_node(plan, node.plan_node_id)
                Orchestrator._validate_adopted_scope(
                    uow, run, plan, current_node, attempt, adoption
                )
                Orchestrator._adopted_artifacts(uow, attempt, adoption)
            except (OrchestrationError, ValueError):
                return False
            return (
                run.status is RunStatus.RUNNING
                and current_node == node
                and node.status is PlanNodeStatus.VERIFYING
                and not attempts
            )
        return (
            run.status is RunStatus.RUNNING
            and node.status is PlanNodeStatus.VERIFYING
            and bool(attempts)
            and attempts[-1].attempt_id == attempt_id
            and attempts[-1].status is AttemptStatus.SUCCEEDED
        )

    def _record_worker_diagnostics(
        self,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
    ) -> None:
        if not artifacts:
            return
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            self._record_artifacts(uow, run, attempt, artifacts)
            uow.commit()

    def _record_worker_failure(
        self,
        attempt_id: ID,
        error: Exception,
        artifacts: tuple[Artifact, ...],
        *,
        diagnostic_error: Exception | None = None,
        fail_run: bool,
    ) -> None:
        reason = f"{type(error).__name__}: {error}"
        if diagnostic_error is not None:
            reason = (
                f"{reason}; diagnostic Artifact persistence failed: "
                f"{type(diagnostic_error).__name__}: {diagnostic_error}"
            )
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            timed_out = isinstance(error, WorkerTimedOutError)
            failed_attempt = (
                attempt.time_out(reason, at=self._clock())
                if timed_out
                else attempt.fail(reason, at=self._clock())
            )
            failed_node = node.fail()
            self._record_artifacts(uow, run, attempt, artifacts)
            uow.states.put_attempt(failed_attempt)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, failed_node))
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_TIMED_OUT if timed_out else EventType.ATTEMPT_FAILED,
                    run,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "reason": reason},
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": reason},
                )
            )
            if fail_run:
                failed_run = run.fail(reason, at=self._clock())
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        EventType.RUN_FAILED,
                        failed_run,
                        run.run_id,
                        {"run_id": run.run_id, "reason": reason},
                    )
                )
            uow.commit()

    def _record_artifacts(
        self,
        uow: UnitOfWork,
        run: Run,
        attempt: Attempt,
        artifacts: tuple[Artifact, ...],
    ) -> None:
        for artifact in artifacts:
            uow.states.put_artifact(artifact)
            uow.events.append(
                self._event(
                    EventType.ARTIFACT_CREATED,
                    run,
                    artifact.artifact_id,
                    {
                        "artifact_id": artifact.artifact_id,
                        "attempt_id": attempt.attempt_id,
                    },
                )
            )

    def _await_controlled_cancellation(
        self,
        attempt_id: ID,
        error: WorkerCancelledError,
    ) -> Run:
        deadline = time.monotonic() + self._cancellation_settle_seconds
        while True:
            with self._uow_factory() as uow:
                attempt = _required_attempt(uow, attempt_id)
                run = _required_run(uow, attempt.run_id)
            if attempt.status is AttemptStatus.CANCELLED and run.status in {
                RunStatus.PAUSED,
                RunStatus.CANCELLED,
            }:
                return run
            if attempt.status is not AttemptStatus.RUNNING or run.status is not RunStatus.RUNNING:
                raise WorkerCancellationUnsettledError(
                    f"attempt {attempt.attempt_id} cancellation settled inconsistently: "
                    f"attempt={attempt.status.value}, run={run.status.value}"
                ) from error
            if time.monotonic() >= deadline:
                raise WorkerCancellationUnsettledError(
                    f"attempt {attempt.attempt_id} was cancelled by its Worker, but no "
                    "RunController pause/cancel transition was committed"
                ) from error
            time.sleep(min(0.01, self._cancellation_settle_seconds))

    def _record_artifact_failure(
        self,
        attempt_id: ID,
        result: WorkerResult,
        error: Exception,
    ) -> None:
        reason = f"Artifact persistence failed: {type(error).__name__}: {error}"
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            succeeded_attempt = attempt.succeed((), at=self._clock())
            failed_node = node.fail()
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_attempt(succeeded_attempt)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, failed_node))
            uow.states.put_run(failed_run)
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_SUCCEEDED,
                    run,
                    attempt.attempt_id,
                    {"attempt_id": attempt.attempt_id, "summary": result.summary},
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": reason},
                )
            )
            uow.events.append(
                self._event(
                    EventType.RUN_FAILED,
                    failed_run,
                    run.run_id,
                    {"run_id": run.run_id, "reason": reason},
                )
            )
            uow.commit()

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


def _branch_evaluation_context(
    uow: UnitOfWork,
    plan: PlanRevision,
    run: Run,
    *,
    branches: tuple[Branch, ...],
    artifact_store: ArtifactStore,
) -> BranchEvaluationContext:
    branch_nodes = {node_id for branch in branches for node_id in branch.node_ids}
    scoped_attempts = tuple(
        attempt
        for attempt in uow.states.list_attempts(run.run_id)
        if attempt.plan_node_id in branch_nodes
    )
    latest = {
        attempt.plan_node_id: attempt
        for attempt in sorted(scoped_attempts, key=lambda item: item.sequence)
        if attempt.status is AttemptStatus.SUCCEEDED
    }
    attempts = tuple(sorted(latest.values(), key=lambda item: item.sequence))
    artifact_ids = {artifact_id for attempt in attempts for artifact_id in attempt.artifact_ids}
    artifacts = tuple(
        artifact
        for artifact in uow.states.list_artifacts_for_run(run.run_id)
        if artifact.artifact_id in artifact_ids
    )
    adoptions: list[ResultAdoption] = []
    for adoption in uow.states.list_result_adoptions(run.run_id):
        if adoption.target_plan_node_id not in branch_nodes:
            continue
        node = _required_node(plan, adoption.target_plan_node_id)
        if node.status is not PlanNodeStatus.COMPLETED or node.plan_node_id in latest:
            continue
        producer = _required_attempt(uow, adoption.source_attempt_id)
        Orchestrator._validate_adopted_scope(uow, run, plan, node, producer, adoption)
        retained = Orchestrator._adopted_artifacts(uow, producer, adoption)
        for artifact in retained:
            content = artifact_store.read(artifact.artifact_id)
            if (
                len(content) != artifact.size_bytes
                or sha256(content).hexdigest() != artifact.sha256
            ):
                raise OrchestrationError("Adopted branch evidence bytes changed")
            if artifact.artifact_id in artifact_ids:
                raise OrchestrationError("Branch evidence has ambiguous adoption target nodes")
            artifact_ids.add(artifact.artifact_id)
        adoptions.append(adoption)
        attempts = (*attempts, producer)
        artifacts = (*artifacts, *retained)
    return BranchEvaluationContext(plan, run, attempts, artifacts, tuple(adoptions))


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


def _rebind_check_run(persisted: CheckRun, executed: CheckRun) -> CheckRun:
    """Attach a CheckRunner outcome to the already persisted running CheckRun ID."""
    if (
        persisted.run_id,
        persisted.plan_node_id,
        persisted.attempt_id,
        persisted.adoption_id,
        persisted.check_id,
    ) != (
        executed.run_id,
        executed.plan_node_id,
        executed.attempt_id,
        executed.adoption_id,
        executed.check_id,
    ):
        raise OrchestrationError("CheckRunner outcome belongs to a different verification subject")
    ended_at = executed.ended_at
    if ended_at is None:
        raise OrchestrationError(
            f"CheckRunner returned non-terminal CheckRun {executed.check_run_id}"
        )
    if executed.status is CheckRunStatus.COMPLETED and executed.result is not None:
        source = executed.result
        result = CheckResult(
            check_id=persisted.check_id,
            check_run_id=persisted.check_run_id,
            run_id=persisted.run_id,
            plan_node_id=persisted.plan_node_id,
            attempt_id=persisted.attempt_id,
            adoption_id=persisted.adoption_id,
            passed=source.passed,
            evaluated_at=source.evaluated_at,
            evidence_artifact_ids=source.evidence_artifact_ids,
            output=source.output,
            failure_reason=source.failure_reason,
        )
        return persisted.complete(result, at=ended_at)
    if executed.status is CheckRunStatus.TIMED_OUT:
        return persisted.time_out(
            executed.failure_reason or "Check adapter timed out",
            at=ended_at,
        )
    if executed.status is CheckRunStatus.FAILED:
        return persisted.fail(
            executed.failure_reason or "Check adapter failed",
            at=ended_at,
        )
    raise OrchestrationError(
        f"CheckRunner returned unsupported status {executed.status.value} for "
        f"Check {persisted.check_id}"
    )


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


def _selected_branch_mapping(plan: PlanRevision) -> dict[ID, ID]:
    return {
        branch.fork_node_id: branch.branch_id
        for branch in plan.branches
        if branch.status is BranchStatus.SELECTED
    }


def _candidate_branch_context(
    plan: PlanRevision,
    artifact_inputs: tuple[ArtifactInputSnapshot, ...],
    *,
    branch_group: tuple[Branch, ...],
) -> list[JsonValue]:
    """Build the Evaluator's explicit branch-to-candidate content map."""
    branches: list[JsonValue] = []
    node_by_id = {node.plan_node_id: node for node in plan.nodes}
    for branch in branch_group:
        if branch.status is not BranchStatus.ACTIVE:
            continue
        artifacts: list[JsonValue] = [
            artifact.to_prompt_dict()
            for artifact in artifact_inputs
            if artifact.effective_plan_node_id in branch.node_ids
        ]
        branches.append(
            {
                "branch_id": branch.branch_id,
                "label": branch.label,
                "fork_node_id": branch.fork_node_id,
                "merge_node_id": branch.merge_node_id,
                "node_ids": list(branch.node_ids),
                "viable": all(
                    node_by_id[node_id].status is PlanNodeStatus.COMPLETED
                    for node_id in branch.node_ids
                ),
                "artifacts": artifacts,
            }
        )
    return branches


def _selected_merge_inputs(
    context: dict[str, JsonValue],
    artifact_inputs: tuple[ArtifactInputSnapshot, ...],
) -> tuple[ArtifactInputSnapshot, ...]:
    """Restrict Merge Worker inputs to the persisted selected Artifact IDs."""
    selection = context.get("branch_selection")
    if not isinstance(selection, dict):
        raise OrchestrationError("Merge context has no validated Branch selection")
    selected_values = selection.get("selected_artifact_ids")
    if not isinstance(selected_values, list) or not selected_values:
        raise OrchestrationError("Merge context has no selected Artifact IDs")
    try:
        selected_ids = tuple(
            normalize_id(value) for value in selected_values if isinstance(value, str)
        )
    except ValueError as error:
        raise OrchestrationError("Merge context has invalid selected Artifact IDs") from error
    if len(selected_ids) != len(selected_values):
        raise OrchestrationError("Merge context has invalid selected Artifact IDs")
    input_by_id = {artifact.artifact_id: artifact for artifact in artifact_inputs}
    if len(input_by_id) != len(artifact_inputs) or any(
        artifact_id not in input_by_id for artifact_id in selected_ids
    ):
        raise OrchestrationError("Merge selected Artifact inputs are incomplete")
    return tuple(input_by_id[artifact_id] for artifact_id in selected_ids)


def _required_selected_branch(plan: PlanRevision, merge_node_id: ID) -> Branch:
    selected = tuple(
        branch
        for branch in plan.branches
        if branch.merge_node_id == merge_node_id and branch.status is BranchStatus.SELECTED
    )
    if len(selected) != 1:
        raise OrchestrationError(
            f"Merge PlanNode {merge_node_id} requires exactly one selected Branch"
        )
    return selected[0]


def _is_final_completion(plan: PlanRevision, completed_node: PlanNode) -> bool:
    if any(edge.source_node_id == completed_node.plan_node_id for edge in plan.edges):
        return False
    if len(plan.nodes) == 1:
        return True
    pruned_branch_node_ids = {
        node_id
        for branch in plan.branches
        if branch.status is BranchStatus.PRUNED
        for node_id in branch.node_ids
    }
    return all(
        node.plan_node_id == completed_node.plan_node_id
        or node.status in {PlanNodeStatus.COMPLETED, PlanNodeStatus.PRUNED}
        or (node.plan_node_id in pruned_branch_node_ids and node.status is PlanNodeStatus.FAILED)
        for node in plan.nodes
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


def _node_definition(node: PlanNode) -> tuple[object, ...]:
    """Return a PlanNode's immutable task definition without lifecycle status."""
    return (
        node.plan_node_id,
        node.title,
        node.instruction,
        node.kind,
        node.required_dependency_ids,
        node.required_check_ids,
        node.required_capabilities,
        node.session_policy,
    )
