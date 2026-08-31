"""P1 single-node orchestration and pure ready-node computation."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.checks import CheckContext, CheckRunner
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.workers import (
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


class WorkerCancellationUnsettledError(OrchestrationError):
    """Raised when Worker cancellation has no matching Run control transition."""


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
    check_specs: tuple[CheckSpec, ...]


class Orchestrator:
    """Execute the deterministic I3 single-node vertical slice."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        worker: WorkerAdapter,
        artifact_store: ArtifactStore,
        check_runner: CheckRunner,
        workspace: str | Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[], ID] = new_id,
        cancellation_settle_seconds: float = 2.0,
    ) -> None:
        if not isinstance(cancellation_settle_seconds, (int, float)) or not (
            0 < cancellation_settle_seconds < float("inf")
        ):
            raise ValueError("cancellation_settle_seconds must be finite and positive")
        self._uow_factory = uow_factory
        self._worker = worker
        self._artifact_store = artifact_store
        self._check_runner = check_runner
        self._workspace = Path.cwd() if workspace is None else Path(workspace)
        self._clock = clock
        self._id_factory = id_factory
        self._cancellation_settle_seconds = float(cancellation_settle_seconds)

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
        except WorkerCancelledError as error:
            try:
                artifacts = self._store_worker_error_artifacts(context, error)
            except Exception as artifact_error:
                settled = self._await_controlled_cancellation(context.attempt.attempt_id, error)
                raise ArtifactPersistenceError(
                    f"attempt {context.attempt.attempt_id} settled as {settled.status.value}, "
                    "but its cancellation diagnostics could not be persisted: "
                    f"{type(artifact_error).__name__}: {artifact_error}"
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
                )
                raise ArtifactPersistenceError(
                    f"attempt {context.attempt.attempt_id} timed out, but its diagnostics "
                    f"could not be persisted: {type(artifact_error).__name__}: {artifact_error}"
                ) from artifact_error
            self._record_worker_failure(context.attempt.attempt_id, error, artifacts)
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
                )
                raise ArtifactPersistenceError(
                    f"attempt {context.attempt.attempt_id} failed, but its diagnostics "
                    f"could not be persisted: {type(artifact_error).__name__}: {artifact_error}"
                ) from artifact_error
            self._record_worker_failure(context.attempt.attempt_id, error, artifacts)
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
        return self._check_and_complete(
            run_id,
            context.attempt.attempt_id,
            artifacts,
            context.check_specs,
        )

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

            already_ready = tuple(
                node for node in plan.nodes if node.status is PlanNodeStatus.READY
            )
            candidates = already_ready or ready_nodes(plan)
            if len(candidates) != 1:
                raise OrchestrationError(
                    f"run {run.run_id} expected one ready PlanNode, found {len(candidates)}"
                )
            ready_node = candidates[0]
            required_specs = _required_check_specs(uow, plan, ready_node)
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
        return _ExecutionContext(
            run,
            plan,
            goal,
            contract,
            running_node,
            attempt,
            required_specs,
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
            self._record_gate_failure(run_id, terminal_checks, decision)
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
        decision: GateDecision,
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
            failed_run = run.fail(reason, at=self._clock())
            self._record_artifacts(uow, run, attempt, artifacts)
            uow.states.put_attempt(failed_attempt)
            uow.states.put_plan_revision(_replace_node(plan, failed_node))
            uow.states.put_run(failed_run)
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
