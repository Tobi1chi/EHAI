"""Minimal JSON CLI for the P1 single-node execution slice."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ehai import JsonValue, json_dumps, normalize_id
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    StartRun,
)
from ehai.application.orchestrator import OrchestrationError, Orchestrator
from ehai.application.planner import DeterministicPlanner
from ehai.application.service import ApplicationError, ExecutionService
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker


def build_service(database_path: Path, artifact_root: Path) -> ExecutionService:
    """Build the local P1 service from concrete infrastructure Adapters."""
    database = SQLiteDatabase(database_path)
    artifact_store = FilesystemArtifactStore(artifact_root)
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=FakeWorker(),
        artifact_store=artifact_store,
    )
    return ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=orchestrator,
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
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("create-project", help="create a Project")
    project.add_argument("--idempotency-key", required=True)
    project.add_argument("--name", required=True)

    goal = commands.add_parser("create-goal", help="create a Goal")
    goal.add_argument("--idempotency-key", required=True)
    goal.add_argument("--project-id", required=True)
    goal.add_argument("--objective", required=True)

    propose = commands.add_parser("propose-plan", help="propose a single-node plan")
    propose.add_argument("--idempotency-key", required=True)
    propose.add_argument("--goal-id", required=True)
    propose.add_argument("--criterion", action="append", required=True)

    approve = commands.add_parser("approve-plan", help="confirm and approve a proposal")
    approve.add_argument("--idempotency-key", required=True)
    approve.add_argument("--plan-revision-id", required=True)
    approve.add_argument("--completion-contract-id", required=True)

    start = commands.add_parser("start-run", help="execute an approved plan")
    start.add_argument("--idempotency-key", required=True)
    start.add_argument("--plan-revision-id", required=True)

    query = commands.add_parser("get-run", help="query one Run")
    query.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI Command and emit exactly one JSON object."""
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        service = build_service(args.database, args.artifacts)
        output = _dispatch(service, args)
    except (ApplicationError, OrchestrationError, ValueError, OSError) as error:
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
    if command == "get-run":
        run = service.get_run(normalize_id(args.run_id))
        return {
            "goal_id": run.goal_id,
            "plan_revision_id": run.plan_revision_id,
            "run_id": run.run_id,
            "status": run.status.value,
        }
    raise RuntimeError(f"unsupported CLI command: {command}")


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())
