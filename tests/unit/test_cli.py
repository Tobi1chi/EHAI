from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from ehai import json_loads
from ehai.application.commands import CreateGoal, CreateProject, ProposePlan
from ehai.application.orchestrator import OrchestrationError
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION
from ehai.application.service import ExecutionService
from ehai.interfaces import cli


def test_cli_maps_orchestration_errors_to_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def fail_to_build(
        _database: Path,
        _artifacts: Path,
        *,
        worker_kind: str,
        worker_workspace: Path | None,
        planner_kind: str,
    ) -> ExecutionService:
        del worker_kind, worker_workspace
        assert planner_kind == "single"
        raise OrchestrationError("controlled orchestration failure")

    monkeypatch.setattr(cli, "build_service", fail_to_build)

    result = cli.main(
        [
            "--database",
            str(tmp_path / "state.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "create-project",
            "--idempotency-key",
            "project",
            "--name",
            "project",
        ]
    )

    captured = capsys.readouterr()
    error = json_loads(captured.err.strip())
    assert result == 2
    assert error == {
        "error": "controlled orchestration failure",
        "error_type": "OrchestrationError",
    }
    assert captured.out == ""


def test_build_service_defaults_to_single_planner(tmp_path: Path) -> None:
    service = cli.build_service(tmp_path / "single.sqlite3", tmp_path / "single-artifacts")
    project = service.create_project(CreateProject("project", "single"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "single goal"))

    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,)))

    assert len(plan.nodes) == 1
    assert plan.branches == ()


def test_build_service_selects_exploration_planner(tmp_path: Path) -> None:
    service = cli.build_service(
        tmp_path / "exploration.sqlite3",
        tmp_path / "exploration-artifacts",
        planner_kind="exploration",
    )
    project = service.create_project(CreateProject("project", "exploration"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "explore goal"))

    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,)))

    assert len(plan.nodes) == 5
    assert len(plan.branches) == 2


def test_parser_accepts_minimal_exploration_selection(tmp_path: Path) -> None:
    args = cli.create_parser().parse_args(
        [
            "--database",
            str(tmp_path / "state.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--planner",
            "exploration",
            "create-project",
            "--idempotency-key",
            "project",
            "--name",
            "project",
        ]
    )

    assert args.planner == "exploration"
