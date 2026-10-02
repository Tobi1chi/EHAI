"""Structural type of the Orchestrator that its responsibility mixins rely on."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ehai import ID, JsonValue
from ehai.application.checks import CheckContext, CheckRunner
from ehai.application.evaluation import (
    BranchEvaluationContext,
    BranchEvaluator,
    BranchSelection,
)
from ehai.application.execution_policy import RetrySafety
from ehai.application.interventions import (
    WorkerBlocker,
)
from ehai.application.orchestration.common import (
    UnitOfWorkFactory,
    WorkerEventReceipt,
    _ExecutionContext,
    _VerificationScope,
)
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.application.workers import (
    AdoptedResultInputs,
    ArtifactInputSnapshot,
    WorkerAdapter,
    WorkerCancelledError,
    WorkerExecutionError,
    WorkerRequest,
    WorkerResult,
)
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import (
    CheckRun,
    CheckSpec,
    GateDecision,
    HumanCheckRequest,
)
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import (
    PlanNode,
    PlanRevision,
)


class OrchestratorHost(Protocol):
    """Attributes and methods of ``Orchestrator``; mypy checks the class against it."""

    _uow_factory: UnitOfWorkFactory
    _worker: WorkerAdapter | None
    _artifact_store: ArtifactStore
    _check_runner: CheckRunner
    _workspace: Path
    _clock: Callable[[], datetime]
    _id_factory: Callable[[], ID]
    _cancellation_settle_seconds: float
    _branch_evaluator: BranchEvaluator | None
    _attempt_budget: int
    _execution_workspace: Callable[[ID], Path | None] | None
    _adoption_workspace: Callable[[ID], Path] | None
    _adoption_inputs: Callable[[AdoptedResultInputs], bool] | None

    def enable_code_execution(self, workspace_resolver: Callable[[ID], Path | None]) -> None: ...

    def read_artifact(self, artifact_id: ID) -> bytes: ...

    def enable_adoption_execution(
        self,
        workspace_resolver: Callable[[ID], Path],
        input_validator: Callable[[AdoptedResultInputs], bool],
    ) -> None: ...

    def _verification_workspace(self, scope: _VerificationScope) -> Path: ...

    @property
    def worker(self) -> WorkerAdapter: ...

    def execute(self, run_id: ID) -> Run: ...

    def prepare_worker_request(self, run_id: ID) -> WorkerRequest: ...

    def worker_request_for_attempt(self, attempt_id: ID) -> WorkerRequest: ...

    def queue_ready_attempts(self, run_id: ID, *, limit: int) -> tuple[Attempt, ...]: ...

    def start_queued_attempt(self, attempt_id: ID) -> WorkerRequest: ...

    def cancel_queued_attempt(self, attempt_id: ID) -> Run: ...

    def pause_after_runtime_error(self, run_id: ID, reason: str) -> Run: ...

    def accept_worker_result(
        self,
        attempt_id: ID,
        result: WorkerResult,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = ...,
    ) -> Run: ...

    def _accept_intermediate(
        self, run_id: ID, attempt_id: ID, *, adoption_id: ID | None = ...
    ) -> Run: ...

    def advance_ready_adoptions(self, run_id: ID) -> Run | None: ...

    def accept_adopted_result(self, adoption_id: ID) -> Run: ...

    def recover_candidate_results(self, run_id: ID) -> None: ...

    def block_attempt(
        self,
        attempt_id: ID,
        blocker: WorkerBlocker,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = ...,
    ) -> Run: ...

    def record_intervention_reply(
        self, uow: UnitOfWork, intervention_id: ID, request_token: str, *, actor: str, message: str
    ) -> Run: ...

    def waiting_for_intervention(self, run_id: ID, *, only_if_idle: bool = ...) -> bool: ...

    def waiting_for_input(self, run_id: ID, *, only_if_idle: bool = ...) -> bool: ...

    def interrupt_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = ...,
    ) -> Run: ...

    def retry_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        retry_safety: RetrySafety,
        timed_out: bool = ...,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = ...,
    ) -> Run: ...

    def _recover_stalled_nodes(
        self, uow: UnitOfWork, run: Run, plan: PlanRevision
    ) -> PlanRevision: ...

    def time_out_attempt(
        self,
        attempt_id: ID,
        reason: str,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = ...,
    ) -> Run: ...

    def fail_attempt(self, attempt_id: ID, reason: str) -> Run: ...

    def _load_attempt_context(self, attempt_id: ID) -> _ExecutionContext: ...

    def _pause_goal_budget(self, uow: UnitOfWork, run: Run, used: int, limit: int) -> Run: ...

    def _prepare(self, run_id: ID) -> _ExecutionContext | None: ...

    def _evaluate_completed_evaluator(self, run_id: ID) -> bool: ...

    def validate_evaluator_candidate(self, attempt_id: ID, document: str) -> None: ...

    def _record_branch_selection(
        self, context: BranchEvaluationContext, selection: BranchSelection
    ) -> None: ...

    def _record_evaluation_failure(self, run_id: ID, error: Exception) -> None: ...

    def _worker_inputs(
        self, context: _ExecutionContext | AdoptedResultInputs
    ) -> tuple[ArtifactInputSnapshot, ...]: ...

    def _worker_context(
        self, context: _ExecutionContext, artifact_inputs: tuple[ArtifactInputSnapshot, ...]
    ) -> dict[str, JsonValue]: ...

    def _node_worker_context(
        self, context: _ExecutionContext, artifact_inputs: tuple[ArtifactInputSnapshot, ...]
    ) -> dict[str, JsonValue]: ...

    def _rework_context(self, context: _ExecutionContext) -> dict[str, JsonValue]: ...

    def _code_task_context(self, context: _ExecutionContext) -> dict[str, JsonValue]: ...

    def _artifact_input_snapshots(
        self,
        artifacts: tuple[Artifact, ...],
        *,
        adoptions: Mapping[ID, ResultAdoption] | None = ...,
    ) -> tuple[ArtifactInputSnapshot, ...]: ...

    def _validate_reviewer_result(
        self, context: _ExecutionContext, result: WorkerResult
    ) -> None: ...

    def _store_candidate_artifacts(
        self, context: _ExecutionContext, result: WorkerResult
    ) -> tuple[Artifact, ...]: ...

    def _store_worker_error_artifacts(
        self, context: _ExecutionContext, error: WorkerExecutionError
    ) -> tuple[Artifact, ...]: ...

    def _store_artifact(
        self,
        context: _ExecutionContext,
        *,
        kind: ArtifactKind,
        name: str,
        media_type: str,
        content: bytes,
    ) -> Artifact: ...

    def _record_candidate(
        self,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        result: WorkerResult,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = ...,
    ) -> None: ...

    def _apply_worker_event_receipts(
        self, uow: UnitOfWork, attempt: Attempt, receipts: tuple[WorkerEventReceipt, ...]
    ) -> Attempt: ...

    def _check_and_complete(
        self,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        branch_local: bool,
        adoption_id: ID | None = ...,
    ) -> Run: ...

    def waiting_for_human(self, run_id: ID, *, only_if_idle: bool = ...) -> bool: ...

    def record_human_decision(
        self,
        uow: UnitOfWork,
        check_run_id: ID,
        request_token: str,
        *,
        passed: bool,
        actor: str,
        comment: str,
    ) -> tuple[Run, ID]: ...

    @staticmethod
    def _human_request(
        plan: PlanRevision,
        contract: CompletionContract,
        spec: CheckSpec,
        artifacts: tuple[Artifact, ...],
    ) -> HumanCheckRequest: ...

    def _load_verification_scope(
        self, uow: UnitOfWork, attempt_id: ID, adoption_id: ID | None
    ) -> _VerificationScope: ...

    @staticmethod
    def _validate_adopted_scope(
        uow: UnitOfWork,
        run: Run,
        plan: PlanRevision,
        node: PlanNode,
        attempt: Attempt,
        adoption: ResultAdoption,
    ) -> None: ...

    @staticmethod
    def _adopted_artifacts(
        uow: UnitOfWork, attempt: Attempt, adoption: ResultAdoption
    ) -> tuple[Artifact, ...]: ...

    def finish_pending_gate(
        self,
        attempt_id: ID,
        *,
        adoption_id: ID | None = ...,
        recover_interrupted_checks: bool = ...,
    ) -> Run: ...

    def _start_checks(
        self,
        run_id: ID,
        attempt_id: ID,
        artifacts: tuple[Artifact, ...],
        check_specs: tuple[CheckSpec, ...],
        *,
        adoption_id: ID | None = ...,
    ) -> tuple[CheckContext | None, tuple[tuple[CheckSpec, CheckRun], ...]]: ...

    def _required_execution_workspace(self, attempt_id: ID) -> Path: ...

    def _persist_terminal_check(self, uow: UnitOfWork, run: Run, check: CheckRun) -> None: ...

    def _record_completion(
        self,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        artifacts: tuple[Artifact, ...],
        *,
        adoption_id: ID | None = ...,
    ) -> Run: ...

    def _record_gate_failure(
        self,
        run_id: ID,
        check_runs: tuple[CheckRun, ...],
        decision: GateDecision,
        *,
        fail_run: bool,
        repair: bool = ...,
        adoption_id: ID | None = ...,
    ) -> bool: ...

    def _reopen_reviewed_phase(
        self, uow: UnitOfWork, run: Run, plan: PlanRevision, decision: GateDecision
    ) -> None: ...

    @staticmethod
    def _current_verification(
        uow: UnitOfWork, run: Run, node: PlanNode, attempt_id: ID, adoption_id: ID | None = ...
    ) -> bool: ...

    def _record_worker_diagnostics(
        self, attempt_id: ID, artifacts: tuple[Artifact, ...]
    ) -> None: ...

    def _record_worker_failure(
        self,
        attempt_id: ID,
        error: Exception,
        artifacts: tuple[Artifact, ...],
        *,
        diagnostic_error: Exception | None = ...,
        fail_run: bool,
    ) -> None: ...

    def _record_artifacts(
        self, uow: UnitOfWork, run: Run, attempt: Attempt, artifacts: tuple[Artifact, ...]
    ) -> None: ...

    def _await_controlled_cancellation(
        self, attempt_id: ID, error: WorkerCancelledError
    ) -> Run: ...

    def _record_artifact_failure(
        self, attempt_id: ID, result: WorkerResult, error: Exception
    ) -> None: ...

    def _load_run_context(
        self, uow: UnitOfWork, run_id: ID
    ) -> tuple[Run, PlanRevision, Goal, CompletionContract]: ...

    @staticmethod
    def _require_single_node(plan: PlanRevision, contract: CompletionContract) -> None: ...

    def _event(
        self, event_type: EventType, run: Run, correlation_id: ID, payload: dict[str, JsonValue]
    ) -> Event: ...
