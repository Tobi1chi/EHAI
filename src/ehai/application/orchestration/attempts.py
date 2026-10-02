"""Queue, start, accept, retry, time out, interrupt and fail Attempts."""

from __future__ import annotations

import time

from ehai import ID, JsonValue, normalize_id
from ehai.application.execution_policy import RetrySafety
from ehai.application.goal_budgets import goal_worker_attempts_used, goal_worker_budget
from ehai.application.interventions import (
    WorkerBlocker,
    open_intervention,
)
from ehai.application.orchestration.common import (
    ArtifactPersistenceError,
    AttemptBudgetExceededError,
    OrchestrationError,
    WorkerCancellationUnsettledError,
    WorkerEventReceipt,
    WorkerFailedError,
    _ExecutionContext,
    _replace_node,
    _required_attempt,
    _required_check_specs,
    _required_node,
    _required_plan,
    _required_process_id,
    _required_run,
    _review_rework_has_active_work,
    _review_rework_uses_planner,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.orchestration.readiness import (
    _branch_containing,
    _exhausted_branch_reason,
    _exploration_dependencies_completed,
    ready_nodes,
)
from ehai.application.pause_causes import PauseCause, pause_event_payload
from ehai.application.ports import UnitOfWork
from ehai.application.process_control import run_contract_is_current
from ehai.application.sanitization import bounded_redacted_text
from ehai.application.workers import (
    ArtifactInputSnapshot,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
    WorkerTimedOutError,
)
from ehai.domain.artifacts import Artifact
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
)


class AttemptLifecycleMixin:
    """Queue, start, accept, retry, time out, interrupt and fail Attempts."""

    def execute(self: OrchestratorHost, run_id: ID) -> Run:
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

    def prepare_worker_request(self: OrchestratorHost, run_id: ID) -> WorkerRequest:
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

    def worker_request_for_attempt(self: OrchestratorHost, attempt_id: ID) -> WorkerRequest:
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

    def queue_ready_attempts(
        self: OrchestratorHost, run_id: ID, *, limit: int
    ) -> tuple[Attempt, ...]:
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

    def start_queued_attempt(self: OrchestratorHost, attempt_id: ID) -> WorkerRequest:
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

    def cancel_queued_attempt(self: OrchestratorHost, attempt_id: ID) -> Run:
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

    def pause_after_runtime_error(self: OrchestratorHost, run_id: ID, reason: str) -> Run:
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
        self: OrchestratorHost,
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
        self: OrchestratorHost, run_id: ID, attempt_id: ID, *, adoption_id: ID | None = None
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

    def recover_candidate_results(self: OrchestratorHost, run_id: ID) -> None:
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

    def interrupt_attempt(
        self: OrchestratorHost,
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
        self: OrchestratorHost,
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

    def _recover_stalled_nodes(
        self: OrchestratorHost, uow: UnitOfWork, run: Run, plan: PlanRevision
    ) -> PlanRevision:
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
        self: OrchestratorHost,
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

    def fail_attempt(self: OrchestratorHost, attempt_id: ID, reason: str) -> Run:
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

    def _load_attempt_context(self: OrchestratorHost, attempt_id: ID) -> _ExecutionContext:
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

    def _pause_goal_budget(
        self: OrchestratorHost, uow: UnitOfWork, run: Run, used: int, limit: int
    ) -> Run:
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

    def _prepare(self: OrchestratorHost, run_id: ID) -> _ExecutionContext | None:
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

    def _record_worker_diagnostics(
        self: OrchestratorHost,
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
        self: OrchestratorHost,
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

    def _await_controlled_cancellation(
        self: OrchestratorHost,
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
