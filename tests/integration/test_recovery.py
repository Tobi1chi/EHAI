from datetime import UTC, datetime, timedelta

import pytest

from ehai import ID, new_id
from ehai.application.checkpointing import (
    InvalidRecoveryStateError,
    NoCheckpointError,
    RecoveryError,
    RecoveryService,
)
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import (
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    Gate,
    GateDecision,
)
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision, PlanRevisionStatus
from ehai.infrastructure.sqlite import PersistenceConflictError, SQLiteDatabase

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
RECOVERY_TIME = NOW + timedelta(minutes=1)


def _node(node: PlanNode, status: PlanNodeStatus) -> PlanNode:
    return PlanNode.rehydrate(
        plan_node_id=node.plan_node_id,
        title=node.title,
        instruction=node.instruction,
        kind=node.kind,
        required_dependency_ids=node.required_dependency_ids,
        required_check_ids=node.required_check_ids,
        status=status,
    )


def _plan(plan: PlanRevision, nodes: tuple[PlanNode, ...]) -> PlanRevision:
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


def _seed_recoverable(database: SQLiteDatabase) -> tuple[ID, ID, ID, ID, int]:
    project = Project.create("recovery", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "recover", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("evidence exists",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    verified_node = _node(
        PlanNode(new_id(), "verified", "already complete", required_check_ids=(check_id,)),
        PlanNodeStatus.COMPLETED,
    )
    later_node = PlanNode(new_id(), "later", "work after checkpoint")
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(verified_node, later_node),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(
        goal_id=goal.goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    artifact_id = new_id()
    verified_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=verified_node.plan_node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    verified_attempt = verified_attempt.succeed((artifact_id,), at=NOW + timedelta(seconds=1))
    artifact = Artifact(
        artifact_id=artifact_id,
        kind=ArtifactKind.EVIDENCE,
        name="evidence.txt",
        media_type="text/plain",
        size_bytes=2,
        sha256="a" * 64,
        relative_path="artifacts/evidence.txt",
        created_at=NOW + timedelta(seconds=1),
        run_id=run.run_id,
        plan_node_id=verified_node.plan_node_id,
        attempt_id=verified_attempt.attempt_id,
    )
    check_run = CheckRun(
        run_id=run.run_id,
        plan_node_id=verified_node.plan_node_id,
        attempt_id=verified_attempt.attempt_id,
        check_id=check_id,
        created_at=NOW,
    ).start(at=NOW)
    result = CheckResult(
        check_id=check_id,
        check_run_id=check_run.check_run_id,
        run_id=run.run_id,
        plan_node_id=verified_node.plan_node_id,
        attempt_id=verified_attempt.attempt_id,
        passed=True,
        evaluated_at=NOW + timedelta(seconds=1),
        evidence_artifact_ids=(artifact_id,),
        output="ok",
    )
    check_run = check_run.complete(result, at=NOW + timedelta(seconds=2))
    decision = Gate(required_check_ids=(check_id,)).evaluate(
        (result,),
        run_id=run.run_id,
        plan_node_id=verified_node.plan_node_id,
        attempt_id=verified_attempt.attempt_id,
        at=NOW + timedelta(seconds=2),
    )
    gate_event = Event(
        type=EventType.GATE_PASSED,
        correlation_id=decision.gate_id,
        run_id=run.run_id,
        payload={"gate_id": decision.gate_id},
        occurred_at=NOW + timedelta(seconds=2),
    )
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
        uow.states.put_attempt(verified_attempt)
        uow.states.put_artifact(artifact)
        uow.states.put_check_run(check_run)
        gate_offset = uow.events.append(gate_event).offset
        checkpoint = Checkpoint(
            plan_revision=plan,
            run=run,
            event_offset=gate_offset,
            gate_decision=decision,
            artifact_refs=(artifact_id,),
            checkpoint_id=new_id(),
            created_at=NOW + timedelta(seconds=3),
        )
        uow.states.put_checkpoint(checkpoint)
        uow.events.append(
            Event(
                type=EventType.CHECKPOINT_CREATED,
                correlation_id=checkpoint.checkpoint_id,
                run_id=run.run_id,
                payload={"checkpoint_id": checkpoint.checkpoint_id},
                occurred_at=NOW + timedelta(seconds=3),
            )
        )
        uow.commit()

    ready_plan = _plan(plan, (verified_node, later_node.mark_ready()))
    with database.unit_of_work() as uow:
        uow.states.put_plan_revision(ready_plan)
        uow.commit()
    running_node = ready_plan.nodes[1].start()
    running_plan = _plan(ready_plan, (verified_node, running_node))
    running_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=running_node.plan_node_id,
        sequence=2,
        attempt_id=new_id(),
        created_at=NOW + timedelta(seconds=4),
    ).start(at=NOW + timedelta(seconds=4))
    with database.unit_of_work() as uow:
        uow.states.put_plan_revision(running_plan)
        uow.states.put_attempt(running_attempt)
        uow.events.append(
            Event(
                type=EventType.ATTEMPT_STARTED,
                correlation_id=running_attempt.attempt_id,
                run_id=run.run_id,
                payload={"attempt_id": running_attempt.attempt_id},
                occurred_at=NOW + timedelta(seconds=4),
            )
        )
        uow.commit()
    return (
        run.run_id,
        running_attempt.attempt_id,
        running_node.plan_node_id,
        artifact_id,
        gate_offset,
    )


def test_restart_interrupts_then_restores_without_repeating_side_effects(tmp_path) -> None:
    path = tmp_path / "recovery.sqlite3"
    run_id, running_attempt_id, running_node_id, artifact_id, gate_offset = _seed_recoverable(
        SQLiteDatabase(path)
    )

    restarted = SQLiteDatabase(path)
    service = RecoveryService(uow_factory=restarted.unit_of_work, clock=lambda: RECOVERY_TIME)
    report = service.recover_startup()

    assert report.paused_run_ids == (run_id,)
    assert report.interrupted_attempt_ids == (running_attempt_id,)
    assert report.failed_plan_node_ids == (running_node_id,)
    checkpoint = service.latest_checkpoint(run_id)
    assert checkpoint is not None and checkpoint.event_offset == gate_offset
    with restarted.unit_of_work() as uow:
        paused = uow.states.get_run(run_id)
        failed_plan = uow.states.get_plan_revision(checkpoint.plan_revision_id)
        attempts_before = uow.states.list_attempts(run_id)
        artifacts_before = uow.states.list_artifacts_for_run(run_id)
        events_before = uow.events.list_events()
    assert paused is not None and paused.status is RunStatus.PAUSED
    assert failed_plan is not None
    assert failed_plan.nodes[1].status is PlanNodeStatus.FAILED
    assert tuple(attempt.status for attempt in attempts_before) == (
        AttemptStatus.SUCCEEDED,
        AttemptStatus.INTERRUPTED,
    )
    assert tuple(artifact.artifact_id for artifact in artifacts_before) == (artifact_id,)
    assert tuple(item.offset for item in events_before) == tuple(range(1, len(events_before) + 1))

    with (
        restarted.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="illegal persisted status transition"),
    ):
        uow.states.put_plan_revision(checkpoint.plan_revision)

    restored = service.restore_latest(run_id)
    restored_again = service.restore_latest(run_id)

    assert restored.status is RunStatus.PAUSED
    assert restored_again == restored
    with restarted.unit_of_work() as uow:
        restored_plan = uow.states.get_plan_revision(checkpoint.plan_revision_id)
        attempts_after = uow.states.list_attempts(run_id)
        artifacts_after = uow.states.list_artifacts_for_run(run_id)
        events_after = uow.events.list_events()
    assert attempts_after == attempts_before
    assert artifacts_after == artifacts_before
    assert restored_plan is not None
    assert restored_plan == checkpoint.plan_revision
    assert restored_plan.nodes[1].status is PlanNodeStatus.PENDING
    assert tuple(item.offset for item in events_after) == tuple(range(1, len(events_after) + 1))
    assert events_after[-2].event.type is EventType.CHECKPOINT_RESTORED
    assert events_after[-1].event.type is EventType.CHECKPOINT_RESTORED


def test_explicit_restore_port_rejects_superseded_checkpoint(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "latest-only.sqlite3")
    run_id, _, _, _, _ = _seed_recoverable(database)
    recovery = RecoveryService(uow_factory=database.unit_of_work, clock=lambda: RECOVERY_TIME)
    recovery.recover_startup()
    recovery.restore_latest(run_id)

    with database.unit_of_work() as uow:
        old_checkpoint = uow.states.list_checkpoints(run_id)[0]
        current_run = uow.states.get_run(run_id)
        current_plan = uow.states.get_plan_revision(old_checkpoint.plan_revision_id)
    assert current_run is not None
    assert current_plan is not None
    ready_plan = _plan(
        current_plan,
        (current_plan.nodes[0], current_plan.nodes[1].mark_ready()),
    )
    with database.unit_of_work() as uow:
        uow.states.put_plan_revision(ready_plan)
        gate_event = uow.events.append(
            Event(
                type=EventType.GATE_PASSED,
                correlation_id=old_checkpoint.gate_decision.gate_id,
                run_id=run_id,
                payload={"gate_id": old_checkpoint.gate_decision.gate_id},
                occurred_at=RECOVERY_TIME + timedelta(seconds=1),
            )
        )
        newer_checkpoint = Checkpoint(
            plan_revision=ready_plan,
            run=current_run,
            event_offset=gate_event.offset,
            gate_decision=old_checkpoint.gate_decision,
            artifact_refs=old_checkpoint.artifact_refs,
            created_at=RECOVERY_TIME + timedelta(seconds=2),
        )
        uow.states.put_checkpoint(newer_checkpoint)
        uow.commit()

    with (
        database.unit_of_work() as uow,
        pytest.raises(PersistenceConflictError, match="superseded"),
    ):
        uow.states.restore_checkpoint_state(old_checkpoint, old_checkpoint.run.pause())


def _seed_post_worker_state(
    database: SQLiteDatabase,
    *,
    node_status: PlanNodeStatus,
    with_running_check: bool,
) -> tuple[ID, ID, ID, ID | None]:
    project = Project.create("post-worker", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "post-worker", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("checked",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = _node(
        PlanNode(new_id(), "candidate", "verify", required_check_ids=(check_id,)),
        node_status,
    )
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(node,),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(goal.goal_id, plan.plan_revision_id, run_id=new_id(), created_at=NOW).start(at=NOW)
    attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    attempt = attempt.succeed((), at=NOW + timedelta(seconds=1))
    check_run = (
        CheckRun(
            run_id=run.run_id,
            plan_node_id=node.plan_node_id,
            attempt_id=attempt.attempt_id,
            check_id=check_id,
            created_at=NOW + timedelta(seconds=1),
        ).start(at=NOW + timedelta(seconds=1))
        if with_running_check
        else None
    )
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
        uow.states.put_attempt(attempt)
        if check_run is not None:
            uow.states.put_check_run(check_run)
        uow.commit()
    return (
        run.run_id,
        attempt.attempt_id,
        node.plan_node_id,
        (None if check_run is None else check_run.check_run_id),
    )


def test_startup_interrupts_running_check_after_worker_succeeded(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "check-crash.sqlite3")
    run_id, attempt_id, node_id, check_run_id = _seed_post_worker_state(
        database,
        node_status=PlanNodeStatus.VERIFYING,
        with_running_check=True,
    )
    assert check_run_id is not None
    service = RecoveryService(uow_factory=database.unit_of_work, clock=lambda: RECOVERY_TIME)

    report = service.recover_startup()

    assert report.interrupted_attempt_ids == ()
    assert report.interrupted_check_run_ids == (check_run_id,)
    assert report.failed_plan_node_ids == (node_id,)
    with database.unit_of_work() as uow:
        run = uow.states.get_run(run_id)
        attempt = uow.states.get_attempt(attempt_id)
        check_run = uow.states.get_check_run(check_run_id)
        plan = uow.states.get_plan_revision(run.plan_revision_id) if run is not None else None
        events = tuple(item.event.type for item in uow.events.list_events())
    assert run is not None and run.status is RunStatus.PAUSED
    assert attempt is not None and attempt.status is AttemptStatus.SUCCEEDED
    assert check_run is not None and check_run.status is CheckRunStatus.INTERRUPTED
    assert plan is not None and plan.nodes[0].status is PlanNodeStatus.FAILED
    assert events == (
        EventType.CHECK_INTERRUPTED,
        EventType.PLAN_NODE_FAILED,
        EventType.RUN_PAUSED,
    )


@pytest.mark.parametrize(
    "node_status",
    [PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING],
)
def test_startup_fails_post_worker_node_without_active_attempt_or_check(
    tmp_path,
    node_status: PlanNodeStatus,
) -> None:
    database = SQLiteDatabase(tmp_path / f"{node_status.value}-crash.sqlite3")
    run_id, attempt_id, node_id, check_run_id = _seed_post_worker_state(
        database,
        node_status=node_status,
        with_running_check=False,
    )
    assert check_run_id is None
    service = RecoveryService(uow_factory=database.unit_of_work, clock=lambda: RECOVERY_TIME)

    report = service.recover_startup()

    assert report.interrupted_attempt_ids == ()
    assert report.interrupted_check_run_ids == ()
    assert report.failed_plan_node_ids == (node_id,)
    with database.unit_of_work() as uow:
        run = uow.states.get_run(run_id)
        attempt = uow.states.get_attempt(attempt_id)
        plan = uow.states.get_plan_revision(run.plan_revision_id) if run is not None else None
    assert run is not None and run.status is RunStatus.PAUSED
    assert attempt is not None and attempt.status is AttemptStatus.SUCCEEDED
    assert plan is not None and plan.nodes[0].status is PlanNodeStatus.FAILED


def _seed_run_without_checkpoint(database: SQLiteDatabase, status: RunStatus) -> ID:
    project = Project.create("invalid", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "invalid", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("done",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = PlanNode(new_id(), "node", "work", required_check_ids=(check_id,))
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(node,),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(goal.goal_id, plan.plan_revision_id, run_id=new_id(), created_at=NOW)
    if status is RunStatus.RUNNING:
        run = run.start(at=NOW)
    elif status is RunStatus.PAUSED:
        run = run.start(at=NOW).pause()
    elif status is RunStatus.CANCELLED:
        run = run.cancel(at=NOW)
    elif status is RunStatus.COMPLETED:
        decision = GateDecision(
            gate_id=new_id(),
            run_id=run.run_id,
            plan_node_id=node.plan_node_id,
            attempt_id=new_id(),
            passed=True,
            evaluated_at=NOW,
            required_check_ids=(check_id,),
            failed_check_ids=(),
            evidence_artifact_ids=(new_id(),),
        )
        run = run.start(at=NOW).complete(decision, at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
        uow.commit()
    return run.run_id


def test_startup_pauses_running_run_without_active_attempt(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "crash-window.sqlite3")
    run_id = _seed_run_without_checkpoint(database, RunStatus.RUNNING)
    service = RecoveryService(uow_factory=database.unit_of_work, clock=lambda: RECOVERY_TIME)

    report = service.recover_startup()

    assert report.paused_run_ids == (run_id,)
    assert report.interrupted_attempt_ids == ()
    assert report.failed_plan_node_ids == ()
    with database.unit_of_work() as uow:
        run = uow.states.get_run(run_id)
        events = uow.events.list_events()
    assert run is not None and run.status is RunStatus.PAUSED
    assert len(events) == 1 and events[0].event.type is EventType.RUN_PAUSED


def test_restore_rejects_missing_checkpoint_terminal_and_unknown_runs(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "invalid.sqlite3")
    paused_run_id = _seed_run_without_checkpoint(database, RunStatus.PAUSED)
    completed_run_id = _seed_run_without_checkpoint(database, RunStatus.COMPLETED)
    cancelled_run_id = _seed_run_without_checkpoint(database, RunStatus.CANCELLED)
    service = RecoveryService(uow_factory=database.unit_of_work, clock=lambda: RECOVERY_TIME)

    with pytest.raises(NoCheckpointError):
        service.restore_latest(paused_run_id)
    for run_id in (completed_run_id, cancelled_run_id):
        with pytest.raises(InvalidRecoveryStateError, match="cannot be restored"):
            service.restore_latest(run_id)
    with pytest.raises(RecoveryError, match="not persisted"):
        service.restore_latest(new_id())


def test_restore_fails_closed_when_checkpoint_event_is_corrupted(tmp_path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    run_id, _, _, _, gate_offset = _seed_recoverable(SQLiteDatabase(path))
    service = RecoveryService(
        uow_factory=SQLiteDatabase(path).unit_of_work,
        clock=lambda: RECOVERY_TIME,
    )
    service.recover_startup()
    invalid_event = Event(
        type=EventType.RUN_STARTED,
        correlation_id=run_id,
        run_id=run_id,
        payload={},
        occurred_at=NOW,
    )
    connection = SQLiteDatabase(path).connect()
    try:
        connection.execute(
            "UPDATE event_log SET event_json = ? WHERE event_offset = ?",
            (invalid_event.to_json(), gate_offset),
        )
    finally:
        connection.close()

    with pytest.raises(InvalidRecoveryStateError, match="GatePassed Event"):
        service.restore_latest(run_id)
