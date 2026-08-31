from pathlib import Path

import pytest
from pytest import CaptureFixture

from ehai import JsonValue, json_loads, normalize_id
from ehai.application.commands import CreateGoal, CreateProject, ProposePlan, StartRun
from ehai.application.service import ApplicationError
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import GoalStatus
from ehai.domain.planning import PlanNodeStatus
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.cli import build_service, main


def _invoke(
    database: Path,
    artifacts: Path,
    capsys: CaptureFixture[str],
    *arguments: str,
) -> dict[str, JsonValue]:
    exit_code = main(
        [
            "--database",
            str(database),
            "--artifacts",
            str(artifacts),
            *arguments,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    decoded = json_loads(captured.out.strip())
    assert isinstance(decoded, dict)
    return decoded


def _string(result: dict[str, JsonValue], key: str) -> str:
    value = result[key]
    assert isinstance(value, str)
    return value


def test_cli_executes_idempotent_single_node_loop_on_real_sqlite(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    database_path = tmp_path / "ehai.sqlite3"
    artifact_root = tmp_path / "artifacts"
    project = _invoke(
        database_path,
        artifact_root,
        capsys,
        "create-project",
        "--idempotency-key",
        "project-1",
        "--name",
        "P1 demo",
    )
    goal = _invoke(
        database_path,
        artifact_root,
        capsys,
        "create-goal",
        "--idempotency-key",
        "goal-1",
        "--project-id",
        _string(project, "project_id"),
        "--objective",
        "produce checked evidence",
    )
    proposal = _invoke(
        database_path,
        artifact_root,
        capsys,
        "propose-plan",
        "--idempotency-key",
        "plan-1",
        "--goal-id",
        _string(goal, "goal_id"),
        "--criterion",
        "a candidate Artifact exists",
    )
    assert proposal["status"] == "draft"
    approved = _invoke(
        database_path,
        artifact_root,
        capsys,
        "approve-plan",
        "--idempotency-key",
        "approve-1",
        "--plan-revision-id",
        _string(proposal, "plan_revision_id"),
        "--completion-contract-id",
        _string(proposal, "completion_contract_id"),
    )
    assert approved["status"] == "approved"
    started = _invoke(
        database_path,
        artifact_root,
        capsys,
        "start-run",
        "--idempotency-key",
        "run-1",
        "--plan-revision-id",
        _string(proposal, "plan_revision_id"),
    )
    assert started["status"] == "completed"
    queried = _invoke(
        database_path,
        artifact_root,
        capsys,
        "get-run",
        "--run-id",
        _string(started, "run_id"),
    )
    assert queried == started

    retried = _invoke(
        database_path,
        artifact_root,
        capsys,
        "start-run",
        "--idempotency-key",
        "run-1",
        "--plan-revision-id",
        _string(proposal, "plan_revision_id"),
    )
    assert retried == started

    database = SQLiteDatabase(database_path)
    run_id = normalize_id(_string(started, "run_id"))
    goal_id = normalize_id(_string(goal, "goal_id"))
    plan_id = normalize_id(_string(proposal, "plan_revision_id"))
    with database.unit_of_work() as uow:
        stored_run = uow.states.get_run(run_id)
        stored_goal = uow.states.get_goal(goal_id)
        stored_plan = uow.states.get_plan_revision(plan_id)
        attempts = uow.states.list_attempts(run_id)
        checks = uow.states.list_check_runs(run_id)
        checkpoints = uow.states.list_checkpoints(run_id)
        artifacts = uow.states.list_artifacts_for_run(run_id)
        event_types = tuple(item.event.type for item in uow.events.list_events())

    assert stored_run is not None and stored_run.status is RunStatus.COMPLETED
    assert stored_goal is not None and stored_goal.status is GoalStatus.SATISFIED
    assert stored_plan is not None
    assert stored_plan.nodes[0].status is PlanNodeStatus.COMPLETED
    assert len(attempts) == 1 and attempts[0].status is AttemptStatus.SUCCEEDED
    assert len(checks) == 1 and checks[0].result is not None and checks[0].result.passed
    assert len(checkpoints) == 1
    assert len(artifacts) == 1
    assert EventType.PLAN_REVISION_APPROVED in event_types
    assert EventType.GATE_PASSED in event_types
    assert EventType.CHECKPOINT_CREATED in event_types
    assert event_types[-1] is EventType.RUN_COMPLETED

    service = build_service(database_path, artifact_root)
    with pytest.raises(ApplicationError, match="one Run per PlanRevision"):
        service.start_run(StartRun("run-2", normalize_id(_string(proposal, "plan_revision_id"))))
    with database.unit_of_work() as uow:
        assert len(uow.states.list_runs(goal_id)) == 1
        assert uow.command_receipts.get("run-2") is None


def test_start_run_cannot_bypass_plan_approval(tmp_path: Path) -> None:
    database_path = tmp_path / "approval.sqlite3"
    service = build_service(database_path, tmp_path / "artifacts")
    project = service.create_project(CreateProject("project", "approval"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "prove approval"))
    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, ("checked",)))

    with pytest.raises(ApplicationError, match="must be approved"):
        service.start_run(StartRun("run", plan.plan_revision_id))

    database = SQLiteDatabase(database_path)
    with database.unit_of_work() as uow:
        assert uow.states.list_runs(goal.goal_id) == ()
