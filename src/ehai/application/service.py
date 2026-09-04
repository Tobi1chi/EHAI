"""Application service shared by the P1 CLI and later HTTP interface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from threading import Lock

from ehai import ID, JsonValue, new_id, normalize_id, utc_now
from ehai.application.checkpointing import (
    STARTUP_PAUSE_REASONS,
    RecoveryReport,
    RecoveryService,
)
from ehai.application.commands import (
    ApprovePlan,
    CancelRun,
    CreateGoal,
    CreateProject,
    PauseRun,
    ProposePlan,
    ReplanPlan,
    ResumeRun,
    StartRun,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import (
    MAX_REPLAN_ATTEMPT_SUMMARIES,
    MAX_REPLAN_CHECK_SUMMARIES,
    MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS,
    Planner,
    ReplanAttemptSummary,
    ReplanCheckpointSummary,
    ReplanCheckSummary,
    ReplanContext,
)
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.run_control import RunControllerPort
from ehai.application.sanitization import bounded_redacted_text
from ehai.domain.checking import CheckRun, CheckRunStatus
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNodeStatus, PlanRevision, PlanRevisionStatus
from ehai.domain.runtime import DispatchWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


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
    ) -> None:
        self._uow_factory = uow_factory
        self._planner = planner
        self._orchestrator = orchestrator
        self._run_controller = run_controller
        self._recovery_service = recovery_service
        self._clock = clock
        self._id_factory = id_factory
        self._background_start = background_start
        self._execution_lock = Lock()

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

    def propose_plan(self, command: ProposePlan) -> PlanRevision:
        """Ask the Planner for an unapproved contract and graph proposal."""
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

        proposal = self._planner.propose(goal, command.criteria)
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
            context = (
                None
                if source_run is None
                else _build_replan_context(uow, source_run=source_run, base=base)
            )

        proposal = self._planner.replan(goal, base, command.criteria, context)
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
            current_goal = _required_goal(uow, goal.goal_id)
            current_base = _required_plan(uow, base.plan_revision_id)
            current_context = (
                None
                if source_run is None
                else _build_replan_context(
                    uow,
                    source_run=_required_run(uow, source_run.run_id),
                    base=current_base,
                )
            )
            if current_goal != goal or current_base != base or current_context != context:
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
                    {"plan_revision_id": approved.plan_revision_id},
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

    def start_run(self, command: StartRun) -> Run:
        """Create an idempotent Run, then execute it outside the Command transaction."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                run = _required_run(uow, _result_id(existing, "run_id"))
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
                run = Run(
                    goal_id=goal.goal_id,
                    plan_revision_id=plan.plan_revision_id,
                    run_id=self._id_factory(),
                    created_at=self._clock(),
                )
                uow.states.put_run(run)
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

    def get_run(self, run_id: ID) -> Run:
        """Return one persisted Run for CLI/API queries."""
        with self._uow_factory() as uow:
            return _required_run(uow, normalize_id(run_id))

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
        return controller.pause(command.run_id, receipt=receipt)

    def resume_run(self, command: ResumeRun) -> Run:
        """Resume a paused Run and synchronously continue its next Attempt."""
        self._required_run_controller()
        existing = self._existing_run_for_command(
            command.idempotency_key,
            type(command).__name__,
            command.fingerprint,
        )
        if existing is not None:
            if self._background_start:
                return existing
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
            return self._required_run_controller().resume(command.run_id, receipt=receipt)
        return self._resume_and_execute_serially(command.run_id, receipt=receipt)

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
        return self._required_recovery_service().recover_startup()

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
    ) -> Run:
        """Keep the resumed state hidden until the prior Attempt settles."""
        controller = self._required_run_controller()
        with self._execution_lock:
            resumed = controller.resume(run_id, receipt=receipt)
            return self._orchestrator.execute(resumed.run_id)

    def _is_ready_to_continue(self, run: Run) -> bool:
        if run.status is not RunStatus.RUNNING:
            return False
        with self._uow_factory() as uow:
            attempts = uow.states.list_attempts(run.run_id)
            if any(attempt.status is AttemptStatus.RUNNING for attempt in attempts):
                return False
            plan = _required_plan(uow, run.plan_revision_id)
            return any(node.status is PlanNodeStatus.READY for node in plan.nodes)

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
    if source_run.status not in {RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ApplicationError(
            f"Run {source_run.run_id} must be failed or cancelled before it can guide replanning"
        )

    all_attempts = uow.states.list_attempts(source_run.run_id)
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
