"""Startup interruption recovery and explicit Checkpoint restoration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from ehai import ID, JsonValue, utc_now
from ehai.application.interventions import list_interventions
from ehai.application.pause_causes import PauseCause, pause_event_payload
from ehai.application.ports import UnitOfWork
from ehai.domain.checking import CheckKind, Checkpoint, CheckRunStatus
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision

UnitOfWorkFactory = Callable[[], UnitOfWork]

STARTUP_ACTIVE_EXECUTION_PAUSE_REASON = "active execution was interrupted"
STARTUP_IDLE_RUN_PAUSE_REASON = "running Run had no active Attempt at startup"
STARTUP_PAUSE_REASONS = frozenset(
    {
        STARTUP_ACTIVE_EXECUTION_PAUSE_REASON,
        STARTUP_IDLE_RUN_PAUSE_REASON,
    }
)


class RecoveryError(RuntimeError):
    """Base class for fail-closed recovery failures."""


class NoCheckpointError(RecoveryError):
    """Raised when a Run has no durable Checkpoint to restore."""


class InvalidRecoveryStateError(RecoveryError):
    """Raised when a Run or Checkpoint is not safe to restore."""


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Immutable summary of state repaired during startup scanning."""

    paused_run_ids: tuple[ID, ...]
    interrupted_attempt_ids: tuple[ID, ...]
    interrupted_check_run_ids: tuple[ID, ...]
    failed_plan_node_ids: tuple[ID, ...]


class RecoveryService:
    """Repair interrupted execution state without invoking external side effects."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def recover_startup(self) -> RecoveryReport:
        """Atomically interrupt active Attempts and pause every running Run."""
        paused_run_ids: list[ID] = []
        interrupted_attempt_ids: list[ID] = []
        interrupted_check_run_ids: list[ID] = []
        failed_plan_node_ids: list[ID] = []
        with self._uow_factory() as uow:
            for project in uow.states.list_projects():
                for goal in uow.states.list_goals(project.project_id):
                    for run in uow.states.list_runs(goal.goal_id):
                        if run.status is not RunStatus.RUNNING:
                            continue
                        plan = _required_plan(uow, run.run_id)
                        failures_before_run = len(failed_plan_node_ids)
                        running_attempts = tuple(
                            attempt
                            for attempt in uow.states.list_attempts(run.run_id)
                            if attempt.status is AttemptStatus.RUNNING
                        )
                        specs = {
                            spec.check_id: spec
                            for spec in uow.states.list_check_specs(plan.plan_revision_id)
                        }
                        checks = uow.states.list_check_runs(run.run_id)
                        durable_verification = {
                            node.plan_node_id
                            for node in plan.nodes
                            if node.status is PlanNodeStatus.VERIFYING
                            and any(
                                attempt.plan_node_id == node.plan_node_id
                                and attempt.status is AttemptStatus.SUCCEEDED
                                and {
                                    check.check_id
                                    for check in checks
                                    if check.attempt_id == attempt.attempt_id
                                }
                                == set(node.required_check_ids)
                                and all(
                                    check.status is CheckRunStatus.COMPLETED
                                    or (
                                        check.check_id in specs
                                        and specs[check.check_id].kind is CheckKind.HUMAN
                                        and check.status is CheckRunStatus.RUNNING
                                    )
                                    for check in checks
                                    if check.attempt_id == attempt.attempt_id
                                )
                                for attempt in uow.states.list_attempts(run.run_id)
                            )
                        }
                        running_check_runs = tuple(
                            check_run
                            for check_run in checks
                            if check_run.status is CheckRunStatus.RUNNING
                            and not (
                                check_run.check_id in specs
                                and specs[check_run.check_id].kind is CheckKind.HUMAN
                            )
                        )
                        updated_nodes = {node.plan_node_id: node for node in plan.nodes}
                        for attempt in running_attempts:
                            interrupted = attempt.interrupt(
                                "execution process stopped before Attempt completed",
                                at=self._clock(),
                            )
                            uow.states.put_attempt(interrupted)
                            interrupted_attempt_ids.append(attempt.attempt_id)
                            uow.events.append(
                                self._event(
                                    EventType.ATTEMPT_INTERRUPTED,
                                    run,
                                    attempt.attempt_id,
                                    {
                                        "attempt_id": attempt.attempt_id,
                                        "plan_node_id": attempt.plan_node_id,
                                        "reason": interrupted.outcome_reason,
                                    },
                                )
                            )
                            node = updated_nodes.get(attempt.plan_node_id)
                            if node is not None and node.status in {
                                PlanNodeStatus.RUNNING,
                                PlanNodeStatus.CANDIDATE,
                                PlanNodeStatus.VERIFYING,
                            }:
                                failed = self._record_node_failure(
                                    uow,
                                    run,
                                    node,
                                    "running Attempt was interrupted",
                                )
                                updated_nodes[node.plan_node_id] = failed
                                failed_plan_node_ids.append(node.plan_node_id)

                        for check_run in running_check_runs:
                            interrupted_check = check_run.interrupt(
                                "execution process stopped before Check completed",
                                at=self._clock(),
                            )
                            uow.states.put_check_run(interrupted_check)
                            interrupted_check_run_ids.append(check_run.check_run_id)
                            uow.events.append(
                                self._event(
                                    EventType.CHECK_INTERRUPTED,
                                    run,
                                    check_run.check_run_id,
                                    {
                                        "check_id": check_run.check_id,
                                        "check_run_id": check_run.check_run_id,
                                        "attempt_id": check_run.attempt_id,
                                        "reason": interrupted_check.failure_reason,
                                    },
                                )
                            )
                            node = updated_nodes.get(check_run.plan_node_id)
                            if node is not None and node.status in {
                                PlanNodeStatus.RUNNING,
                                PlanNodeStatus.CANDIDATE,
                                PlanNodeStatus.VERIFYING,
                            }:
                                failed = self._record_node_failure(
                                    uow,
                                    run,
                                    node,
                                    "running CheckRun was interrupted",
                                )
                                updated_nodes[node.plan_node_id] = failed
                                failed_plan_node_ids.append(node.plan_node_id)

                        active_attempt_node_ids = {
                            attempt.plan_node_id for attempt in running_attempts
                        }
                        running_check_node_ids = {
                            check_run.plan_node_id for check_run in running_check_runs
                        }
                        for node in tuple(updated_nodes.values()):
                            if (
                                node.status in {PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING}
                                and node.plan_node_id not in durable_verification
                                and node.plan_node_id not in active_attempt_node_ids
                                and node.plan_node_id not in running_check_node_ids
                            ):
                                failed = self._record_node_failure(
                                    uow,
                                    run,
                                    node,
                                    "candidate verification was interrupted before startup",
                                )
                                updated_nodes[node.plan_node_id] = failed
                                failed_plan_node_ids.append(node.plan_node_id)

                        if updated_nodes != {node.plan_node_id: node for node in plan.nodes}:
                            plan = _replace_nodes(plan, updated_nodes)
                            uow.states.put_execution_plan(run.run_id, plan)

                        if (
                            (
                                durable_verification
                                or any(node.status is PlanNodeStatus.BLOCKED for node in plan.nodes)
                            )
                            and not running_attempts
                            and not running_check_runs
                            and len(failed_plan_node_ids) == failures_before_run
                        ):
                            continue
                        paused = run.pause()
                        uow.states.put_run(paused)
                        paused_run_ids.append(run.run_id)
                        uow.events.append(
                            self._event(
                                EventType.RUN_PAUSED,
                                paused,
                                run.run_id,
                                pause_event_payload(
                                    run.run_id,
                                    PauseCause.STARTUP_RECOVERY,
                                    reason=(
                                        STARTUP_ACTIVE_EXECUTION_PAUSE_REASON
                                        if running_attempts or running_check_runs
                                        else STARTUP_IDLE_RUN_PAUSE_REASON
                                    ),
                                ),
                            )
                        )
            uow.commit()
        return RecoveryReport(
            paused_run_ids=tuple(paused_run_ids),
            interrupted_attempt_ids=tuple(interrupted_attempt_ids),
            interrupted_check_run_ids=tuple(interrupted_check_run_ids),
            failed_plan_node_ids=tuple(failed_plan_node_ids),
        )

    def _record_node_failure(
        self,
        uow: UnitOfWork,
        run: Run,
        node: PlanNode,
        reason: str,
    ) -> PlanNode:
        failed = node.fail()
        uow.events.append(
            self._event(
                EventType.PLAN_NODE_FAILED,
                run,
                node.plan_node_id,
                {"plan_node_id": node.plan_node_id, "reason": reason},
            )
        )
        return failed

    def latest_checkpoint(self, run_id: ID) -> Checkpoint | None:
        """Return the Checkpoint with the greatest durable Event offset."""
        with self._uow_factory() as uow:
            checkpoints = uow.states.list_checkpoints(run_id)
        return max(checkpoints, key=lambda checkpoint: checkpoint.event_offset, default=None)

    def restore_latest(self, run_id: ID) -> Run:
        """Restore the latest validated snapshot and leave active work paused."""
        with self._uow_factory() as uow:
            current_run = _required_run(uow, run_id)
            if current_run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                raise InvalidRecoveryStateError(
                    f"run {run_id} in {current_run.status.value} state cannot be restored"
                )
            if any(item.get("status") == "open" for item in list_interventions(uow.events, run_id)):
                raise InvalidRecoveryStateError(
                    "Resolve the version-bound intervention before restoring a Checkpoint; "
                    "restoration cannot discard unresolved external effects or user decisions"
                )
            checkpoints = uow.states.list_checkpoints(run_id)
            if not checkpoints:
                raise NoCheckpointError(f"run {run_id} has no Checkpoint")
            checkpoint = max(checkpoints, key=lambda item: item.event_offset)
            self._validate_checkpoint(uow, current_run, checkpoint)

            restored_run = checkpoint.run
            if restored_run.status is RunStatus.RUNNING:
                restored_run = restored_run.pause()
            uow.states.restore_checkpoint_state(checkpoint, restored_run)
            uow.events.append(
                self._event(
                    EventType.CHECKPOINT_RESTORED,
                    restored_run,
                    checkpoint.checkpoint_id,
                    {
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "source_event_offset": checkpoint.event_offset,
                        "restored_run_status": restored_run.status.value,
                    },
                )
            )
            uow.commit()
        return restored_run

    def _validate_checkpoint(
        self,
        uow: UnitOfWork,
        current_run: Run,
        checkpoint: Checkpoint,
    ) -> None:
        if checkpoint.run_id != current_run.run_id:
            raise InvalidRecoveryStateError(
                f"Checkpoint {checkpoint.checkpoint_id} belongs to another Run"
            )
        if checkpoint.plan_revision_id != current_run.plan_revision_id:
            raise InvalidRecoveryStateError(
                f"Checkpoint {checkpoint.checkpoint_id} belongs to another PlanRevision"
            )
        stored_event = next(
            (item for item in uow.events.list_events() if item.offset == checkpoint.event_offset),
            None,
        )
        if stored_event is None:
            raise InvalidRecoveryStateError(
                f"Checkpoint {checkpoint.checkpoint_id} Event offset is missing"
            )
        event = stored_event.event
        if (
            event.type is not EventType.GATE_PASSED
            or event.run_id != current_run.run_id
            or event.payload.get("gate_id") != checkpoint.gate_decision.gate_id
        ):
            raise InvalidRecoveryStateError(
                f"Checkpoint {checkpoint.checkpoint_id} does not reference its GatePassed Event"
            )
        attempt = uow.states.get_attempt(checkpoint.gate_decision.attempt_id)
        if (
            attempt is None
            or attempt.run_id != checkpoint.run_id
            or attempt.plan_node_id != checkpoint.gate_decision.plan_node_id
            or attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise InvalidRecoveryStateError(
                f"Checkpoint {checkpoint.checkpoint_id} Attempt ownership is invalid"
            )
        completed_checks = tuple(
            check_run
            for check_run in uow.states.list_check_runs(checkpoint.run_id)
            if check_run.attempt_id == attempt.attempt_id
            and check_run.plan_node_id == attempt.plan_node_id
            and check_run.status is CheckRunStatus.COMPLETED
            and check_run.result is not None
            and check_run.result.passed
            and check_run.result.evidence_artifact_ids
        )
        decision_evidence = set(checkpoint.gate_decision.evidence_artifact_ids)
        completed_check_ids = {
            check_run.check_id
            for check_run in completed_checks
            if check_run.result is not None
            and set(check_run.result.evidence_artifact_ids).issubset(decision_evidence)
        }
        if not set(checkpoint.gate_decision.required_check_ids).issubset(completed_check_ids):
            raise InvalidRecoveryStateError(
                f"Checkpoint {checkpoint.checkpoint_id} required Check evidence is incomplete"
            )
        for artifact_id in checkpoint.artifact_refs:
            artifact = uow.states.get_artifact(artifact_id)
            if artifact is None:
                raise InvalidRecoveryStateError(
                    f"Checkpoint {checkpoint.checkpoint_id} Artifact {artifact_id} is missing"
                )
            if artifact_id in decision_evidence and (
                artifact.run_id != checkpoint.run_id
                or artifact.plan_node_id != checkpoint.gate_decision.plan_node_id
                or artifact.attempt_id != checkpoint.gate_decision.attempt_id
            ):
                raise InvalidRecoveryStateError(
                    f"Checkpoint {checkpoint.checkpoint_id} Artifact {artifact_id} "
                    "belongs to another execution scope"
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


def _replace_nodes(plan: PlanRevision, replacements: dict[ID, PlanNode]) -> PlanRevision:
    nodes = tuple(replacements.get(node.plan_node_id, node) for node in plan.nodes)
    return PlanRevision.rehydrate(
        plan_revision_id=plan.plan_revision_id,
        goal_id=plan.goal_id,
        version=plan.version,
        completion_contract_id=plan.completion_contract_id,
        completion_contract_version=plan.completion_contract_version,
        nodes=nodes,
        edges=plan.edges,
        branches=plan.branches,
        phases=plan.phases,
        created_at=plan.created_at,
        status=plan.status,
        approved_at=plan.approved_at,
        supersedes_plan_revision_id=plan.supersedes_plan_revision_id,
        design_document=plan.design_document,
    )


def _required_run(uow: UnitOfWork, run_id: ID) -> Run:
    run = uow.states.get_run(run_id)
    if run is None:
        raise RecoveryError(f"Run {run_id} is not persisted")
    return run


def _required_plan(uow: UnitOfWork, run_id: ID) -> PlanRevision:
    plan = uow.states.get_execution_plan(run_id)
    if plan is None:
        raise RecoveryError(f"Run {run_id} execution PlanRevision is not persisted")
    return plan
