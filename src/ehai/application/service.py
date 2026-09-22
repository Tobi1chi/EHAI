"""Application service shared by the CLI, HTTP API and its MCP adapter."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from threading import Lock

from ehai import ID, JsonValue, json_dumps, json_loads, new_id, normalize_id, utc_now
from ehai.application.checkpointing import (
    STARTUP_PAUSE_REASONS,
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
from ehai.application.goal_budgets import validate_goal_worker_budget
from ehai.application.interventions import list_interventions, process_intervention_context
from ehai.application.orchestrator import GateRejectedError, Orchestrator
from ehai.application.pause_causes import PauseCause, require_pause_cause
from ehai.application.planner import (
    MAX_REPLAN_ATTEMPT_SUMMARIES,
    MAX_REPLAN_CHECK_SUMMARIES,
    MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS,
    MAX_REPLAN_INTERVENTION_SUMMARIES,
    AsyncProcessPlanner,
    AsyncProcessReviewer,
    ConversationalPlanner,
    Planner,
    PlanProposal,
    ProcessPlanner,
    ProcessReviewer,
    ReplanAttemptSummary,
    ReplanCheckpointSummary,
    ReplanCheckSummary,
    ReplanContext,
    ReplanInterventionSummary,
    build_plan_proposal,
    discussion_proposal_criteria,
    require_p1_criteria,
)
from ehai.application.planner_capacity import (
    PlannerCapacity,
    async_planning_operation,
    planning_operation,
)
from ehai.application.planning_dialogue import PlanningConversationView, planning_conversation
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.process_control import (
    ProcessControlGuard,
    latest_run_control,
    require_process_control,
)
from ehai.application.process_review import (
    ProcessBoundaryReport,
    approved_source_documents,
    parse_process_boundary_report,
    process_boundary_report_document,
)
from ehai.application.process_review_context import (
    ProcessReviewContext,
    load_process_review_context,
)
from ehai.application.process_reviews import (
    ProcessReviewStatus,
    ProcessReviewView,
    process_review_history,
)
from ehai.application.run_control import RunControllerPort, ensure_dispatch_queued
from ehai.application.sanitization import bounded_redacted_text, redact_sensitive_text
from ehai.application.successions import (
    adopt_successor_results,
    approve_succession,
    approved_succession,
    prepare_succession,
)
from ehai.domain.checking import CheckKind, CheckRun, CheckRunStatus, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, GoalStatus, Project
from ehai.domain.planning import PlanNodeStatus, PlanRevision, PlanRevisionStatus
from ehai.domain.process import ProcessRevision
from ehai.domain.process_drafts import ProcessDraft, ProcessDraftStatus
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus

UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class _PreparedProcessDraft:
    """Durable process-draft request state captured before the model call."""

    draft: ProcessDraft
    goal: Goal
    approved: PlanRevision
    previous: ProcessRevision
    current: PlanRevision
    checks: tuple[CheckSpec, ...]
    intervention_context: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class _PreparedProcessReview:
    """Durable independent-review request state captured before the model call."""

    context: ProcessReviewContext
    review_id: ID
    session_id: ID


@dataclass(frozen=True, slots=True)
class _DiscussionSource:
    """Immutable source facts captured before a source-bound Planner call."""

    run: Run
    pause_event_id: ID
    approved_plan: PlanRevision
    execution_plan: PlanRevision
    process_revision: ProcessRevision
    context: ReplanContext
    interventions: tuple[dict[str, JsonValue], ...]


class ApplicationError(RuntimeError):
    """Base error for P1 application use cases."""


class EntityNotFoundError(ApplicationError):
    """Raised when a Command references a missing persisted entity."""


class IdempotencyConflictError(ApplicationError):
    """Raised before side effects when an idempotency key changes meaning."""


class ExecutionService:
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

    def create_project(self, command: CreateProject) -> Project:
        """Create a Project and its immutable fact Event atomically."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_project(uow, _result_id(existing, "project_id"))

            project = Project.create(
                command.name,
                project_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_project(project)
            uow.events.append(
                self._event(
                    EventType.PROJECT_CREATED,
                    project.project_id,
                    {"project_id": project.project_id},
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"project_id": project.project_id},
            )
            uow.commit()
            return project

    def create_goal(self, command: CreateGoal) -> Goal:
        """Create an open Goal without inventing completion criteria."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_goal(uow, _result_id(existing, "goal_id"))
            _required_project(uow, command.project_id)
            goal = Goal.create(
                command.project_id,
                command.objective,
                goal_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_goal(goal)
            uow.events.append(
                self._event(
                    EventType.GOAL_CREATED,
                    goal.goal_id,
                    {"goal_id": goal.goal_id, "project_id": goal.project_id},
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"goal_id": goal.goal_id},
            )
            uow.commit()
            return goal

    @planning_operation
    def propose_plan(self, command: ProposePlan) -> PlanRevision:
        """Ask the Planner for an unapproved contract and graph proposal."""
        return self._create_plan_proposal(
            command, lambda goal: self._planner.propose(goal, command.criteria)
        )

    def import_plan(self, command: ImportPlan) -> PlanRevision:
        """Import an initial draft for an unplanned Goal without invoking a model."""
        from ehai.application.plan_imports import import_plan_template

        def produce(goal: Goal) -> PlanProposal:
            if goal.status is not GoalStatus.OPEN or goal.completion_contract is not None:
                raise ApplicationError(
                    "Import requires an open, unplanned Goal; "
                    "use revision workflows for existing plans"
                )
            value = json_loads(command.plan_json)
            assert isinstance(value, dict)
            template = import_plan_template(value)
            return build_plan_proposal(
                goal,
                discussion_proposal_criteria((), template),
                template,
                id_factory=self._id_factory,
                clock=self._clock,
            )

        return self._create_plan_proposal(command, produce)

    def _create_plan_proposal(
        self, command: ProposePlan | ImportPlan, produce: Callable[[Goal], PlanProposal]
    ) -> PlanRevision:
        """One idempotent, rechecked persistence path for both proposal sources."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            goal = _required_goal(uow, command.goal_id)
            _require_no_active_runs(uow, goal.goal_id)

        proposal = produce(goal)
        aligned_goal = goal.use_completion_contract(proposal.contract)
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            current_goal = _required_goal(uow, command.goal_id)
            _require_no_active_runs(uow, goal.goal_id)
            if current_goal != goal:
                raise ApplicationError(f"Goal {goal.goal_id} changed while planning")
            uow.states.put_completion_contract(proposal.contract)
            uow.states.put_goal(aligned_goal)
            uow.states.put_plan_revision(proposal.plan_revision)
            for check_spec in proposal.check_specs:
                uow.states.put_check_spec(proposal.plan_revision.plan_revision_id, check_spec)
            uow.events.append(
                self._event(
                    EventType.PLAN_REVISION_PROPOSED,
                    proposal.plan_revision.plan_revision_id,
                    {
                        "completion_contract_id": proposal.contract.completion_contract_id,
                        "goal_id": goal.goal_id,
                        "plan_revision_id": proposal.plan_revision.plan_revision_id,
                        "planner_diagnostics": list(proposal.planner_diagnostics),
                        "planner_event_types": list(proposal.planner_event_types),
                    },
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {
                    "completion_contract_id": proposal.contract.completion_contract_id,
                    "plan_revision_id": proposal.plan_revision.plan_revision_id,
                },
            )
            uow.commit()
            return proposal.plan_revision

    @planning_operation
    def propose_process(self, command: ProposeProcess) -> ProcessDraft:
        """Retain a single generation attempt; never publish the proposed process."""
        prepared = self._prepare_process_draft(command, asynchronous=False)
        if isinstance(prepared, ProcessDraft):
            return prepared
        if not isinstance(self._planner, ProcessPlanner):  # pragma: no cover - checked at prepare
            raise ApplicationError("Configured Planner does not support process drafts")
        try:
            candidate = self._planner.propose_process(
                prepared.goal,
                prepared.approved,
                prepared.previous,
                prepared.current,
                prepared.checks,
                prepared.draft.reason,
                session_ref_id=prepared.draft.planner_session_ref_id,
                intervention_context=prepared.intervention_context,
            )
            return self._complete_process_draft(prepared, candidate)
        except BaseException as error:
            retained = self._fail_process_draft(prepared, error)
            if not isinstance(error, Exception):
                raise
            return retained

    @async_planning_operation
    async def propose_process_async(self, command: ProposeProcess) -> ProcessDraft:
        """Retain one asynchronously cancellable process-draft generation attempt."""
        prepared = self._prepare_process_draft(command, asynchronous=True)
        if isinstance(prepared, ProcessDraft):
            return prepared
        if not isinstance(self._planner, AsyncProcessPlanner):  # pragma: no cover
            raise ApplicationError("Configured Planner does not support async process drafts")
        try:
            candidate = await self._planner.propose_process_async(
                prepared.goal,
                prepared.approved,
                prepared.previous,
                prepared.current,
                prepared.checks,
                prepared.draft.reason,
                session_ref_id=prepared.draft.planner_session_ref_id,
                intervention_context=prepared.intervention_context,
            )
            return self._complete_process_draft(prepared, candidate)
        except BaseException as error:
            retained = self._fail_process_draft(prepared, error)
            if not isinstance(error, Exception):
                raise
            return retained

    def _prepare_process_draft(
        self, command: ProposeProcess, *, asynchronous: bool
    ) -> ProcessDraft | _PreparedProcessDraft:
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                existing = uow.states.get_process_draft(_result_id(receipt, "draft_id"))
                if existing is None:
                    raise ApplicationError("Process draft receipt has no retained draft")
                return existing
            if asynchronous:
                if not _has_async_process_planner(self._planner):
                    raise ApplicationError(
                        "Configured Planner does not support async process drafts"
                    )
            elif not isinstance(self._planner, ProcessPlanner):
                raise ApplicationError("Configured Planner does not support process drafts")
            run = _required_run(uow, command.run_id)
            if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                raise ApplicationError("Process drafting requires a nonterminal started Run")
            goal = _required_goal(uow, run.goal_id)
            approved = _required_plan(uow, run.plan_revision_id)
            current = _required_execution_plan(uow, run.run_id)
            previous = uow.states.get_active_process_revision(run.run_id)
            if previous is None:
                raise ApplicationError("Run has no active process baseline")
            checks = uow.states.list_check_specs(approved.plan_revision_id)
            draft = ProcessDraft(
                draft_id=self._id_factory(),
                run_id=run.run_id,
                parent_process_revision_id=previous.process_revision_id,
                planner_session_ref_id=self._id_factory(),
                base_execution_plan=current,
                reason=command.reason,
                created_at=self._clock(),
            )
            uow.states.put_process_draft(draft)
            self._append_process_draft_event(uow, draft, EventType.PROCESS_DRAFT_STARTED)
            intervention_context = process_intervention_context(uow, draft)
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"draft_id": draft.draft_id},
            )
            uow.commit()
            return _PreparedProcessDraft(
                draft=draft,
                goal=goal,
                approved=approved,
                previous=previous,
                current=current,
                checks=checks,
                intervention_context=intervention_context,
            )

    def _complete_process_draft(
        self, prepared: _PreparedProcessDraft, candidate: ProcessRevision
    ) -> ProcessDraft:
        completed = prepared.draft.finish(candidate, at=self._clock())
        with self._uow_factory() as uow:
            uow.states.put_process_draft(completed)
            self._append_process_draft_event(uow, completed, EventType.PROCESS_DRAFT_COMPLETED)
            uow.commit()
        return completed

    def _fail_process_draft(
        self, prepared: _PreparedProcessDraft, error: BaseException
    ) -> ProcessDraft:
        message = bounded_redacted_text(f"{type(error).__name__}: {error}", max_bytes=16_000)
        assert message is not None
        with self._uow_factory() as uow:
            retained = uow.states.get_process_draft(prepared.draft.draft_id)
            if retained is None:
                raise ApplicationError("Process generation lost its durable request") from error
            if retained.status is ProcessDraftStatus.PLANNING:
                retained = retained.fail(message, at=self._clock())
                uow.states.put_process_draft(retained)
                self._append_process_draft_event(uow, retained, EventType.PROCESS_DRAFT_FAILED)
                uow.commit()
            return retained

    def _append_process_draft_event(
        self, uow: UnitOfWork, draft: ProcessDraft, event_type: EventType
    ) -> None:
        uow.events.append(
            Event(
                type=event_type,
                run_id=draft.run_id,
                correlation_id=draft.draft_id,
                payload={
                    "draft_id": draft.draft_id,
                    "parent_process_revision_id": draft.parent_process_revision_id,
                    "planner_session_ref_id": draft.planner_session_ref_id,
                    "status": draft.status.value,
                    "candidate_process_revision_id": (
                        None if draft.candidate is None else draft.candidate.process_revision_id
                    ),
                    "error": draft.error,
                },
                occurred_at=self._clock(),
            )
        )

    def apply_process(
        self, command: ApplyProcess, *, control_guard: ProcessControlGuard | None = None
    ) -> ProcessRevision:
        """Publish one reviewed candidate atomically, leaving the drained Run paused."""
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                process_id = _result_id(receipt, "process_revision_id")
                process = uow.states.get_process_revision(process_id)
                if process is None:
                    raise ApplicationError("Process application receipt has no retained version")
                return process
            review = _required_process_review(uow, command.review_id)
            if control_guard is not None:
                require_process_control(uow, review.run_id, control_guard)
            if (
                review.status is not ProcessReviewStatus.COMPLETED
                or not review.preserves_boundary
                or review.report is None
            ):
                raise ApplicationError("Only a completed preserving review can support application")
            context = load_process_review_context(uow, review.draft_id)
            if context.run.run_id != review.run_id or (
                review.reviewer_session_ref_id == context.draft.planner_session_ref_id
            ):
                raise ApplicationError("Review identity does not belong to this independent draft")
            if context.run.status is not RunStatus.PAUSED:
                raise ApplicationError("Pause and drain the Run before applying a process change")
            report = parse_process_boundary_report(review.report, context)
            if not report.preserves_boundary or report.obligation_mapping is None:
                raise ApplicationError(
                    "Review no longer establishes the original approval boundary"
                )
            candidate = context.draft.candidate
            assert candidate is not None
            uow.states.publish_process_revision(
                candidate,
                expected_current=context.draft.base_execution_plan,
                obligation_mapping=report.obligation_mapping,
                source_documents=approved_source_documents(context),
            )
            uow.events.append(
                Event(
                    type=EventType.PROCESS_REVISION_APPLIED,
                    run_id=context.run.run_id,
                    correlation_id=candidate.process_revision_id,
                    payload={
                        "process_revision_id": candidate.process_revision_id,
                        "parent_process_revision_id": context.previous.process_revision_id,
                        "draft_id": context.draft.draft_id,
                        "review_id": review.review_id,
                        "reason": candidate.reason,
                    },
                    occurred_at=self._clock(),
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"process_revision_id": candidate.process_revision_id},
            )
            uow.commit()
            return candidate

    @planning_operation
    def review_process(self, command: ReviewProcess) -> ProcessReviewView:
        """Retain the independent model review; its conclusion does not apply the candidate."""
        prepared = self._prepare_process_review(command, asynchronous=False)
        if isinstance(prepared, ProcessReviewView):
            return prepared
        if not isinstance(self._planner, ProcessReviewer):  # pragma: no cover - checked at prepare
            raise ApplicationError("Configured Planner cannot independently review processes")
        try:
            report = self._planner.review_process(
                prepared.context, session_ref_id=prepared.session_id
            )
            return self._complete_process_review(prepared, report)
        except BaseException as error:
            result = self._fail_process_review(prepared, error)
            if not isinstance(error, Exception):
                raise
            return result

    @async_planning_operation
    async def review_process_async(self, command: ReviewProcess) -> ProcessReviewView:
        """Retain one independently reviewable result from a cancellable async model call."""
        prepared = self._prepare_process_review(command, asynchronous=True)
        if isinstance(prepared, ProcessReviewView):
            return prepared
        if not isinstance(self._planner, AsyncProcessReviewer):  # pragma: no cover
            raise ApplicationError("Configured Planner does not support async process reviews")
        try:
            report = await self._planner.review_process_async(
                prepared.context, session_ref_id=prepared.session_id
            )
            return self._complete_process_review(prepared, report)
        except BaseException as error:
            result = self._fail_process_review(prepared, error)
            if not isinstance(error, Exception):
                raise
            return result

    def _prepare_process_review(
        self, command: ReviewProcess, *, asynchronous: bool
    ) -> ProcessReviewView | _PreparedProcessReview:
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                return _required_process_review(uow, _result_id(receipt, "review_id"))
            if asynchronous:
                if not _has_async_process_reviewer(self._planner):
                    raise ApplicationError(
                        "Configured Planner does not support async process reviews"
                    )
            elif not isinstance(self._planner, ProcessReviewer):
                raise ApplicationError("Configured Planner cannot independently review processes")
            context = load_process_review_context(uow, command.draft_id)
            review_id = self._id_factory()
            session_id = self._id_factory()
            self._append_process_review_event(
                uow, review_id, context.draft, session_id, EventType.PROCESS_REVIEW_STARTED, {}
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"review_id": review_id},
            )
            uow.commit()
            return _PreparedProcessReview(context, review_id, session_id)

    def _complete_process_review(
        self, prepared: _PreparedProcessReview, report: ProcessBoundaryReport
    ) -> ProcessReviewView:
        document = process_boundary_report_document(report)
        validated = parse_process_boundary_report(document, prepared.context)
        with self._uow_factory() as uow:
            self._append_process_review_event(
                uow,
                prepared.review_id,
                prepared.context.draft,
                prepared.session_id,
                EventType.PROCESS_REVIEW_COMPLETED,
                {"report": document, "preserves_boundary": validated.preserves_boundary},
            )
            result = _required_process_review(uow, prepared.review_id)
            uow.commit()
            return result

    def _fail_process_review(
        self, prepared: _PreparedProcessReview, error: BaseException
    ) -> ProcessReviewView:
        message = bounded_redacted_text(f"{type(error).__name__}: {error}", max_bytes=16_000)
        assert message is not None
        with self._uow_factory() as uow:
            result = _required_process_review(uow, prepared.review_id)
            if result.status is ProcessReviewStatus.REVIEWING:
                self._append_process_review_event(
                    uow,
                    prepared.review_id,
                    prepared.context.draft,
                    prepared.session_id,
                    EventType.PROCESS_REVIEW_FAILED,
                    {"error": message},
                )
                result = _required_process_review(uow, prepared.review_id)
                uow.commit()
            return result

    def _append_process_review_event(
        self,
        uow: UnitOfWork,
        review_id: ID,
        draft: ProcessDraft,
        session_id: ID,
        event_type: EventType,
        outcome: Mapping[str, JsonValue],
    ) -> None:
        uow.events.append(
            Event(
                type=event_type,
                run_id=draft.run_id,
                correlation_id=review_id,
                payload={
                    "review_id": review_id,
                    "draft_id": draft.draft_id,
                    "reviewer_session_ref_id": session_id,
                    **outcome,
                },
                occurred_at=self._clock(),
            )
        )

    @planning_operation
    def discuss_plan(self, command: DiscussPlan) -> PlanningConversationView:
        requested_criteria = (
            require_p1_criteria(command.criteria, "Planning discussion") if command.criteria else ()
        )
        if not isinstance(self._planner, ConversationalPlanner):
            raise ApplicationError("Configured Planner does not support discussion")
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                conversation_id = _result_id(receipt, "conversation_id")
                view = _required_conversation(uow, conversation_id)
                turn_id = _result_id(receipt, "turn_id")
                turn = next(item for item in view.turns if item.turn_id == turn_id)
                if turn.status != "completed":
                    raise ApplicationError(
                        f"Planning turn {turn_id} is {turn.status}; inspect conversation "
                        f"{conversation_id}. This idempotency key will not repeat a model call."
                    )
                return view
            goal = _required_goal(uow, command.goal_id)
            if goal.status is not GoalStatus.OPEN:
                raise ApplicationError("Only an open Goal can be discussed")
            conversation_id = command.conversation_id or self._id_factory()
            previous = (
                None
                if command.conversation_id is None
                else _required_conversation(uow, conversation_id)
            )
            if previous is not None:
                if (
                    previous.goal_id != goal.goal_id
                    or previous.workspace != self._planning_workspace
                ):
                    raise ApplicationError("Planning conversation Goal or workspace does not match")
                if any(turn.status == "running" for turn in previous.turns):
                    raise ApplicationError("The previous planning turn has an unresolved outcome")
                if len(previous.turns) >= 24:
                    raise ApplicationError("Start a new discussion using the retained current plan")

            source_run_id = command.source_run_id
            if previous is not None and previous.source_run_id is not None:
                if source_run_id is not None and source_run_id != previous.source_run_id:
                    raise ApplicationError(
                        "A planning conversation cannot be rebound to another source Run"
                    )
                source_run_id = previous.source_run_id

            source: _DiscussionSource | None = None
            if source_run_id is None:
                _require_no_active_runs(uow, goal.goal_id)
                plans = uow.states.list_plan_revisions(goal.goal_id)
                base = plans[-1] if plans else None
            else:
                source_run = _required_run(uow, source_run_id)
                approved_source = _validate_source_discussion_run(uow, goal, source_run)
                _require_no_other_active_runs(uow, goal.goal_id, source_run.run_id)
                base = _source_discussion_base(uow, approved_source)
                _validate_source_discussion_base(uow, approved_source, base)
                source = _capture_discussion_source(
                    uow,
                    source_run=source_run,
                    approved_plan=approved_source,
                )
            planner_base = (
                source.execution_plan
                if source is not None and base == source.approved_plan
                else base
            )
            checks = () if planner_base is None else _base_check_specs(uow, planner_base)
            if source is not None and not requested_criteria:
                assert base is not None
                discussion_criteria = require_p1_criteria(
                    _required_contract_for_plan(uow, base).criteria, "Source planning discussion"
                )
            else:
                discussion_criteria = _resolve_discussion_criteria(
                    uow,
                    conversation_id=conversation_id,
                    requested=requested_criteria,
                    base=base,
                )
            history: tuple[dict[str, JsonValue], ...] = (
                ()
                if previous is None
                else tuple(
                    {"user": turn.message, "planner": turn.reply, "error": turn.error}
                    for turn in previous.turns
                )
            )
            if len(json_dumps(list(history)).encode("utf-8")) > 64_000:
                raise ApplicationError("Discussion context is full; start a new conversation")
            turn_id = self._id_factory()
            planner_session_id = self._id_factory()
            started_payload: dict[str, JsonValue] = {
                "turn_id": turn_id,
                "agent_session_ref_id": planner_session_id,
                "goal_id": goal.goal_id,
                "workspace": self._planning_workspace,
                "message": command.message,
                "criteria": list(command.criteria),
                "base_plan_revision_id": None if base is None else base.plan_revision_id,
            }
            if source is not None:
                started_payload.update(
                    {
                        "source_run_id": source.run.run_id,
                        "source_process_revision_id": source.process_revision.process_revision_id,
                        "source_approved_plan_revision_id": source.approved_plan.plan_revision_id,
                    }
                )
            uow.events.append(
                self._event(
                    EventType.PLANNING_TURN_STARTED,
                    conversation_id,
                    started_payload,
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"conversation_id": conversation_id, "turn_id": turn_id},
            )
            uow.commit()
        try:
            if source is not None:
                reply = self._planner.discuss(
                    goal,
                    discussion_criteria,
                    command.message,
                    history,
                    planner_base,
                    checks=checks,
                    source_context=source.context,
                    session_ref_id=planner_session_id,
                )
            elif checks:
                reply = self._planner.discuss(
                    goal,
                    discussion_criteria,
                    command.message,
                    history,
                    base,
                    checks=checks,
                    session_ref_id=planner_session_id,
                )
            else:
                reply = self._planner.discuss(
                    goal,
                    discussion_criteria,
                    command.message,
                    history,
                    base,
                    session_ref_id=planner_session_id,
                )
            if reply.agent_session_ref_id != planner_session_id:
                raise ApplicationError("Planner returned a different discussion Session identity")
            with self._uow_factory() as uow:
                if source is None:
                    if _required_goal(uow, goal.goal_id) != goal:
                        raise ApplicationError(
                            "Goal or approval changed while the Planner was responding"
                        )
                    _require_no_active_runs(uow, goal.goal_id)
                    current_plans = uow.states.list_plan_revisions(goal.goal_id)
                    if (current_plans[-1] if current_plans else None) != base:
                        raise ApplicationError("Plan changed while the Planner was responding")
                else:
                    assert base is not None
                    assert planner_base is not None
                    _recheck_discussion_source(
                        uow,
                        goal=goal,
                        base=base,
                        source=source,
                    )
                    if _base_check_specs(uow, planner_base) != checks:
                        raise ApplicationError("Discussion Check configuration changed")
                proposal = reply.proposal
                if proposal is not None:
                    previous_contract = None
                    if source is not None:
                        assert base is not None
                        previous_contract = _required_contract_for_plan(uow, base)
                    proposal = _discussion_revision(
                        goal,
                        base,
                        proposal,
                        previous_contract=previous_contract,
                    )
                    uow.states.put_completion_contract(proposal.contract)
                    if source is None:
                        uow.states.put_goal(goal.use_completion_contract(proposal.contract))
                    uow.states.put_plan_revision(proposal.plan_revision)
                    for spec in proposal.check_specs:
                        uow.states.put_check_spec(proposal.plan_revision.plan_revision_id, spec)
                    proposal_payload: dict[str, JsonValue] = {
                        "goal_id": goal.goal_id,
                        "plan_revision_id": proposal.plan_revision.plan_revision_id,
                        "completion_contract_id": proposal.contract.completion_contract_id,
                        "conversation_id": conversation_id,
                    }
                    if source is not None:
                        proposal_payload.update(
                            {
                                "source_run_id": source.run.run_id,
                                "source_process_revision_id": (
                                    source.process_revision.process_revision_id
                                ),
                                "source_approved_plan_revision_id": (
                                    source.approved_plan.plan_revision_id
                                ),
                            }
                        )
                    uow.events.append(
                        self._event(
                            EventType.PLAN_REVISION_PROPOSED,
                            proposal.plan_revision.plan_revision_id,
                            proposal_payload,
                        )
                    )
                uow.events.append(
                    self._event(
                        EventType.PLANNING_TURN_COMPLETED,
                        conversation_id,
                        {
                            "turn_id": turn_id,
                            "reply": redact_sensitive_text(reply.text),
                            "agent_session_ref_id": reply.agent_session_ref_id,
                            "plan_revision_id": (
                                None
                                if proposal is None
                                else proposal.plan_revision.plan_revision_id
                            ),
                        },
                    )
                )
                result = _required_conversation(uow, conversation_id)
                uow.commit()
                return result
        except Exception as error:
            with self._uow_factory() as uow:
                uow.events.append(
                    self._event(
                        EventType.PLANNING_TURN_FAILED,
                        conversation_id,
                        {
                            "turn_id": turn_id,
                            "error": bounded_redacted_text(str(error), max_bytes=2000),
                        },
                    )
                )
                uow.commit()
            raise ApplicationError(
                f"Planning turn failed; inspect conversation {conversation_id}: "
                f"{bounded_redacted_text(str(error), max_bytes=2000)}"
            ) from error

    @planning_operation
    def replan_plan(self, command: ReplanPlan) -> PlanRevision:
        """Create a new draft revision without mutating its approved base or history."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            base = _required_plan(uow, command.base_plan_revision_id)
            goal = _required_goal(uow, base.goal_id)
            source_run = (
                None if command.source_run_id is None else _required_run(uow, command.source_run_id)
            )
            if source_run is None or source_run.status is not RunStatus.PAUSED:
                _require_no_active_runs(uow, goal.goal_id)
            else:
                _require_available_replan_version(uow, base)
            context = (
                None
                if source_run is None
                else _build_replan_context(uow, source_run=source_run, base=base)
            )
            planning_base = (
                base if source_run is None else _required_execution_plan(uow, source_run.run_id)
            )
            source_interventions = (
                None if source_run is None else list_interventions(uow.events, source_run.run_id)
            )

        proposal = self._planner.replan(goal, planning_base, command.criteria, context)
        aligned_goal = (
            goal
            if source_run is not None and source_run.status is RunStatus.PAUSED
            else goal.use_completion_contract(proposal.contract)
        )
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            current_goal = _required_goal(uow, goal.goal_id)
            current_base = _required_plan(uow, base.plan_revision_id)
            if source_run is None or source_run.status is not RunStatus.PAUSED:
                _require_no_active_runs(uow, goal.goal_id)
            else:
                _require_available_replan_version(uow, current_base)
            current_context = (
                None
                if source_run is None
                else _build_replan_context(
                    uow,
                    source_run=_required_run(uow, source_run.run_id),
                    base=current_base,
                )
            )
            current_planning_base = (
                current_base
                if source_run is None
                else _required_execution_plan(uow, source_run.run_id)
            )
            if (
                current_goal != goal
                or current_base != base
                or current_context != context
                or current_planning_base != planning_base
                or (
                    source_run is not None
                    and list_interventions(uow.events, source_run.run_id) != source_interventions
                )
            ):
                raise ApplicationError(
                    f"Goal {goal.goal_id}, base PlanRevision {base.plan_revision_id}, or source "
                    "Run evidence changed while replanning"
                )
            uow.states.put_completion_contract(proposal.contract)
            uow.states.put_goal(aligned_goal)
            uow.states.put_plan_revision(proposal.plan_revision)
            for check_spec in proposal.check_specs:
                uow.states.put_check_spec(proposal.plan_revision.plan_revision_id, check_spec)
            uow.events.append(
                self._event(
                    EventType.PLAN_REVISION_PROPOSED,
                    proposal.plan_revision.plan_revision_id,
                    {
                        "completion_contract_id": proposal.contract.completion_contract_id,
                        "goal_id": goal.goal_id,
                        "plan_revision_id": proposal.plan_revision.plan_revision_id,
                        "planner_diagnostics": list(proposal.planner_diagnostics),
                        "planner_event_types": list(proposal.planner_event_types),
                        "replan_context": None if context is None else context.to_dict(),
                        "supersedes_plan_revision_id": base.plan_revision_id,
                    },
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {
                    "completion_contract_id": proposal.contract.completion_contract_id,
                    "plan_revision_id": proposal.plan_revision.plan_revision_id,
                },
            )
            uow.commit()
            return proposal.plan_revision

    def approve_plan(self, command: ApprovePlan) -> PlanRevision:
        """Confirm the exact CompletionContract and approve its PlanRevision."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            plan = _required_plan(uow, command.plan_revision_id)
            contract = _required_contract(uow, command.completion_contract_id)
            goal = _required_goal(uow, plan.goal_id)
            succession = approved_succession(uow, plan.plan_revision_id)
            if command.supersession_json is not None:
                if plan.status is PlanRevisionStatus.APPROVED:
                    raise ApplicationError("An existing approval's succession context is immutable")
                document = json_loads(command.supersession_json)
                if not isinstance(document, dict):
                    raise ValueError("supersession must be an object")
                succession = approve_succession(uow, plan, document)
            if any(
                item.status in {RunStatus.PENDING, RunStatus.RUNNING}
                or any(
                    attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
                    for attempt in uow.states.list_attempts(item.run_id)
                )
                or any(
                    check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                    and check.human_request is None
                    for check in uow.states.list_check_runs(item.run_id)
                )
                for item in uow.states.list_runs(goal.goal_id)
            ):
                raise ApplicationError(
                    "Stop active execution before approving a changed Goal contract"
                )
            for run in uow.states.list_runs(goal.goal_id):
                if run.status is not RunStatus.PAUSED:
                    continue
                if any(
                    item.predecessor_run_id == run.run_id
                    for item in uow.states.list_runs(goal.goal_id)
                ):
                    continue
                if succession is not None and run.run_id == succession["predecessor_run_id"]:
                    continue
                source_plan = _required_plan(uow, run.plan_revision_id)
                if (
                    source_plan.completion_contract_id == contract.completion_contract_id
                    and source_plan.completion_contract_version == contract.version
                ):
                    continue
                if any(
                    check.human_request is not None
                    and check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                    for check in uow.states.list_check_runs(run.run_id)
                ) or any(
                    notice["status"] == "open"
                    for notice in list_interventions(uow.events, run.run_id)
                ):
                    raise ApplicationError(
                        f"Paused Run {run.run_id} has unresolved human Checks or interventions; "
                        "approving a different contract would invalidate their replies. "
                        "Provide explicit supersession dispositions with approval"
                    )
            confirmed = contract.confirm(confirmed_at=self._clock())
            aligned_goal = goal.use_completion_contract(confirmed)
            approved = plan.approve(confirmed, approved_at=self._clock())
            uow.states.put_completion_contract(confirmed)
            uow.states.put_goal(aligned_goal)
            uow.states.put_plan_revision(approved)
            uow.events.append(
                self._event(
                    EventType.COMPLETION_CONTRACT_CONFIRMED,
                    confirmed.completion_contract_id,
                    {
                        "completion_contract_id": confirmed.completion_contract_id,
                        "goal_id": confirmed.goal_id,
                    },
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_REVISION_APPROVED,
                    approved.plan_revision_id,
                    {"plan_revision_id": approved.plan_revision_id, "succession": succession},
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"plan_revision_id": approved.plan_revision_id},
            )
            uow.commit()
            return approved

    def get_start_run_replay(self, command: StartRun) -> Run | None:
        """Read an existing StartRun result without changing durable state."""
        self._require_authorized_start_mode(command)
        with self._uow_factory() as uow:
            return self._start_run_replay(uow, command)

    def start_run(self, command: StartRun) -> Run:
        """Create an idempotent Run and, when authorized, start it atomically."""
        self._require_authorized_start_mode(command)
        with self._uow_factory() as uow:
            replay = self._start_run_replay(uow, command)
            if replay is not None:
                run = replay
            else:
                plan = _required_plan(uow, command.plan_revision_id)
                if plan.status is not PlanRevisionStatus.APPROVED:
                    raise ApplicationError(
                        f"PlanRevision {plan.plan_revision_id} must be approved before StartRun"
                    )
                goal = _required_goal(uow, plan.goal_id)
                contract = goal.completion_contract
                if (
                    contract is None
                    or not contract.is_confirmed
                    or contract.completion_contract_id != plan.completion_contract_id
                    or contract.version != plan.completion_contract_version
                ):
                    raise ApplicationError(
                        f"PlanRevision {plan.plan_revision_id} is not aligned to the current "
                        "confirmed CompletionContract"
                    )
                prior_runs = tuple(
                    run
                    for run in uow.states.list_runs(goal.goal_id)
                    if run.plan_revision_id == plan.plan_revision_id
                )
                if prior_runs:
                    raise ApplicationError(
                        f"PlanRevision {plan.plan_revision_id} already has a Run; "
                        "P1 permits one Run per PlanRevision"
                    )
                config = command.authorized_execution_config
                project_configuration = (
                    None
                    if self._project_configuration_resolver is None
                    else self._project_configuration_resolver(goal.project_id, config)
                )
                budget_document = None if config is None else config.get("goal_worker_budget")
                if budget_document is not None and not isinstance(budget_document, dict):
                    raise ApplicationError("goal_worker_budget must be an object or null")
                validate_goal_worker_budget(uow, goal.goal_id, budget_document)
                source, source_process, succession = prepare_succession(
                    uow, plan, command.predecessor_run_id
                )
                run = Run(
                    goal_id=goal.goal_id,
                    plan_revision_id=plan.plan_revision_id,
                    run_id=self._id_factory(),
                    created_at=self._clock(),
                    predecessor_run_id=command.predecessor_run_id,
                )
                uow.states.put_run(run)
                if project_configuration is not None:
                    uow.events.append(
                        Event(
                            type=EventType.RUN_CONFIGURATION_CAPTURED,
                            run_id=run.run_id,
                            correlation_id=run.run_id,
                            occurred_at=self._clock(),
                            payload={"project_configuration": project_configuration},
                        )
                    )
                if source is not None and source_process is not None:
                    selections = json_loads(command.result_adoptions_json)
                    assert isinstance(selections, list)
                    records = adopt_successor_results(
                        uow,
                        source=source,
                        process=source_process,
                        target=run,
                        selections=selections,
                        artifact_reader=self._orchestrator.read_artifact,
                        id_factory=self._id_factory,
                        clock=self._clock,
                    )
                    uow.events.append(
                        Event(
                            type=EventType.RUN_SUCCESSOR_CREATED,
                            run_id=run.run_id,
                            correlation_id=run.run_id,
                            occurred_at=self._clock(),
                            payload={
                                "predecessor_run_id": source.run_id,
                                "source_process_revision_id": source_process.process_revision_id,
                                "succession": succession,
                                "adoptions": [record.to_dict() for record in records],
                            },
                        )
                    )
                if command.authorized_execution_config_json is not None:
                    run = run.start(at=self._clock())
                    uow.states.put_run(run)
                    config_json = command.authorized_execution_config_json
                    uow.events.append(
                        Event(
                            type=EventType.RUN_STARTED,
                            run_id=run.run_id,
                            correlation_id=run.run_id,
                            payload={
                                "run_id": run.run_id,
                                "execution_config": command.authorized_execution_config,
                                "project_configuration": project_configuration,
                                "execution_config_fingerprint": _config_fingerprint(config_json),
                                "authorization": {"explicit": True},
                            },
                            occurred_at=run.started_at,
                        )
                    )
                if self._background_start:
                    uow.states.put_dispatch_work(
                        DispatchWork(
                            run_id=run.run_id,
                            dispatch_work_id=self._id_factory(),
                            created_at=self._clock(),
                        )
                    )
                self._record_receipt(
                    uow,
                    command.idempotency_key,
                    type(command).__name__,
                    command.fingerprint,
                    {"run_id": run.run_id},
                )
                uow.commit()

        if run.status is RunStatus.PENDING and not self._background_start:
            return self._execute_serially(run.run_id)
        return run

    def decide_human_check(self, command: DecideHumanCheck) -> Run:
        """Record a version-bound human decision without invoking a Worker or model."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                run = _required_run(uow, _result_id(existing, "run_id"))
                attempt_id = _result_id(existing, "attempt_id")
            else:
                run, attempt_id = self._orchestrator.record_human_decision(
                    uow,
                    command.check_run_id,
                    command.request_token,
                    passed=command.passed,
                    actor=command.actor,
                    comment=command.comment,
                )
                self._record_receipt(
                    uow,
                    command.idempotency_key,
                    type(command).__name__,
                    command.fingerprint,
                    {"run_id": run.run_id, "attempt_id": attempt_id},
                )
            check = uow.states.get_check_run(command.check_run_id)
            if check is None or check.run_id != run.run_id or check.attempt_id != attempt_id:
                raise ApplicationError("Human decision receipt does not match its CheckRun")
            adoption_id = check.adoption_id
            if run.status is RunStatus.RUNNING:
                works = tuple(
                    work for work in uow.states.list_dispatch_work() if work.run_id == run.run_id
                )
                if not works:
                    uow.states.put_dispatch_work(DispatchWork(run_id=run.run_id))
                elif works[0].status is DispatchWorkStatus.COMPLETED:
                    uow.states.put_dispatch_work(works[0].requeue())
            uow.commit()
        if run.status is not RunStatus.RUNNING:
            return run
        try:
            return self._orchestrator.finish_pending_gate(attempt_id, adoption_id=adoption_id)
        except GateRejectedError:
            return self.get_run(run.run_id)

    def get_run_interventions(self, run_id: ID) -> tuple[dict[str, JsonValue], ...]:
        with self._uow_factory() as uow:
            normalized = normalize_id(run_id)
            _required_run(uow, normalized)
            return list_interventions(uow.events, normalized)

    @staticmethod
    def _ensure_dispatch_queued(uow: UnitOfWork, run: Run) -> None:
        ensure_dispatch_queued(uow, run)

    def reply_intervention(self, command: ReplyIntervention) -> Run:
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_run(uow, _result_id(existing, "run_id"))
            run = self._orchestrator.record_intervention_reply(
                uow,
                command.intervention_id,
                command.request_token,
                actor=command.actor,
                message=command.message,
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"run_id": run.run_id},
            )
            if run.status is RunStatus.RUNNING:
                self._ensure_dispatch_queued(uow, run)
            uow.commit()
            return run

    def get_run(self, run_id: ID) -> Run:
        """Return one persisted Run for CLI/API queries."""
        with self._uow_factory() as uow:
            return _required_run(uow, normalize_id(run_id))

    def get_run_control_replay(self, command: PauseRun | CancelRun | ResumeRun) -> Run | None:
        """Check idempotency before an async host quiesces external executions."""
        return self._existing_run_for_command(
            command.idempotency_key, type(command).__name__, command.fingerprint
        )

    def pause_run(self, command: PauseRun) -> Run:
        """Pause one Run with its idempotency receipt in the control transaction."""
        controller = self._required_run_controller()
        existing = self._existing_run_for_command(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
        )
        if existing is not None:
            return existing
        receipt = self._make_receipt(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
            {"run_id": command.run_id},
        )
        return controller.pause(
            command.run_id,
            receipt=receipt,
            pause_cause=PauseCause.OPERATOR,
        )

    def pause_run_internal(self, run_id: ID, *, pause_cause: PauseCause) -> Run:
        """Pause from an application-owned host path without a user command receipt."""
        cause = require_pause_cause(pause_cause)
        if cause is PauseCause.OPERATOR:
            raise ValueError("operator pauses must use the public PauseRun command")
        return self._required_run_controller().pause(
            normalize_id(run_id),
            pause_cause=cause,
        )

    def resume_run(
        self, command: ResumeRun, *, control_guard: ProcessControlGuard | None = None
    ) -> Run:
        """Resume a paused Run and synchronously continue its next Attempt."""
        self._required_run_controller()
        existing = self._existing_run_for_command(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
        )
        if existing is not None:
            if control_guard is not None:
                # A replay must not execute recovery or serial work after a later
                # user pause. The original transition already has its receipt.
                return existing
            if self._background_start:
                if existing.status is RunStatus.RUNNING:
                    self._orchestrator.recover_candidate_results(existing.run_id)
                return self.get_run(existing.run_id)
            if existing.status is RunStatus.PAUSED and self._was_paused_by_startup(existing.run_id):
                return self._resume_and_execute_serially(existing.run_id)
            return (
                self._execute_serially(existing.run_id)
                if self._is_ready_to_continue(existing)
                else existing
            )
        receipt = self._make_receipt(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
            {"run_id": command.run_id},
        )
        if self._background_start:
            resumed = self._required_run_controller().resume(
                command.run_id, receipt=receipt, control_guard=control_guard
            )
            self._orchestrator.recover_candidate_results(resumed.run_id)
            return self.get_run(resumed.run_id)
        return self._resume_and_execute_serially(
            command.run_id, receipt=receipt, control_guard=control_guard
        )

    def cancel_run(self, command: CancelRun) -> Run:
        """Cancel one Run without allowing Worker or interface code to complete it."""
        controller = self._required_run_controller()
        existing = self._existing_run_for_command(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
        )
        if existing is not None:
            return existing
        receipt = self._make_receipt(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
            {"run_id": command.run_id},
        )
        return controller.cancel(command.run_id, command.reason, receipt=receipt)

    def recover_startup(self) -> RecoveryReport:
        """Reconcile interrupted work after a process restart."""
        report = self._required_recovery_service().recover_startup()
        with self._uow_factory() as uow:
            resumable = tuple(
                run.run_id
                for project in uow.states.list_projects()
                for goal in uow.states.list_goals(project.project_id)
                for run in uow.states.list_runs(goal.goal_id)
                if run.status is RunStatus.RUNNING
            )
        for run_id in resumable:
            self._orchestrator.recover_candidate_results(run_id)
        return report

    def restore_latest_checkpoint(self, run_id: ID) -> Run:
        """Restore a validated Checkpoint without rerunning external side effects."""
        return self._required_recovery_service().restore_latest(normalize_id(run_id))

    def _existing_run_for_command(
        self,
        idempotency_key: str,
        command_name: str,
        fingerprint: str,
    ) -> Run | None:
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                idempotency_key,
                command_name,
                fingerprint,
            )
            return None if existing is None else _required_run(uow, _result_id(existing, "run_id"))

    def _required_run_controller(self) -> RunControllerPort:
        if self._run_controller is None:
            raise ApplicationError("Run control is not configured")
        return self._run_controller

    def _required_recovery_service(self) -> RecoveryService:
        if self._recovery_service is None:
            raise ApplicationError("Checkpoint recovery is not configured")
        return self._recovery_service

    def _execute_serially(self, run_id: ID) -> Run:
        """Keep external Worker Attempts serial without blocking pause or cancel controls."""
        with self._execution_lock:
            return self._orchestrator.execute(run_id)

    def _resume_and_execute_serially(
        self,
        run_id: ID,
        *,
        receipt: CommandReceipt | None = None,
        control_guard: ProcessControlGuard | None = None,
    ) -> Run:
        """Keep the resumed state hidden until the prior Attempt settles."""
        controller = self._required_run_controller()
        with self._execution_lock:
            resumed = controller.resume(run_id, receipt=receipt, control_guard=control_guard)
            self._orchestrator.recover_candidate_results(resumed.run_id)
            current = self.get_run(resumed.run_id)
            if current.status is not RunStatus.RUNNING:
                return current
            return self._orchestrator.execute(resumed.run_id)

    def _is_ready_to_continue(self, run: Run) -> bool:
        if run.status is not RunStatus.RUNNING:
            return False
        with self._uow_factory() as uow:
            attempts = uow.states.list_attempts(run.run_id)
            if any(attempt.status is AttemptStatus.RUNNING for attempt in attempts):
                return False
            plan = _required_execution_plan(uow, run.run_id)
            return any(
                node.status in {PlanNodeStatus.READY, PlanNodeStatus.STALLED} for node in plan.nodes
            )

    def _was_paused_by_startup(self, run_id: ID) -> bool:
        with self._uow_factory() as uow:
            pause_events = tuple(
                stored.event
                for stored in uow.events.list_events()
                if stored.event.run_id == run_id and stored.event.type is EventType.RUN_PAUSED
            )
        if not pause_events:
            return False
        reason = pause_events[-1].payload.get("reason")
        return isinstance(reason, str) and reason in STARTUP_PAUSE_REASONS

    def _require_authorized_start_mode(self, command: StartRun) -> None:
        if command.predecessor_run_id is not None and (
            not self._background_start or command.authorized_execution_config_json is None
        ):
            raise ApplicationError(
                "Successor execution requires explicit background execution authorization"
            )
        if command.authorized_execution_config_json is not None and not self._background_start:
            raise ApplicationError(
                "an explicitly authorized StartRun requires the P2 background Runtime"
            )

    def _start_run_replay(self, uow: UnitOfWork, command: StartRun) -> Run | None:
        """Resolve a StartRun receipt, including the pre-authorization CLI migration case."""
        receipt = uow.command_receipts.get(command.idempotency_key)
        if receipt is None:
            return None
        command_name = type(command).__name__
        if receipt.command_name != command_name:
            raise IdempotencyConflictError(
                f"idempotency key {command.idempotency_key!r} already belongs to "
                f"{receipt.command_name}"
            )
        run = _required_run(uow, _result_id(receipt.result, "run_id"))
        if run.plan_revision_id != command.plan_revision_id:
            raise IdempotencyConflictError(
                f"idempotency key {command.idempotency_key!r} already targets another PlanRevision"
            )
        if receipt.command_fingerprint == command.fingerprint:
            if command.authorized_execution_config_json is None or _has_start_authorization(
                uow, run, command
            ):
                return run
        elif (
            command.authorized_execution_config_json is not None
            and command.predecessor_run_id is None
        ):
            # Before the atomic path existed, the foreground CLI first wrote the
            # historical StartRun receipt and then recorded explicit authorization
            # in a separate transaction. Accept only that exact durable history.
            legacy = StartRun(command.idempotency_key, command.plan_revision_id)
            if receipt.command_fingerprint == legacy.fingerprint and _has_start_authorization(
                uow, run, command
            ):
                return run
        raise IdempotencyConflictError(
            f"idempotency key {command.idempotency_key!r} already belongs to another StartRun"
        )

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


def _required_process_review(uow: UnitOfWork, review_id: ID) -> ProcessReviewView:
    result = next(
        (
            view
            for view in process_review_history(uow.events.list_events())
            if view.review_id == review_id
        ),
        None,
    )
    if result is None:
        raise EntityNotFoundError(f"Process review {review_id} is not retained")
    return result


def _base_check_specs(uow: UnitOfWork, base: PlanRevision) -> tuple[CheckSpec, ...]:
    """Return only CheckSpecs referenced by this exact base PlanRevision."""
    required_ids = tuple(
        dict.fromkeys(check_id for node in base.nodes for check_id in node.required_check_ids)
    )
    specs_by_id = {
        spec.check_id: spec for spec in uow.states.list_check_specs(base.plan_revision_id)
    }
    missing = tuple(check_id for check_id in required_ids if check_id not in specs_by_id)
    if missing:
        raise ApplicationError(
            f"PlanRevision {base.plan_revision_id} is missing CheckSpecs: {', '.join(missing)}"
        )
    return tuple(specs_by_id[check_id] for check_id in required_ids)


def _has_async_process_planner(value: object) -> bool:
    """Require both the async port and a coroutine function, never a sync fallback."""
    return isinstance(value, AsyncProcessPlanner) and inspect.iscoroutinefunction(
        getattr(value, "propose_process_async", None)
    )


def _has_async_process_reviewer(value: object) -> bool:
    """Require a real coroutine Reviewer entry point."""
    return isinstance(value, AsyncProcessReviewer) and inspect.iscoroutinefunction(
        getattr(value, "review_process_async", None)
    )


def _required_conversation(uow: UnitOfWork, conversation_id: ID) -> PlanningConversationView:
    result = planning_conversation(uow.events.list_events(), conversation_id)
    if result is None:
        raise EntityNotFoundError(f"PlanningConversation {conversation_id} was not found")
    return result


def _resolve_discussion_criteria(
    uow: UnitOfWork,
    *,
    conversation_id: ID,
    requested: tuple[str, ...],
    base: PlanRevision | None,
) -> tuple[str, ...]:
    """Resolve omitted criteria from an approved baseline, then prior discussion input."""
    if requested:
        return requested

    if base is not None and base.status is PlanRevisionStatus.APPROVED:
        contract = uow.states.get_completion_contract(base.completion_contract_id)
        if contract is not None:
            return require_p1_criteria(tuple(contract.criteria), "Planning discussion")

    recent_explicit: tuple[str, ...] | None = None
    prior_approved_base: PlanRevision | None = None
    for stored in reversed(uow.events.list_events()):
        event = stored.event
        if (
            event.correlation_id != conversation_id
            or event.type is not EventType.PLANNING_TURN_STARTED
        ):
            continue
        raw_criteria = event.payload.get("criteria")
        if raw_criteria is not None:
            if not isinstance(raw_criteria, list):
                raise ApplicationError("Planning turn criteria history is invalid")
            criteria: list[str] = []
            for criterion in raw_criteria:
                if not isinstance(criterion, str):
                    raise ApplicationError("Planning turn criteria history is invalid")
                criteria.append(criterion)
            if criteria and recent_explicit is None:
                recent_explicit = require_p1_criteria(tuple(criteria), "Planning discussion")

        raw_base_id = event.payload.get("base_plan_revision_id")
        if isinstance(raw_base_id, str):
            try:
                prior_base = uow.states.get_plan_revision(normalize_id(raw_base_id))
            except ValueError as error:
                raise ApplicationError(
                    "Planning turn base PlanRevision history is invalid"
                ) from error
            if (
                prior_approved_base is None
                and prior_base is not None
                and prior_base.status is PlanRevisionStatus.APPROVED
            ):
                prior_approved_base = prior_base

    if recent_explicit is not None:
        return recent_explicit
    if prior_approved_base is not None:
        contract = uow.states.get_completion_contract(prior_approved_base.completion_contract_id)
        if contract is not None:
            return require_p1_criteria(tuple(contract.criteria), "Planning discussion")
    return ()


def _require_available_replan_version(uow: UnitOfWork, base: PlanRevision) -> None:
    """Do not regenerate an occupied next version while the approved base stays active."""
    successor = next(
        (
            plan
            for plan in uow.states.list_plan_revisions(base.goal_id)
            if plan.version > base.version
        ),
        None,
    )
    if successor is not None:
        raise ApplicationError(
            f"PlanRevision {successor.plan_revision_id} already follows this approved base; "
            "inspect the retained draft with get-plan. Further paused-source draft revision "
            "is not yet supported; the active contract and existing draft were preserved"
        )


def _require_no_active_runs(uow: UnitOfWork, goal_id: ID) -> None:
    if any(
        run.status in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED}
        for run in uow.states.list_runs(goal_id)
    ):
        raise ApplicationError("An active Run must be resolved before revising its Goal")


def _discussion_revision(
    goal: Goal,
    base: PlanRevision | None,
    proposal: PlanProposal,
    *,
    previous_contract: CompletionContract | None = None,
) -> PlanProposal:
    if proposal.plan_revision.design_document is None:
        raise ApplicationError("A discussion proposal must include its reviewable design")
    if base is None:
        return proposal
    current = previous_contract or goal.completion_contract
    if current is None or current.completion_contract_id != base.completion_contract_id:
        raise ApplicationError("Current plan and Goal completion contract do not match")
    contract = current.revise(
        proposal.contract.criteria,
        proposal.contract.required_check_ids,
        completion_contract_id=proposal.contract.completion_contract_id,
        created_at=proposal.contract.created_at,
    )
    plan = PlanRevision.draft(
        goal_id=goal.goal_id,
        completion_contract=contract,
        nodes=proposal.plan_revision.nodes,
        edges=proposal.plan_revision.edges,
        branches=proposal.plan_revision.branches,
        phases=proposal.plan_revision.phases,
        plan_revision_id=proposal.plan_revision.plan_revision_id,
        version=base.version + 1,
        supersedes_plan_revision_id=base.plan_revision_id,
        created_at=proposal.plan_revision.created_at,
        design_document=proposal.plan_revision.design_document,
    )
    return replace(proposal, contract=contract, plan_revision=plan)


def _required_contract_for_plan(uow: UnitOfWork, plan: PlanRevision) -> CompletionContract:
    contract = _required_contract(uow, plan.completion_contract_id)
    if contract.goal_id != plan.goal_id or contract.version != plan.completion_contract_version:
        raise ApplicationError("Plan and CompletionContract identities do not match")
    return contract


def _validate_source_discussion_run(uow: UnitOfWork, goal: Goal, run: Run) -> PlanRevision:
    if run.goal_id != goal.goal_id or run.status is not RunStatus.PAUSED:
        raise ApplicationError("Discussion source must be a paused Run of this Goal")
    approved = _required_plan(uow, run.plan_revision_id)
    contract = _required_contract_for_plan(uow, approved)
    if (
        approved.status is not PlanRevisionStatus.APPROVED
        or not contract.is_confirmed
        or goal.completion_contract != contract
    ):
        raise ApplicationError("Source Run no longer owns the Goal's effective approval")
    return approved


def _require_no_other_active_runs(uow: UnitOfWork, goal_id: ID, source_run_id: ID) -> None:
    for run in uow.states.list_runs(goal_id):
        if run.run_id == source_run_id:
            continue
        human_check_ids = {
            spec.check_id
            for spec in uow.states.list_check_specs(run.plan_revision_id)
            if spec.kind is CheckKind.HUMAN
        }
        if (
            run.status in {RunStatus.PENDING, RunStatus.RUNNING}
            or any(
                attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
                for attempt in uow.states.list_attempts(run.run_id)
            )
            or any(
                check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                and check.check_id not in human_check_ids
                for check in uow.states.list_check_runs(run.run_id)
            )
        ):
            raise ApplicationError("Stop other active Goal execution before source discussion")


def _source_discussion_base(uow: UnitOfWork, approved: PlanRevision) -> PlanRevision:
    plans = uow.states.list_plan_revisions(approved.goal_id)
    return plans[-1] if plans else approved


def _validate_source_discussion_base(
    uow: UnitOfWork, approved: PlanRevision, base: PlanRevision
) -> None:
    current = base
    while current.plan_revision_id != approved.plan_revision_id:
        if (
            current.goal_id != approved.goal_id
            or current.status is not PlanRevisionStatus.DRAFT
            or current.supersedes_plan_revision_id is None
        ):
            raise ApplicationError("Latest draft does not descend from the source approval")
        parent = _required_plan(uow, current.supersedes_plan_revision_id)
        if current.version != parent.version + 1:
            raise ApplicationError("Source discussion draft version chain is not continuous")
        _required_contract_for_plan(uow, current)
        current = parent


def _capture_discussion_source(
    uow: UnitOfWork, *, source_run: Run, approved_plan: PlanRevision
) -> _DiscussionSource:
    control = latest_run_control(uow, source_run.run_id)
    if control is None or control.event.type is not EventType.RUN_PAUSED:
        raise ApplicationError("Source discussion requires a retained pause control event")
    process = uow.states.get_active_process_revision(source_run.run_id)
    if process is None:
        raise ApplicationError("Source Run has no active process revision")
    return _DiscussionSource(
        run=source_run,
        pause_event_id=control.event.id,
        approved_plan=approved_plan,
        execution_plan=_required_execution_plan(uow, source_run.run_id),
        process_revision=process,
        context=_build_replan_context(uow, source_run=source_run, base=approved_plan),
        interventions=list_interventions(uow.events, source_run.run_id),
    )


def _recheck_discussion_source(
    uow: UnitOfWork, *, goal: Goal, base: PlanRevision, source: _DiscussionSource
) -> None:
    current_goal = _required_goal(uow, goal.goal_id)
    if current_goal != goal:
        raise ApplicationError("Goal or approval changed during source discussion")
    run = _required_run(uow, source.run.run_id)
    approved = _validate_source_discussion_run(uow, current_goal, run)
    _require_no_other_active_runs(uow, goal.goal_id, run.run_id)
    current_base = _source_discussion_base(uow, approved)
    if current_base != base:
        raise ApplicationError("Latest draft changed during source discussion")
    current = _capture_discussion_source(uow, source_run=run, approved_plan=approved)
    if current != source:
        raise ApplicationError("Source control, execution or intervention facts changed")


def _config_fingerprint(config_json: str) -> str:
    return sha256(config_json.encode("utf-8")).hexdigest()


def _has_start_authorization(uow: UnitOfWork, run: Run, command: StartRun) -> bool:
    """Require one unambiguous persisted explicit authorization for a replay."""
    config_json = command.authorized_execution_config_json
    if config_json is None:
        return False
    expected_fingerprint = _config_fingerprint(config_json)
    found = False
    for stored in uow.events.list_events():
        event = stored.event
        if event.run_id != run.run_id or event.type is not EventType.RUN_STARTED:
            continue
        payload = event.payload
        authorization = payload.get("authorization")
        if not isinstance(authorization, dict) or authorization.get("explicit") is not True:
            continue
        found = True
        config = payload.get("execution_config")
        if (
            payload.get("run_id") != run.run_id
            or not isinstance(config, dict)
            or json_dumps(config) != config_json
            or payload.get("execution_config_fingerprint") != expected_fingerprint
        ):
            return False
    return found


def _result_id(result: Mapping[str, JsonValue], key: str) -> ID:
    value = result.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"stored Command receipt has no {key}")
    return normalize_id(value)


def _required_project(uow: UnitOfWork, project_id: ID) -> Project:
    project = uow.states.get_project(project_id)
    if project is None:
        raise EntityNotFoundError(f"Project {project_id} does not exist")
    return project


def _required_goal(uow: UnitOfWork, goal_id: ID) -> Goal:
    goal = uow.states.get_goal(goal_id)
    if goal is None:
        raise EntityNotFoundError(f"Goal {goal_id} does not exist")
    return goal


def _required_contract(uow: UnitOfWork, contract_id: ID) -> CompletionContract:
    contract = uow.states.get_completion_contract(contract_id)
    if contract is None:
        raise EntityNotFoundError(f"CompletionContract {contract_id} does not exist")
    return contract


def _required_plan(uow: UnitOfWork, plan_revision_id: ID) -> PlanRevision:
    plan = uow.states.get_plan_revision(plan_revision_id)
    if plan is None:
        raise EntityNotFoundError(f"PlanRevision {plan_revision_id} does not exist")
    return plan


def _required_run(uow: UnitOfWork, run_id: ID) -> Run:
    run = uow.states.get_run(run_id)
    if run is None:
        raise EntityNotFoundError(f"Run {run_id} does not exist")
    return run


def _required_execution_plan(uow: UnitOfWork, run_id: ID) -> PlanRevision:
    plan = uow.states.get_execution_plan(run_id)
    if plan is None:
        raise EntityNotFoundError(f"Run {run_id} has no execution plan")
    return plan


def _build_replan_context(
    uow: UnitOfWork,
    *,
    source_run: Run,
    base: PlanRevision,
) -> ReplanContext:
    if source_run.plan_revision_id != base.plan_revision_id or source_run.goal_id != base.goal_id:
        raise ApplicationError(
            f"Run {source_run.run_id} does not belong to base PlanRevision {base.plan_revision_id}"
        )
    approved_design_document = base.design_document
    approved_checks = uow.states.list_check_specs(base.plan_revision_id)
    base = _required_execution_plan(uow, source_run.run_id)
    if source_run.status not in {RunStatus.PAUSED, RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ApplicationError(
            f"Run {source_run.run_id} must be paused, failed or cancelled to guide replanning"
        )

    all_attempts = uow.states.list_attempts(source_run.run_id)
    if any(item.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING} for item in all_attempts):
        raise ApplicationError("Replanning requires the source Run's execution to be stopped")
    check_specs = {
        item.check_id: item for item in uow.states.list_check_specs(base.plan_revision_id)
    }
    if any(
        item.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
        and (
            item.check_id not in check_specs
            or check_specs[item.check_id].kind is not CheckKind.HUMAN
        )
        for item in uow.states.list_check_runs(source_run.run_id)
    ):
        raise ApplicationError("Stop active automatic Checks before replanning")
    suspended_node_ids = tuple(
        node.plan_node_id for node in base.nodes if node.status is PlanNodeStatus.SUSPENDED
    )
    stalled_node_ids = tuple(
        node.plan_node_id for node in base.nodes if node.status is PlanNodeStatus.STALLED
    )
    notices = list_interventions(uow.events, source_run.run_id)
    attempts_by_id = {attempt.attempt_id: attempt for attempt in all_attempts}
    intervention_summaries: list[ReplanInterventionSummary] = []
    for notice in notices[-MAX_REPLAN_INTERVENTION_SUMMARIES:]:
        source_attempt = attempts_by_id.get(_result_id(notice, "attempt_id"))
        if source_attempt is None or source_attempt.plan_node_id != _result_id(
            notice, "plan_node_id"
        ):
            raise ApplicationError("Replan intervention has no matching source Attempt")
        reply = notice.get("reply")
        reply_text = reply.get("message") if isinstance(reply, dict) else None

        def summary_text(key: str, document: Mapping[str, JsonValue] = notice) -> str | None:
            value = document.get(key)
            return bounded_redacted_text(value if isinstance(value, str) else None, max_bytes=2000)

        intervention_summaries.append(
            ReplanInterventionSummary(
                intervention_id=_result_id(notice, "intervention_id"),
                plan_node_id=_result_id(notice, "plan_node_id"),
                attempt_id=source_attempt.attempt_id,
                source_process_revision_id=source_attempt.process_revision_id,
                kind=str(notice["kind"]),
                status=str(notice.get("status")),
                reason=summary_text("reason"),
                evidence=summary_text("evidence"),
                needed=summary_text("needed"),
                reply=bounded_redacted_text(
                    reply_text if isinstance(reply_text, str) else None, max_bytes=2000
                ),
            )
        )
    attempts = tuple(
        ReplanAttemptSummary(
            attempt_id=attempt.attempt_id,
            plan_node_id=attempt.plan_node_id,
            sequence=attempt.sequence,
            status=attempt.status,
            reason=bounded_redacted_text(attempt.outcome_reason or attempt.queue_reason),
        )
        for attempt in all_attempts[-MAX_REPLAN_ATTEMPT_SUMMARIES:]
    )
    failed_check_runs = tuple(
        check_run
        for check_run in uow.states.list_check_runs(source_run.run_id)
        if _check_failed(check_run)
    )
    failed_checks = tuple(
        ReplanCheckSummary(
            check_run_id=check_run.check_run_id,
            check_id=check_run.check_id,
            plan_node_id=check_run.plan_node_id,
            attempt_id=check_run.attempt_id,
            status=check_run.status,
            passed=None if check_run.result is None else check_run.result.passed,
            reason=bounded_redacted_text(_check_failure_reason(check_run)),
        )
        for check_run in failed_check_runs[-MAX_REPLAN_CHECK_SUMMARIES:]
    )
    failed_node_ids = {
        node.plan_node_id for node in base.nodes if node.status is PlanNodeStatus.FAILED
    }
    failed_node_ids.update(
        attempt.plan_node_id
        for attempt in all_attempts
        if attempt.status
        in {
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.INTERRUPTED,
        }
    )
    failed_node_ids.update(check_run.plan_node_id for check_run in failed_check_runs)
    failed_node_ids.difference_update((*suspended_node_ids, *stalled_node_ids))
    ordered_failed_node_ids = tuple(
        node.plan_node_id for node in base.nodes if node.plan_node_id in failed_node_ids
    )
    checkpoints = uow.states.list_checkpoints(source_run.run_id)
    latest = checkpoints[-1] if checkpoints else None
    checkpoint = (
        None
        if latest is None
        else ReplanCheckpointSummary(
            checkpoint_id=latest.checkpoint_id,
            event_offset=latest.event_offset,
            artifact_ids=latest.artifact_refs[-MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS:],
        )
    )
    return ReplanContext(
        source_run_id=source_run.run_id,
        source_run_status=source_run.status,
        source_run_reason=bounded_redacted_text(source_run.status_reason),
        failed_plan_node_ids=ordered_failed_node_ids,
        attempts=attempts,
        failed_checks=failed_checks,
        consumed_attempt_count=len(all_attempts),
        latest_checkpoint=checkpoint,
        stalled_plan_node_ids=stalled_node_ids,
        suspended_plan_node_ids=suspended_node_ids,
        interventions=tuple(intervention_summaries),
        intervention_count=len(notices),
        approved_checks=approved_checks,
        approved_design_document=approved_design_document,
    )


def _check_failed(check_run: CheckRun) -> bool:
    if check_run.status is CheckRunStatus.COMPLETED:
        return check_run.result is not None and not check_run.result.passed
    return check_run.status in {
        CheckRunStatus.FAILED,
        CheckRunStatus.TIMED_OUT,
        CheckRunStatus.CANCELLED,
        CheckRunStatus.INTERRUPTED,
    }


def _check_failure_reason(check_run: CheckRun) -> str | None:
    if check_run.result is not None and not check_run.result.passed:
        return check_run.result.failure_reason
    return check_run.failure_reason
