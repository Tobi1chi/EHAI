"""Structural type of ExecutionService that its responsibility mixins rely on."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import Lock
from typing import Protocol

from ehai import ID, JsonValue
from ehai.application.checkpointing import (
    RecoveryReport,
    RecoveryService,
)
from ehai.application.commands import (
    ApplyProcess,
    ApprovePlan,
    CancelRun,
    CreateGoal,
    CreateProject,
    DecideHumanCheck,
    DiscussPlan,
    ImportPlan,
    PauseRun,
    ProposePlan,
    ProposeProcess,
    ReplanPlan,
    ReplyIntervention,
    ResumeRun,
    ReviewProcess,
    StartRun,
)
from ehai.application.execution_service.common import (
    UnitOfWorkFactory,
    _PreparedProcessDraft,
    _PreparedProcessReview,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.pause_causes import PauseCause
from ehai.application.planner import (
    Planner,
    PlanProposal,
)
from ehai.application.planner_capacity import (
    PlannerCapacity,
    async_planning_operation,
    planning_operation,
)
from ehai.application.planning_dialogue import PlanningConversationView
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.process_control import (
    ProcessControlGuard,
)
from ehai.application.process_review import (
    ProcessBoundaryReport,
)
from ehai.application.process_reviews import (
    ProcessReviewView,
)
from ehai.application.run_control import RunControllerPort
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Run
from ehai.domain.goal import Goal, Project
from ehai.domain.planning import PlanRevision
from ehai.domain.process import ProcessRevision
from ehai.domain.process_drafts import ProcessDraft


class ExecutionServiceHost(Protocol):
    """Attributes and methods of ``ExecutionService``; mypy checks the class against it."""

    _uow_factory: UnitOfWorkFactory
    _planner: Planner
    _orchestrator: Orchestrator
    _run_controller: RunControllerPort | None
    _recovery_service: RecoveryService | None
    _clock: Callable[[], datetime]
    _id_factory: Callable[[], ID]
    _background_start: bool
    _planning_workspace: str | None
    _planner_capacity: PlannerCapacity
    _project_configuration_resolver: (
        Callable[[ID, dict[str, JsonValue] | None], dict[str, JsonValue]] | None
    )
    _execution_lock: Lock

    @property
    def planner_capacity(self) -> PlannerCapacity: ...

    @property
    def orchestrator(self) -> Orchestrator: ...

    def create_project(self, command: CreateProject) -> Project: ...

    def create_goal(self, command: CreateGoal) -> Goal: ...

    @planning_operation
    def propose_plan(self, command: ProposePlan) -> PlanRevision: ...

    def import_plan(self, command: ImportPlan) -> PlanRevision: ...

    def _create_plan_proposal(
        self, command: ProposePlan | ImportPlan, produce: Callable[[Goal], PlanProposal]
    ) -> PlanRevision: ...

    @planning_operation
    def propose_process(self, command: ProposeProcess) -> ProcessDraft: ...

    @async_planning_operation
    async def propose_process_async(self, command: ProposeProcess) -> ProcessDraft: ...

    def _prepare_process_draft(
        self, command: ProposeProcess, *, asynchronous: bool
    ) -> ProcessDraft | _PreparedProcessDraft: ...

    def _complete_process_draft(
        self, prepared: _PreparedProcessDraft, candidate: ProcessRevision
    ) -> ProcessDraft: ...

    def _fail_process_draft(
        self, prepared: _PreparedProcessDraft, error: BaseException
    ) -> ProcessDraft: ...

    def _append_process_draft_event(
        self, uow: UnitOfWork, draft: ProcessDraft, event_type: EventType
    ) -> None: ...

    def apply_process(
        self, command: ApplyProcess, *, control_guard: ProcessControlGuard | None = ...
    ) -> ProcessRevision: ...

    @planning_operation
    def review_process(self, command: ReviewProcess) -> ProcessReviewView: ...

    @async_planning_operation
    async def review_process_async(self, command: ReviewProcess) -> ProcessReviewView: ...

    def _prepare_process_review(
        self, command: ReviewProcess, *, asynchronous: bool
    ) -> ProcessReviewView | _PreparedProcessReview: ...

    def _complete_process_review(
        self, prepared: _PreparedProcessReview, report: ProcessBoundaryReport
    ) -> ProcessReviewView: ...

    def _fail_process_review(
        self, prepared: _PreparedProcessReview, error: BaseException
    ) -> ProcessReviewView: ...

    def _append_process_review_event(
        self,
        uow: UnitOfWork,
        review_id: ID,
        draft: ProcessDraft,
        session_id: ID,
        event_type: EventType,
        outcome: Mapping[str, JsonValue],
    ) -> None: ...

    @planning_operation
    def discuss_plan(self, command: DiscussPlan) -> PlanningConversationView: ...

    @planning_operation
    def replan_plan(self, command: ReplanPlan) -> PlanRevision: ...

    def approve_plan(self, command: ApprovePlan) -> PlanRevision: ...

    def get_start_run_replay(self, command: StartRun) -> Run | None: ...

    def start_run(self, command: StartRun) -> Run: ...

    def decide_human_check(self, command: DecideHumanCheck) -> Run: ...

    def get_run_interventions(self, run_id: ID) -> tuple[dict[str, JsonValue], ...]: ...

    @staticmethod
    def _ensure_dispatch_queued(uow: UnitOfWork, run: Run) -> None: ...

    def reply_intervention(self, command: ReplyIntervention) -> Run: ...

    def get_run(self, run_id: ID) -> Run: ...

    def get_run_control_replay(self, command: PauseRun | CancelRun | ResumeRun) -> Run | None: ...

    def pause_run(self, command: PauseRun) -> Run: ...

    def pause_run_internal(self, run_id: ID, *, pause_cause: PauseCause) -> Run: ...

    def resume_run(
        self, command: ResumeRun, *, control_guard: ProcessControlGuard | None = ...
    ) -> Run: ...

    def cancel_run(self, command: CancelRun) -> Run: ...

    def recover_startup(self) -> RecoveryReport: ...

    def restore_latest_checkpoint(self, run_id: ID) -> Run: ...

    def _existing_run_for_command(
        self, idempotency_key: str, command_name: str, fingerprint: str
    ) -> Run | None: ...

    def _required_run_controller(self) -> RunControllerPort: ...

    def _required_recovery_service(self) -> RecoveryService: ...

    def _execute_serially(self, run_id: ID) -> Run: ...

    def _resume_and_execute_serially(
        self,
        run_id: ID,
        *,
        receipt: CommandReceipt | None = ...,
        control_guard: ProcessControlGuard | None = ...,
    ) -> Run: ...

    def _is_ready_to_continue(self, run: Run) -> bool: ...

    def _was_paused_by_startup(self, run_id: ID) -> bool: ...

    def _require_authorized_start_mode(self, command: StartRun) -> None: ...

    def _start_run_replay(self, uow: UnitOfWork, command: StartRun) -> Run | None: ...

    @staticmethod
    def _existing_result(
        uow: UnitOfWork, idempotency_key: str, command_name: str, fingerprint: str
    ) -> Mapping[str, JsonValue] | None: ...

    def _record_receipt(
        self,
        uow: UnitOfWork,
        idempotency_key: str,
        command_name: str,
        fingerprint: str,
        result: Mapping[str, JsonValue],
    ) -> None: ...

    def _make_receipt(
        self,
        idempotency_key: str,
        command_name: str,
        fingerprint: str,
        result: Mapping[str, JsonValue],
    ) -> CommandReceipt: ...

    def _event(
        self, event_type: EventType, correlation_id: ID, payload: Mapping[str, JsonValue]
    ) -> Event: ...
