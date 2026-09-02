"""Minimal JSON CLI for the P1 execution slice."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ehai import JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.checkpointing import RecoveryError, RecoveryService
from ehai.application.checks import CheckAdapter, CheckRunner
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
from ehai.application.orchestrator import OrchestrationError, Orchestrator
from ehai.application.planner import (
    COMMAND_EXIT_ZERO_CRITERION,
    P1_COMPLETION_CRITERIA,
    SEMANTIC_REQUIRED_TERMS_CRITERION,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    Planner,
    PlanProposal,
)
from ehai.application.run_control import RunControlError, RunController
from ehai.application.service import ApplicationError, ExecutionService
from ehai.application.workers import WorkerAdapter
from ehai.domain.checking import CheckKind
from ehai.domain.goal import Goal
from ehai.domain.planning import PlanRevision
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import (
    ArtifactCheckAdapter,
    ArtifactCheckRule,
    CommandCheckAdapter,
    SemanticCheckAdapter,
    SemanticRubric,
)
from ehai.infrastructure.planners import CodexPlannerAdapter, CodexPlannerError
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import CodexWorkerAdapter, FakeWorker


class _ExplorationPlannerAdapter:
    """Adapt the I6 request object to the application Planner Port."""

    def __init__(self) -> None:
        self._planner = DeterministicExplorationPlanner()
        self._budget = ExplorationBudget(max_attempts=5)

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        return self._planner.propose(
            ExplorationPlanRequest(
                goal=goal,
                criteria=criteria,
                budget=self._budget,
            )
        )

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
    ) -> PlanProposal:
        return self._planner.replan(
            ExplorationPlanRequest(
                goal=goal,
                criteria=criteria,
                budget=self._budget,
            ),
            base,
        )


def build_service(
    database_path: Path,
    artifact_root: Path,
    *,
    worker_kind: str = "fake",
    worker_workspace: Path | None = None,
    planner_kind: str = "single",
    planner_timeout_seconds: float = 120.0,
    command_check_argv: Sequence[str] | None = None,
    semantic_required_terms: Sequence[str] = (),
    worker_timeout_seconds: float = 300.0,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    background_start: bool = False,
) -> ExecutionService:
    """Build the local P1 service from concrete infrastructure Adapters."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = SQLiteDatabase(database_path)
    artifact_store = FilesystemArtifactStore(artifact_root)
    worker: WorkerAdapter | None
    if worker_kind == "fake":
        worker = FakeWorker()
    elif worker_kind == "codex":
        worker = CodexWorkerAdapter(
            workspace=worker_workspace or Path.cwd(),
            timeout_seconds=worker_timeout_seconds,
            model=codex_model,
            reasoning_effort=codex_reasoning_effort,
        )
    elif worker_kind == "builtin":
        if not background_start:
            raise ValueError("Built-in Worker requires the P2 background Runtime")
        worker = None
    else:
        raise ValueError(f"unsupported Worker: {worker_kind}")
    planner: Planner
    if planner_kind == "single":
        planner = DeterministicPlanner()
    elif planner_kind == "exploration":
        planner = _ExplorationPlannerAdapter()
    elif planner_kind == "codex":
        planner = CodexPlannerAdapter(
            workspace=worker_workspace or Path.cwd(),
            timeout_seconds=planner_timeout_seconds,
        )
    else:
        raise ValueError(f"unsupported Planner: {planner_kind}")
    adapters: dict[CheckKind, CheckAdapter] = {
        CheckKind.ARTIFACT: ArtifactCheckAdapter(
            artifact_store,
            {},
            default_rule=ArtifactCheckRule(minimum_count=1, require_non_empty=True),
        )
    }
    if command_check_argv is not None:
        adapters[CheckKind.COMMAND] = CommandCheckAdapter(
            {},
            store=artifact_store,
            default_argv=tuple(command_check_argv),
        )
    semantic_terms = tuple(term.strip() for term in semantic_required_terms if term.strip())
    if semantic_terms:
        adapters[CheckKind.SEMANTIC] = SemanticCheckAdapter(
            artifact_store,
            {},
            default_rubric=SemanticRubric(
                description="required host-configured terms must appear",
                required_terms=semantic_terms,
                minimum_score=1.0,
            ),
        )
    check_runner = CheckRunner(
        adapters,
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=check_runner,
        workspace=database_path.parent,
    )
    return ExecutionService(
        uow_factory=database.unit_of_work,
        planner=planner,
        orchestrator=orchestrator,
        run_controller=(None if worker is None else RunController(database.unit_of_work, worker)),
        recovery_service=RecoveryService(uow_factory=database.unit_of_work),
        background_start=background_start,
    )


def create_parser() -> argparse.ArgumentParser:
    """Create the stable argparse surface used by tests and the console script."""
    parser = argparse.ArgumentParser(prog="ehai", description="EHAI P1 Execution Plane")
    parser.add_argument("--database", type=Path, required=True, help="SQLite database path")
    parser.add_argument(
        "--artifacts",
        type=Path,
        required=True,
        help="immutable Artifact store root",
    )
    parser.add_argument(
        "--worker",
        choices=("fake", "codex"),
        default="fake",
        help="Worker Adapter (default: fake)",
    )
    parser.add_argument(
        "--worker-workspace",
        type=Path,
        help="Codex working directory (default: current directory)",
    )
    parser.add_argument(
        "--planner",
        choices=("single", "exploration", "codex"),
        default="single",
        help="Planner implementation (default: single)",
    )
    parser.add_argument(
        "--planner-timeout-seconds",
        type=float,
        default=120.0,
        help="Codex Planner wall-clock timeout (default: 120)",
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=300.0,
        help="Codex Worker wall-clock timeout (default: 300)",
    )
    parser.add_argument("--codex-model", help="Codex model override for Worker invocations")
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        help="Codex reasoning effort override for Worker invocations",
    )
    parser.add_argument(
        "--command-check-argv",
        help=(
            "trusted host Command Check argv as a JSON string array, for criterion "
            f"{COMMAND_EXIT_ZERO_CRITERION}"
        ),
    )
    parser.add_argument(
        "--semantic-required-term",
        action="append",
        default=[],
        help=(
            "trusted term required by the Semantic Check; repeat for criterion "
            f"{SEMANTIC_REQUIRED_TERMS_CRITERION}"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("create-project", help="create a Project")
    project.add_argument("--idempotency-key", required=True)
    project.add_argument("--name", required=True)

    goal = commands.add_parser("create-goal", help="create a Goal")
    goal.add_argument("--idempotency-key", required=True)
    goal.add_argument("--project-id", required=True)
    goal.add_argument("--objective", required=True)

    propose = commands.add_parser("propose-plan", help="propose a plan")
    propose.add_argument("--idempotency-key", required=True)
    propose.add_argument("--goal-id", required=True)
    propose.add_argument(
        "--criterion",
        action="append",
        required=True,
        help="P1 requires exactly one value: " + ", ".join(sorted(P1_COMPLETION_CRITERIA)),
    )

    replan = commands.add_parser("replan-plan", help="create a new draft from an approved plan")
    replan.add_argument("--idempotency-key", required=True)
    replan.add_argument("--base-plan-revision-id", required=True)
    replan.add_argument(
        "--criterion",
        action="append",
        required=True,
        help="P1 requires exactly one value: " + ", ".join(sorted(P1_COMPLETION_CRITERIA)),
    )

    approve = commands.add_parser("approve-plan", help="confirm and approve a proposal")
    approve.add_argument("--idempotency-key", required=True)
    approve.add_argument("--plan-revision-id", required=True)
    approve.add_argument("--completion-contract-id", required=True)

    start = commands.add_parser("start-run", help="execute an approved plan")
    start.add_argument("--idempotency-key", required=True)
    start.add_argument("--plan-revision-id", required=True)

    pause = commands.add_parser("pause-run", help="pause a running Run")
    pause.add_argument("--idempotency-key", required=True)
    pause.add_argument("--run-id", required=True)

    resume = commands.add_parser("resume-run", help="resume and continue a paused Run")
    resume.add_argument("--idempotency-key", required=True)
    resume.add_argument("--run-id", required=True)

    cancel = commands.add_parser("cancel-run", help="cancel a non-terminal Run")
    cancel.add_argument("--idempotency-key", required=True)
    cancel.add_argument("--run-id", required=True)
    cancel.add_argument("--reason")

    restore = commands.add_parser("restore-run", help="restore the latest Checkpoint")
    restore.add_argument("--run-id", required=True)

    commands.add_parser("recover", help="reconcile interrupted work after restart")

    query = commands.add_parser("get-run", help="query one Run")
    query.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI Command and emit exactly one JSON object."""
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        service = build_service(
            args.database,
            args.artifacts,
            worker_kind=args.worker,
            worker_workspace=args.worker_workspace,
            planner_kind=args.planner,
            planner_timeout_seconds=args.planner_timeout_seconds,
            command_check_argv=_parse_command_argv(args.command_check_argv),
            semantic_required_terms=tuple(args.semantic_required_term),
            worker_timeout_seconds=args.worker_timeout_seconds,
            codex_model=args.codex_model,
            codex_reasoning_effort=args.codex_reasoning_effort,
        )
        output = _dispatch(service, args)
    except (
        ApplicationError,
        CodexPlannerError,
        OrchestrationError,
        RecoveryError,
        RunControlError,
        ValueError,
        OSError,
    ) as error:
        print(
            json_dumps({"error": str(error), "error_type": type(error).__name__}), file=sys.stderr
        )
        return 2
    print(json_dumps(output))
    return 0


def _dispatch(service: ExecutionService, args: argparse.Namespace) -> dict[str, JsonValue]:
    command = str(args.command)
    if command == "create-project":
        project = service.create_project(CreateProject(args.idempotency_key, args.name))
        return {"name": project.name, "project_id": project.project_id}
    if command == "create-goal":
        goal = service.create_goal(
            CreateGoal(
                args.idempotency_key,
                normalize_id(args.project_id),
                args.objective,
            )
        )
        return {
            "goal_id": goal.goal_id,
            "project_id": goal.project_id,
            "status": goal.status.value,
        }
    if command == "propose-plan":
        plan = service.propose_plan(
            ProposePlan(
                args.idempotency_key,
                normalize_id(args.goal_id),
                tuple(args.criterion),
            )
        )
        return {
            "completion_contract_id": plan.completion_contract_id,
            "plan_revision_id": plan.plan_revision_id,
            "status": plan.status.value,
        }
    if command == "replan-plan":
        plan = service.replan_plan(
            ReplanPlan(
                args.idempotency_key,
                normalize_id(args.base_plan_revision_id),
                tuple(args.criterion),
            )
        )
        return {
            "completion_contract_id": plan.completion_contract_id,
            "plan_revision_id": plan.plan_revision_id,
            "status": plan.status.value,
            "supersedes_plan_revision_id": plan.supersedes_plan_revision_id,
            "version": plan.version,
        }
    if command == "approve-plan":
        plan = service.approve_plan(
            ApprovePlan(
                args.idempotency_key,
                normalize_id(args.plan_revision_id),
                normalize_id(args.completion_contract_id),
            )
        )
        return {"plan_revision_id": plan.plan_revision_id, "status": plan.status.value}
    if command == "start-run":
        run = service.start_run(StartRun(args.idempotency_key, normalize_id(args.plan_revision_id)))
        return {
            "goal_id": run.goal_id,
            "plan_revision_id": run.plan_revision_id,
            "run_id": run.run_id,
            "status": run.status.value,
        }
    if command == "pause-run":
        run = service.pause_run(PauseRun(args.idempotency_key, normalize_id(args.run_id)))
        return {"run_id": run.run_id, "status": run.status.value}
    if command == "resume-run":
        run = service.resume_run(ResumeRun(args.idempotency_key, normalize_id(args.run_id)))
        return {"run_id": run.run_id, "status": run.status.value}
    if command == "cancel-run":
        run = service.cancel_run(
            CancelRun(args.idempotency_key, normalize_id(args.run_id), args.reason)
        )
        return {"run_id": run.run_id, "status": run.status.value}
    if command == "restore-run":
        run = service.restore_latest_checkpoint(normalize_id(args.run_id))
        return {"run_id": run.run_id, "status": run.status.value}
    if command == "recover":
        report = service.recover_startup()
        return {
            "failed_plan_node_ids": list(report.failed_plan_node_ids),
            "interrupted_attempt_ids": list(report.interrupted_attempt_ids),
            "paused_run_ids": list(report.paused_run_ids),
        }
    if command == "get-run":
        run = service.get_run(normalize_id(args.run_id))
        return {
            "goal_id": run.goal_id,
            "plan_revision_id": run.plan_revision_id,
            "run_id": run.run_id,
            "status": run.status.value,
        }
    raise RuntimeError(f"unsupported CLI command: {command}")


def _parse_command_argv(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    decoded = json_loads(value)
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(item, str) or not item for item in decoded)
    ):
        raise ValueError("--command-check-argv must be a non-empty JSON string array")
    return tuple(item for item in decoded if isinstance(item, str))


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
