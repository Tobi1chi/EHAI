"""Local FastAPI/Uvicorn composition root for the P1 Execution Plane."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from ehai import json_loads
from ehai.application.queries import QueryService
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.api import create_app
from ehai.interfaces.cli import build_service


def create_local_app(
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
) -> FastAPI:
    """Construct one long-lived Command service and short-lived read sessions."""
    execution_service = build_service(
        database_path,
        artifact_root,
        worker_kind=worker_kind,
        worker_workspace=worker_workspace,
        planner_kind=planner_kind,
        planner_timeout_seconds=planner_timeout_seconds,
        command_check_argv=command_check_argv,
        semantic_required_terms=semantic_required_terms,
        worker_timeout_seconds=worker_timeout_seconds,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
    )
    execution_service.recover_startup()
    query_database = SQLiteDatabase(database_path)
    query_service = QueryService(read_session_factory=query_database.read_session)
    return create_app(execution_service, query_service)


def create_parser() -> argparse.ArgumentParser:
    """Create the local API server command line."""
    parser = argparse.ArgumentParser(prog="ehai-api", description="EHAI P1 HTTP API")
    parser.add_argument("--database", type=Path, default=Path(".ehai/state.sqlite3"))
    parser.add_argument("--artifacts", type=Path, default=Path(".ehai/artifacts"))
    parser.add_argument("--worker", choices=("fake", "codex"), default="fake")
    parser.add_argument("--worker-workspace", type=Path)
    parser.add_argument(
        "--planner",
        choices=("single", "exploration", "codex"),
        default="single",
    )
    parser.add_argument("--planner-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--worker-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--codex-model")
    parser.add_argument(
        "--codex-reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument(
        "--command-check-argv",
        help="trusted host Command Check argv as a JSON string array",
    )
    parser.add_argument("--semantic-required-term", action="append", default=[])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local P1 API without introducing a second application service."""
    args = create_parser().parse_args(argv)
    app = create_local_app(
        args.database,
        args.artifacts,
        worker_kind=args.worker,
        worker_workspace=args.worker_workspace,
        planner_kind=args.planner,
        planner_timeout_seconds=args.planner_timeout_seconds,
        worker_timeout_seconds=args.worker_timeout_seconds,
        codex_model=args.codex_model,
        codex_reasoning_effort=args.codex_reasoning_effort,
        command_check_argv=_parse_command_argv(args.command_check_argv),
        semantic_required_terms=tuple(args.semantic_required_term),
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


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


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
