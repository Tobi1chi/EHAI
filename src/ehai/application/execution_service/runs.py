"""Start, control, recover and decide on Runs."""

from __future__ import annotations

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.checkpointing import (
    STARTUP_PAUSE_REASONS,
    RecoveryReport,
    RecoveryService,
)
from ehai.application.commands import (
    CancelRun,
    DecideHumanCheck,
    PauseRun,
    ReplyIntervention,
    ResumeRun,
    StartRun,
)
from ehai.application.execution_service.common import (
    ApplicationError,
    IdempotencyConflictError,
    _config_fingerprint,
    _required_execution_plan,
    _required_goal,
    _required_plan,
    _required_run,
    _result_id,
)
from ehai.application.execution_service.host import ExecutionServiceHost
from ehai.application.goal_budgets import validate_goal_worker_budget
from ehai.application.interventions import list_interventions
from ehai.application.orchestrator import GateRejectedError
from ehai.application.pause_causes import PauseCause, require_pause_cause
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.application.process_control import (
    ProcessControlGuard,
)
from ehai.application.run_control import RunControllerPort, ensure_dispatch_queued
from ehai.application.successions import (
    adopt_successor_results,
    prepare_succession,
)
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanNodeStatus, PlanRevisionStatus
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus


class RunCommandsMixin:
    """Start, control, recover and decide on Runs."""

    def get_start_run_replay(self: ExecutionServiceHost, command: StartRun) -> Run | None:
        """Read an existing StartRun result without changing durable state."""
        self._require_authorized_start_mode(command)
        with self._uow_factory() as uow:
            return self._start_run_replay(uow, command)

    def start_run(self: ExecutionServiceHost, command: StartRun) -> Run:
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

    def decide_human_check(self: ExecutionServiceHost, command: DecideHumanCheck) -> Run:
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

    def get_run_interventions(
        self: ExecutionServiceHost, run_id: ID
    ) -> tuple[dict[str, JsonValue], ...]:
        with self._uow_factory() as uow:
            normalized = normalize_id(run_id)
            _required_run(uow, normalized)
            return list_interventions(uow.events, normalized)

    @staticmethod
    def _ensure_dispatch_queued(uow: UnitOfWork, run: Run) -> None:
        ensure_dispatch_queued(uow, run)

    def reply_intervention(self: ExecutionServiceHost, command: ReplyIntervention) -> Run:
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

    def get_run(self: ExecutionServiceHost, run_id: ID) -> Run:
        """Return one persisted Run for CLI/API queries."""
        with self._uow_factory() as uow:
            return _required_run(uow, normalize_id(run_id))

    def get_run_control_replay(
        self: ExecutionServiceHost, command: PauseRun | CancelRun | ResumeRun
    ) -> Run | None:
        """Check idempotency before an async host quiesces external executions."""
        return self._existing_run_for_command(
            command.idempotency_key, type(command).__name__, command.fingerprint
        )

    def pause_run(self: ExecutionServiceHost, command: PauseRun) -> Run:
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

    def pause_run_internal(
        self: ExecutionServiceHost, run_id: ID, *, pause_cause: PauseCause
    ) -> Run:
        """Pause from an application-owned host path without a user command receipt."""
        cause = require_pause_cause(pause_cause)
        if cause is PauseCause.OPERATOR:
            raise ValueError("operator pauses must use the public PauseRun command")
        return self._required_run_controller().pause(
            normalize_id(run_id),
            pause_cause=cause,
        )

    def resume_run(
        self: ExecutionServiceHost,
        command: ResumeRun,
        *,
        control_guard: ProcessControlGuard | None = None,
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

    def cancel_run(self: ExecutionServiceHost, command: CancelRun) -> Run:
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

    def recover_startup(self: ExecutionServiceHost) -> RecoveryReport:
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

    def restore_latest_checkpoint(self: ExecutionServiceHost, run_id: ID) -> Run:
        """Restore a validated Checkpoint without rerunning external side effects."""
        return self._required_recovery_service().restore_latest(normalize_id(run_id))

    def _existing_run_for_command(
        self: ExecutionServiceHost,
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

    def _required_run_controller(self: ExecutionServiceHost) -> RunControllerPort:
        if self._run_controller is None:
            raise ApplicationError("Run control is not configured")
        return self._run_controller

    def _required_recovery_service(self: ExecutionServiceHost) -> RecoveryService:
        if self._recovery_service is None:
            raise ApplicationError("Checkpoint recovery is not configured")
        return self._recovery_service

    def _execute_serially(self: ExecutionServiceHost, run_id: ID) -> Run:
        """Keep external Worker Attempts serial without blocking pause or cancel controls."""
        with self._execution_lock:
            return self._orchestrator.execute(run_id)

    def _resume_and_execute_serially(
        self: ExecutionServiceHost,
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

    def _is_ready_to_continue(self: ExecutionServiceHost, run: Run) -> bool:
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

    def _was_paused_by_startup(self: ExecutionServiceHost, run_id: ID) -> bool:
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

    def _require_authorized_start_mode(self: ExecutionServiceHost, command: StartRun) -> None:
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

    def _start_run_replay(
        self: ExecutionServiceHost, uow: UnitOfWork, command: StartRun
    ) -> Run | None:
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
