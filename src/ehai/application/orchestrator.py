"""P1 single-node orchestration and pure ready-node computation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.workers import WorkerAdapter, WorkerRequest, WorkerResult
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import Checkpoint, CheckResult, CheckRun, Gate, GateDecision
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import (
    PlanNode,
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
        if all(
            node_by_id[dependency_id].status is PlanNodeStatus.COMPLETED
            for dependency_id in node.required_dependency_ids
        ):
            ready.append(node)
    return tuple(ready)


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    run: Run
    plan_revision: PlanRevision
    goal: Goal
    completion_contract: CompletionContract
    plan_node: PlanNode
    attempt: Attempt


class Orchestrator:
    """Execute the deterministic I3 single-node vertical slice."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        worker: WorkerAdapter,
        artifact_store: ArtifactStore,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], ID] = new_id,
    ) -> None:
        self._uow_factory = uow_factory
        self._worker = worker
        self._artifact_store = artifact_store
        self._clock = clock
        self._id_factory = id_factory

    def execute(self, run_id: ID) -> Run:
        """Execute one approved single-node Run through its final Gate and Checkpoint."""
        context = self._prepare(run_id)
        request = WorkerRequest(
            run=context.run,
            attempt=context.attempt,
            plan_node=context.plan_node,
            completion_contract=context.completion_contract,
            context={},
            artifact_inputs=(),
        )
        try:
            result = self._worker.execute(request)
        except Exception as error:
            self._record_worker_failure(context.attempt.attempt_id, error)
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
        return self._check_and_complete(run_id, context.attempt.attempt_id, artifacts)

    def _prepare(self, run_id: ID) -> _ExecutionContext:
        with self._uow_factory() as uow:
            run, plan, goal, contract = self._load_run_context(uow, run_id)
            self._require_single_node(plan, contract)
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

            candidates = ready_nodes(plan)
            if len(candidates) != 1:
                raise OrchestrationError(
                    f"run {run.run_id} expected one ready PlanNode, found {len(candidates)}"
                )
            ready_node = candidates[0].mark_ready()
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
            attempt = Attempt(
                run_id=run.run_id,
                plan_node_id=running_node.plan_node_id,
                sequence=len(uow.states.list_attempts(run.run_id)) + 1,
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
                    },
                )
            )
            uow.commit()
        return _ExecutionContext(run, plan, goal, contract, running_node, attempt)

    def _store_candidate_artifacts(
        self,
        context: _ExecutionContext,
        result: WorkerResult,
    ) -> tuple[Artifact, ...]:
        artifacts: list[Artifact] = []
        for candidate in result.artifacts:
            artifact_id = self._id_factory()
            artifact = Artifact(
                artifact_id=artifact_id,
                kind=candidate.kind,
                name=candidate.name,
                media_type=candidate.media_type,
                size_bytes=len(candidate.content),
                sha256=sha256(candidate.content).hexdigest(),
                relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
                created_at=self._clock(),
                run_id=context.run.run_id,
                plan_node_id=context.plan_node.plan_node_id,
                attempt_id=context.attempt.attempt_id,
            )
            self._artifact_store.put(artifact, candidate.content)
            artifacts.append(artifact)
        return tuple(artifacts)

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
            succeeded = attempt.succeed(
                tuple(artifact.artifact_id for artifact in artifacts), at=self._clock()
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
    ) -> Run:
        check_runs = self._start_checks(run_id, attempt_id)
        evidence_ids = tuple(artifact.artifact_id for artifact in artifacts)
        passed = bool(evidence_ids)
        results = tuple(
            CheckResult(
                check_id=check_run.check_id,
                check_run_id=check_run.check_run_id,
                run_id=check_run.run_id,
                plan_node_id=check_run.plan_node_id,
                attempt_id=check_run.attempt_id,
                passed=passed,
                evaluated_at=self._clock(),
                evidence_artifact_ids=evidence_ids,
                output="candidate artifact present" if passed else "candidate artifact missing",
                failure_reason=None if passed else "Worker returned no candidate Artifact",
            )
            for check_run in check_runs
        )
        gate = Gate(
            required_check_ids=tuple(check_run.check_id for check_run in check_runs),
            gate_id=self._id_factory(),
        )
        decision = gate.evaluate(
            results,
            run_id=run_id,
            plan_node_id=check_runs[0].plan_node_id,
            attempt_id=attempt_id,
            at=self._clock(),
        )
        if not decision.passed:
            self._record_gate_failure(run_id, check_runs, results, decision)
            raise GateRejectedError(
                f"run {run_id} failed Gate {decision.gate_id}: {decision.reason}"
            )
        return self._record_completion(run_id, check_runs, results, decision, artifacts)

    def _start_checks(self, run_id: ID, attempt_id: ID) -> tuple[CheckRun, ...]:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            attempt = _required_attempt(uow, attempt_id)
            node = _required_node(plan, attempt.plan_node_id)
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
        return check_runs

    def _record_completion(
        self,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        results: tuple[CheckResult, ...],
        decision: GateDecision,
        artifacts: tuple[Artifact, ...],
    ) -> Run:
        with self._uow_factory() as uow:
            run, plan, goal, _ = self._load_run_context(uow, run_id)
            node = _required_node(plan, decision.plan_node_id)
            for check_run, result in zip(check_runs, results, strict=True):
                completed_check = check_run.complete(result, at=self._clock())
                uow.states.put_check_run(completed_check)
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
        results: tuple[CheckResult, ...],
        decision: GateDecision,
    ) -> None:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, decision.plan_node_id)
            for check_run, result in zip(check_runs, results, strict=True):
                uow.states.put_check_run(check_run.complete(result, at=self._clock()))
                uow.events.append(
                    self._event(
                        EventType.CHECK_FAILED,
                        run,
                        check_run.check_run_id,
                        {"check_id": check_run.check_id, "reason": result.failure_reason},
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
            failed_run = run.fail(decision.reason or "Gate rejected candidate", at=self._clock())
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
            uow.states.put_run(failed_run)
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_FAILED,
                    run,
                    node.plan_node_id,
                    {"plan_node_id": node.plan_node_id, "reason": decision.reason},
                )
            )
            uow.events.append(
                self._event(
                    EventType.RUN_FAILED,
                    failed_run,
                    run.run_id,
                    {"run_id": run.run_id, "reason": decision.reason},
                )
            )
            uow.commit()

    def _record_worker_failure(self, attempt_id: ID, error: Exception) -> None:
        reason = f"{type(error).__name__}: {error}"
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.plan_revision_id)
            node = _required_node(plan, attempt.plan_node_id)
            failed_attempt = attempt.fail(reason, at=self._clock())
            failed_node = node.fail()
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_attempt(failed_attempt)
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
            uow.states.put_run(failed_run)
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_FAILED,
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
                    EventType.RUN_FAILED,
                    failed_run,
                    run.run_id,
                    {"run_id": run.run_id, "reason": reason},
                )
            )
            uow.commit()

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


def _replace_node(plan: PlanRevision, replacement: PlanNode) -> PlanRevision:
    nodes = tuple(
        replacement if node.plan_node_id == replacement.plan_node_id else node
        for node in plan.nodes
    )
    if nodes == plan.nodes:
        raise OrchestrationError(
            f"PlanNode {replacement.plan_node_id} is not in PlanRevision {plan.plan_revision_id}"
        )
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=nodes,
        edges=plan.edges,
        branches=plan.branches,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
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
