"""P1 single-node orchestration and pure ready-node computation."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ehai import ID, JsonValue, new_id, normalize_id, utc_now
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
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.workers import (
    MAX_ARTIFACT_INPUT_BYTES,
    MAX_ARTIFACT_INPUT_TOTAL_BYTES,
    ArtifactInputBudgetExceeded,
    ArtifactInputSnapshot,
    WorkerAdapter,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
    WorkerTimedOutError,
)
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import (
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    CheckSpec,
    Gate,
    GateDecision,
)
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)

UnitOfWorkFactory = Callable[[], UnitOfWork]


class OrchestrationError(RuntimeError):
    """Base failure for the bounded P1 Orchestrator."""


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


def _branch_containing(plan: PlanRevision, plan_node_id: ID) -> Branch | None:
    return next(
        (branch for branch in plan.branches if plan_node_id in branch.node_ids),
        None,
    )


def _branch_is_terminal(branch: Branch, node_by_id: dict[ID, PlanNode]) -> bool:
    return _branch_has_failed(branch, node_by_id) or node_by_id[branch.node_ids[-1]].status in {
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.FAILED,
    }


def _branch_has_failed(branch: Branch, node_by_id: dict[ID, PlanNode]) -> bool:
    return any(node_by_id[node_id].status is PlanNodeStatus.FAILED for node_id in branch.node_ids)


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    run: Run
    plan_revision: PlanRevision
    goal: Goal
    completion_contract: CompletionContract
    plan_node: PlanNode
    attempt: Attempt
    check_specs: tuple[CheckSpec, ...]


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
            if outcome.status is not RunStatus.RUNNING:
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
            attempts = uow.states.list_attempts(run.run_id)
            active_nodes = {
                attempt.plan_node_id
                for attempt in attempts
                if attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
            }
            candidates = tuple(
                node
                for node in (
                    tuple(node for node in plan.nodes if node.status is PlanNodeStatus.READY)
                    or ready_nodes(plan)
                )
                if node.plan_node_id not in active_nodes
            )[:limit]
            queued: list[Attempt] = []
            for candidate in candidates:
                ready = (
                    candidate.mark_ready()
                    if candidate.status is PlanNodeStatus.PENDING
                    else candidate
                )
                plan = _replace_node(plan, ready)
                if candidate.status is PlanNodeStatus.PENDING:
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
                uow.states.put_plan_revision(plan)
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
            uow.states.put_plan_revision(plan)
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

    def accept_worker_result(self, attempt_id: ID, result: WorkerResult) -> Run:
        """Persist one Worker candidate and advance existing Check/Gate semantics."""
        context = self._load_attempt_context(attempt_id)
        branch_local = (
            _branch_containing(context.plan_revision, context.plan_node.plan_node_id) is not None
        )
        try:
            artifacts = self._store_candidate_artifacts(context, result)
        except Exception as error:
            self._record_artifact_failure(context.attempt.attempt_id, result, error)
            raise ArtifactPersistenceError(
                f"attempt {context.attempt.attempt_id} produced a candidate but Artifact "
                f"persistence failed: {type(error).__name__}: {error}"
            ) from error
        self._record_candidate(context.attempt.attempt_id, artifacts, result)
        return self._check_and_complete(
            context.run.run_id,
            context.attempt.attempt_id,
            artifacts,
            context.check_specs,
            branch_local=branch_local,
        )

    def interrupt_attempt(self, attempt_id: ID, reason: str) -> Run:
        """Fail closed when Runtime recovery cannot find the original execution."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            interrupted = attempt.interrupt(reason, at=self._clock())
            failed_node = node.fail()
            paused = run.pause()
            uow.states.put_attempt(interrupted)
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
            uow.states.put_run(paused)
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
            uow.events.append(
                self._event(
                    EventType.RUN_PAUSED,
                    paused,
                    run.run_id,
                    {"run_id": run.run_id, "reason": reason},
                )
            )
            uow.commit()
            return paused

    def retry_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        retry_safety: RetrySafety,
        timed_out: bool = False,
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
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            attempts = uow.states.list_attempts(run.run_id)
            if timed_out:
                terminal_attempt = attempt.time_out(reason, at=self._clock())
                terminal_event = EventType.ATTEMPT_TIMED_OUT
            elif safety is RetrySafety.EXECUTION_NOT_FOUND:
                terminal_attempt = attempt.interrupt(reason, at=self._clock())
                terminal_event = EventType.ATTEMPT_INTERRUPTED
            else:
                terminal_attempt = attempt.fail(reason, at=self._clock())
                terminal_event = EventType.ATTEMPT_FAILED
            failed_node = node.fail()
            uow.states.put_attempt(terminal_attempt)
            failed_plan = _replace_node(plan, failed_node)
            uow.states.put_plan_revision(failed_plan)
            uow.events.append(
                self._event(
                    terminal_event,
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
            if len(attempts) >= self._attempt_budget:
                exhausted_reason = (
                    f"safe retry exhausted Attempt budget: consumed={len(attempts)}, "
                    f"limit={self._attempt_budget}; {reason}"
                )
                failed_run = run.fail(exhausted_reason, at=self._clock())
                uow.states.put_run(failed_run)
                uow.events.append(
                    self._event(
                        EventType.RUN_FAILED,
                        failed_run,
                        run.run_id,
                        {"run_id": run.run_id, "reason": exhausted_reason},
                    )
                )
                uow.commit()
                return failed_run
            ready_node = failed_node.retry()
            uow.states.put_plan_revision(_replace_node(failed_plan, ready_node))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_READIED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": "safe retry"},
                )
            )
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

    def time_out_attempt(self, attempt_id: ID, reason: str) -> Run:
        """Time out unknown in-flight work without creating replacement side effects."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            timed_out = attempt.time_out(reason, at=self._clock())
            failed_node = node.fail()
            paused = run.pause()
            uow.states.put_attempt(timed_out)
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
            uow.states.put_run(paused)
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
            uow.events.append(
                self._event(
                    EventType.RUN_PAUSED,
                    paused,
                    run.run_id,
                    {"run_id": run.run_id, "reason": reason},
                )
            )
            uow.commit()
            return paused

    def fail_attempt(self, attempt_id: ID, reason: str) -> Run:
        """Fail an Attempt and its Run when an explicit resource budget is exhausted."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            failed_attempt = attempt.fail(reason, at=self._clock())
            failed_node = node.fail()
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_attempt(failed_attempt)
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
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
                uow.states.put_plan_revision(plan)
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
                plan_node_id=running_node.plan_node_id,
                sequence=len(attempts) + 1,
                attempt_id=self._id_factory(),
                created_at=self._clock(),
            ).start(at=self._clock())
            uow.states.put_plan_revision(plan)
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
            completed_evaluator = next(
                (
                    node
                    for node in plan.nodes
                    if node.kind is PlanNodeKind.EVALUATOR
                    and node.status is PlanNodeStatus.COMPLETED
                ),
                None,
            )
            if completed_evaluator is None:
                return False
            if not any(branch.status is BranchStatus.ACTIVE for branch in plan.branches):
                return False
            branch_node_ids = {node_id for branch in plan.branches for node_id in branch.node_ids}
            attempts = tuple(
                attempt
                for attempt in uow.states.list_attempts(run.run_id)
                if attempt.plan_node_id in branch_node_ids
            )
            artifact_ids = {
                artifact_id for attempt in attempts for artifact_id in attempt.artifact_ids
            }
            artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(run.run_id)
                if artifact.artifact_id in artifact_ids
            )
            evaluator_attempts = tuple(
                attempt
                for attempt in uow.states.list_attempts(run.run_id)
                if attempt.plan_node_id == completed_evaluator.plan_node_id
                and attempt.status is AttemptStatus.SUCCEEDED
            )
            evaluator_artifact_ids = {
                artifact_id
                for attempt in evaluator_attempts
                for artifact_id in attempt.artifact_ids
            }
            evaluator_artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(run.run_id)
                if artifact.artifact_id in evaluator_artifact_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
        context = BranchEvaluationContext(plan, run, attempts, artifacts)
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
            artifact_plan_node_id = None if artifact is None else artifact.plan_node_id
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
                or artifact.plan_node_id not in selected.node_ids
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or attempt is None
                or attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise OrchestrationError(
                    "BranchEvaluator selected Artifacts do not belong to the selected Branch"
                )

        with self._uow_factory() as uow:
            run = _required_run(uow, context.run.run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            if run != context.run or plan != context.plan:
                raise OrchestrationError(
                    f"run {run.run_id} changed while BranchEvaluator was deciding"
                )
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
            uow.states.put_plan_revision(plan)
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

    def _worker_inputs(self, context: _ExecutionContext) -> tuple[ArtifactInputSnapshot, ...]:
        plan = context.plan_revision
        if context.plan_node.kind is PlanNodeKind.EVALUATOR:
            source_node_ids = {
                node_id
                for branch in plan.branches
                if branch.status is BranchStatus.ACTIVE
                for node_id in branch.node_ids
            }
        elif context.plan_node.kind is PlanNodeKind.MERGE:
            selected = _required_selected_branch(plan, context.plan_node.plan_node_id)
            source_node_ids = set(selected.node_ids)
        else:
            source_node_ids = set(context.plan_node.required_dependency_ids)
        if not source_node_ids:
            return ()
        with self._uow_factory() as uow:
            artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(context.run.run_id)
                if artifact.plan_node_id in source_node_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
        return self._artifact_input_snapshots(artifacts)

    def _worker_context(
        self,
        context: _ExecutionContext,
        artifact_inputs: tuple[ArtifactInputSnapshot, ...],
    ) -> dict[str, JsonValue]:
        if context.plan_node.kind is PlanNodeKind.EVALUATOR:
            return {
                "candidate_branches": _candidate_branch_context(
                    context.plan_revision,
                    artifact_inputs,
                )
            }
        if context.plan_node.kind is not PlanNodeKind.MERGE:
            return {}
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
            artifact for artifact in artifact_inputs if artifact.plan_node_id in selected.node_ids
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

    def _artifact_input_snapshots(
        self,
        artifacts: tuple[Artifact, ...],
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
            snapshots.append(ArtifactInputSnapshot.from_artifact(artifact, content))
        return tuple(snapshots)

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
    ) -> None:
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            evidence_artifacts = tuple(
                artifact
                for artifact in artifacts
                if artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
            succeeded = attempt.succeed(
                tuple(artifact.artifact_id for artifact in evidence_artifacts), at=self._clock()
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
            uow.states.put_plan_revision(plan)
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
                    EventType.PLAN_NODE_CANDIDATE_SUBMITTED,
                    run,
                    candidate_node.plan_node_id,
                    {"plan_node_id": candidate_node.plan_node_id},
                )
            )
            uow.commit()

    def _check_and_complete(
        self,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        branch_local: bool,
    ) -> Run:
        context, prepared = self._start_checks(run_id, attempt_id, artifacts, check_specs)
        check_runs: list[CheckRun] = []
        for spec, running_check in prepared:
            try:
                executed = self._check_runner.run(spec, context)
                terminal = _rebind_check_run(running_check, executed)
            except Exception as error:
                terminal = running_check.fail(
                    f"CheckRunner failed: {type(error).__name__}: {error}",
                    at=self._clock(),
                )
            check_runs.append(terminal)

        terminal_checks = tuple(check_runs)
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
        )
        if not decision.passed:
            self._record_gate_failure(
                run_id,
                terminal_checks,
                decision,
                fail_run=not branch_local,
            )
            if branch_local:
                with self._uow_factory() as uow:
                    return _required_run(uow, run_id)
            raise GateRejectedError(
                f"run {run_id} failed Gate {decision.gate_id}: {decision.reason}"
            )
        return self._record_completion(run_id, terminal_checks, decision, artifacts)

    def _start_checks(
        self,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
    ) -> tuple[CheckContext, tuple[tuple[CheckSpec, CheckRun], ...]]:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            attempt = _required_attempt(uow, attempt_id)
            node = _required_node(plan, attempt.plan_node_id)
            if tuple(spec.check_id for spec in check_specs) != node.required_check_ids or any(
                not spec.required for spec in check_specs
            ):
                raise OrchestrationError(
                    f"PlanNode {node.plan_node_id} required CheckSpecs are missing or optional"
                )
            verifying_node = node.begin_verification()
            plan = _replace_node(plan, verifying_node)
            check_runs = tuple(
                CheckRun(
                    run_id=run.run_id,
                    plan_node_id=node.plan_node_id,
                    attempt_id=attempt.attempt_id,
                    check_id=check_id,
                    check_run_id=self._id_factory(),
                    created_at=self._clock(),
                ).start(at=self._clock())
                for check_id in node.required_check_ids
            )
            if not check_runs:
                raise OrchestrationError(f"PlanNode {node.plan_node_id} has no required Checks")
            uow.states.put_plan_revision(plan)
            for check_run in check_runs:
                uow.states.put_check_run(check_run)
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
            workspace=self._workspace,
        )
        return context, tuple(zip(check_specs, check_runs, strict=True))

    def _record_completion(
        self,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        artifacts: tuple[Artifact, ...],
    ) -> Run:
        with self._uow_factory() as uow:
            run, plan, goal, _ = self._load_run_context(uow, run_id)
            node = _required_node(plan, decision.plan_node_id)
            for check_run in check_runs:
                uow.states.put_check_run(check_run)
                uow.events.append(
                    self._event(
                        EventType.CHECK_PASSED,
                        run,
                        check_run.check_run_id,
                        {"check_id": check_run.check_id},
                    )
                )
            gate_event = uow.events.append(
                self._event(
                    EventType.GATE_PASSED,
                    run,
                    decision.gate_id,
                    {"gate_id": decision.gate_id},
                )
            )
            completed_node = node.complete(decision)
            plan = _replace_node(plan, completed_node)
            uow.states.put_plan_revision(plan)
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
    ) -> None:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, decision.plan_node_id)
            for check_run in check_runs:
                uow.states.put_check_run(check_run)
                passed = check_run.result is not None and check_run.result.passed
                reason = (
                    None
                    if passed
                    else (
                        check_run.failure_reason
                        if check_run.result is None
                        else check_run.result.failure_reason
                    )
                )
                uow.events.append(
                    self._event(
                        EventType.CHECK_PASSED if passed else EventType.CHECK_FAILED,
                        run,
                        check_run.check_run_id,
                        {
                            "check_id": check_run.check_id,
                            "check_run_status": check_run.status.value,
                            "reason": reason,
                        },
                    )
                )
            uow.events.append(
                self._event(
                    EventType.GATE_FAILED,
                    run,
                    decision.gate_id,
                    {"gate_id": decision.gate_id, "reason": decision.reason},
                )
            )
            failed_node = node.fail()
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": decision.reason},
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
            plan = _required_plan(uow, run.plan_revision_id)
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
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
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
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            succeeded_attempt = attempt.succeed((), at=self._clock())
            failed_node = node.fail()
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_attempt(succeeded_attempt)
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
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
        plan = _required_plan(uow, run.plan_revision_id)
        goal = uow.states.get_goal(run.goal_id)
        if goal is None:
            raise OrchestrationError(f"Goal {run.goal_id} is not persisted")
        contract = goal.completion_contract
        if contract is None or not contract.is_confirmed:
            raise OrchestrationError(f"Goal {goal.goal_id} has no confirmed CompletionContract")
        if (
            contract.completion_contract_id != plan.completion_contract_id
            or contract.version != plan.completion_contract_version
        ):
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


def _rebind_check_run(persisted: CheckRun, executed: CheckRun) -> CheckRun:
    """Attach a CheckRunner outcome to the already persisted running CheckRun ID."""
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
    if (
        not required_specs
        or len(required_specs) != len(node.required_check_ids)
        or any(not spec.required for spec in required_specs)
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
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
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
) -> list[JsonValue]:
    """Build the Evaluator's explicit branch-to-candidate content map."""
    branches: list[JsonValue] = []
    node_by_id = {node.plan_node_id: node for node in plan.nodes}
    for branch in plan.branches:
        if branch.status is not BranchStatus.ACTIVE:
            continue
        artifacts: list[JsonValue] = [
            artifact.to_prompt_dict()
            for artifact in artifact_inputs
            if artifact.plan_node_id in branch.node_ids
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
    if plan.branches and completed_node.kind is not PlanNodeKind.MERGE:
        return False
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


def _required_plan(uow: UnitOfWork, plan_revision_id: ID) -> PlanRevision:
    plan = uow.states.get_plan_revision(plan_revision_id)
    if plan is None:
        raise OrchestrationError(f"PlanRevision {plan_revision_id} is not persisted")
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
