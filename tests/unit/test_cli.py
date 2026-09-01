from pathlib import Path
from types import SimpleNamespace

from pytest import CaptureFixture, MonkeyPatch

from ehai import json_loads, new_id
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    ReplanPlan,
)
from ehai.application.orchestrator import OrchestrationError
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION
from ehai.application.service import ExecutionService
from ehai.infrastructure.planners import CodexPlannerError
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
        planner_timeout_seconds: float,
    ) -> ExecutionService:
        del worker_kind, worker_workspace
        assert planner_kind == "single"
        assert planner_timeout_seconds == 120.0
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


def test_cli_maps_codex_planner_errors_to_json(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    def fail_to_build(
        _database: Path,
        _artifacts: Path,
        **_options: object,
    ) -> ExecutionService:
        raise CodexPlannerError("controlled planner failure")

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
    assert result == 2
    assert json_loads(captured.err.strip()) == {
        "error": "controlled planner failure",
        "error_type": "CodexPlannerError",
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
    approved = service.approve_plan(
        ApprovePlan("approve", plan.plan_revision_id, plan.completion_contract_id)
    )
    replanned = service.replan_plan(
        ReplanPlan("replan", approved.plan_revision_id, (NON_EMPTY_ARTIFACT_CRITERION,))
    )

    assert len(plan.nodes) == 5
    assert len(plan.branches) == 2
    assert replanned.version == 2
    assert replanned.supersedes_plan_revision_id == approved.plan_revision_id
    assert len(replanned.nodes) == 5
    assert len(replanned.branches) == 2


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


def test_parser_accepts_codex_planner_with_independent_timeout(tmp_path: Path) -> None:
    args = cli.create_parser().parse_args(
        [
            "--database",
            str(tmp_path / "state.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "--planner",
            "codex",
            "--planner-timeout-seconds",
            "17",
            "create-project",
            "--idempotency-key",
            "project",
            "--name",
            "project",
        ]
    )

    assert args.planner == "codex"
    assert args.planner_timeout_seconds == 17


def test_parser_accepts_replan_product_command(tmp_path: Path) -> None:
    base_plan_revision_id = new_id()
    args = cli.create_parser().parse_args(
        [
            "--database",
            str(tmp_path / "state.sqlite3"),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "replan-plan",
            "--idempotency-key",
            "replan",
            "--base-plan-revision-id",
            base_plan_revision_id,
            "--criterion",
            NON_EMPTY_ARTIFACT_CRITERION,
        ]
    )

    assert args.command == "replan-plan"
    assert args.idempotency_key == "replan"
    assert args.base_plan_revision_id == base_plan_revision_id
    assert args.criterion == [NON_EMPTY_ARTIFACT_CRITERION]

    class _Service:
        command: ReplanPlan | None = None

        def replan_plan(self, command: ReplanPlan) -> object:
            self.command = command
            return SimpleNamespace(
                completion_contract_id=new_id(),
                plan_revision_id=new_id(),
                status=SimpleNamespace(value="draft"),
                supersedes_plan_revision_id=base_plan_revision_id,
                version=2,
            )

    service = _Service()
    output = cli._dispatch(service, args)  # type: ignore[arg-type]

    assert service.command == ReplanPlan(
        "replan",
        base_plan_revision_id,
        (NON_EMPTY_ARTIFACT_CRITERION,),
    )
    assert output["version"] == 2
    assert output["supersedes_plan_revision_id"] == base_plan_revision_id
