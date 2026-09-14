"""Minimal JSON CLI for the P1 execution slice."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections.abc import Sequence
from io import TextIOWrapper
from pathlib import Path

from fastapi import FastAPI

from ehai import JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.checkpointing import RecoveryError, RecoveryService
from ehai.application.checks import CheckAdapter, CheckRunner
from ehai.application.commands import (
    ApplyProcess,
    ApprovePlan,
    CancelRun,
    CreateGoal,
    CreateProject,
    DecideHumanCheck,
    DiscussPlan,
    PauseRun,
    ProposePlan,
    ProposeProcess,
    ReplanPlan,
    ReplyIntervention,
    ResumeRun,
    ReviewProcess,
    StartRun,
)
from ehai.application.legacy_config import ResponsesEndpointCapabilities
from ehai.application.orchestrator import OrchestrationError, Orchestrator
from ehai.application.planner import (
    COMMAND_EXIT_ZERO_CRITERION,
    P1_COMPLETION_CRITERIA,
    SEMANTIC_REQUIRED_TERMS_CRITERION,
    ConfiguredCheckPlanner,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    Planner,
    PlanProposal,
    ReplanContext,
)
from ehai.application.ports import StateConflictError
from ehai.application.queries import QueryNotFoundError, QueryService
from ehai.application.run_control import BackgroundRunController, RunControlError, RunController
from ehai.application.service import ApplicationError, ExecutionService
from ehai.application.workers import WorkerAdapter
from ehai.domain.checking import CheckKind
from ehai.domain.goal import Goal
from ehai.domain.planning import PlanRevision
from ehai.infrastructure.agent_traces import SQLiteAgentTraceStore
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import (
    ArtifactCheckAdapter,
    ArtifactCheckRule,
    CommandCheckAdapter,
    SemanticCheckAdapter,
)
from ehai.infrastructure.pi_config import PiBackendConfig
from ehai.infrastructure.pi_rpc import PiRpcError
from ehai.infrastructure.planners import (
    CodexPlannerAdapter,
    CodexPlannerError,
    PiPlannerAdapter,
    PiPlannerError,
)
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import CodexWorkerAdapter, FakeWorker
from ehai.interfaces.public_documents import public_json_value
from ehai.interfaces.session_host import (
    ExecutionConfig,
    ForegroundSessionHost,
    SessionHostError,
    get_result_document,
    load_execution_config,
    prepare_execute_run,
    prepare_resume_run,
)


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
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        return self._planner.replan(
            ExplorationPlanRequest(
                goal=goal,
                criteria=criteria,
                budget=self._budget,
            ),
            base,
            context,
        )


def build_service(
    database_path: Path,
    artifact_root: Path,
    *,
    worker_kind: str = "fake",
    worker_workspace: Path | None = None,
    planner_kind: str | None = None,
    planner_timeout_seconds: float = 120.0,
    planner_model: str | None = None,
    planner_reasoning_effort: str | None = None,
    command_check_argv: Sequence[str] | None = None,
    semantic_required_terms: Sequence[str] = (),
    command_check_timeout_seconds: float = 30.0,
    worker_timeout_seconds: float = 300.0,
    codex_model: str | None = None,
    codex_reasoning_effort: str | None = None,
    background_start: bool = False,
    endpoint_capabilities: ResponsesEndpointCapabilities | None = None,
    pi_backend: PiBackendConfig | None = None,
) -> ExecutionService:
    """Build the local service from concrete infrastructure Adapters."""
    from ehai.interfaces.agent_backends import resolve_planner_kind

    planner_kind = resolve_planner_kind(
        planner_kind, pi_configured=pi_backend is not None, model=planner_model
    )
    if worker_kind == "builtin" or planner_kind == "builtin":
        raise ValueError("The self-written Built-in Runtime was removed; configure Pi explicitly")
    if planner_kind == "pi":
        if pi_backend is None:
            raise ValueError("Pi Planner requires --pi-config")
        if planner_model is None or not planner_model.strip():
            raise ValueError("Pi Planner requires --planner-model")
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
    elif worker_kind in {"pi", "codex-server"}:
        if not background_start:
            raise ValueError("this Worker requires the P2 background Runtime")
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
    elif planner_kind == "pi":
        assert pi_backend is not None and planner_model is not None
        planner = PiPlannerAdapter(
            backend=pi_backend,
            model=planner_model,
            reasoning_effort=planner_reasoning_effort,
            session_store=SQLiteAgentTraceStore(database),
            workspace=worker_workspace or Path.cwd(),
            artifact_store=artifact_store,
            check_configuration={
                "command_argv": [] if command_check_argv is None else list(command_check_argv),
                "semantic_required_terms": list(semantic_required_terms),
            },
        )
    else:
        raise ValueError(f"unsupported Planner: {planner_kind}")
    planner = ConfiguredCheckPlanner(
        planner,
        command_argv=() if command_check_argv is None else tuple(command_check_argv),
        semantic_required_terms=tuple(semantic_required_terms),
    )
    adapters: dict[CheckKind, CheckAdapter] = {
        CheckKind.ARTIFACT: ArtifactCheckAdapter(
            artifact_store,
            {},
            default_rule=ArtifactCheckRule(minimum_count=1, require_non_empty=True),
        ),
        CheckKind.COMMAND: CommandCheckAdapter(
            {},
            store=artifact_store,
            timeout_seconds=command_check_timeout_seconds,
        ),
        CheckKind.SEMANTIC: SemanticCheckAdapter(artifact_store, {}),
    }
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
        run_controller=(
            BackgroundRunController(database.unit_of_work)
            if worker is None
            else RunController(database.unit_of_work, worker)
        ),
        recovery_service=RecoveryService(uow_factory=database.unit_of_work),
        background_start=background_start,
        planning_workspace=str((worker_workspace or Path.cwd()).resolve()),
    )


def create_parser() -> argparse.ArgumentParser:
    """Create the stable argparse surface used by tests and the console script."""
    parser = argparse.ArgumentParser(prog="ehai", description="EHAI P1 Execution Plane")
    parser.add_argument("--pi-config", type=Path, help="explicit native Pi backend configuration")
    parser.add_argument("--database", type=Path, required=True, help="SQLite database path")
    parser.add_argument(
        "--artifacts",
        type=Path,
        required=True,
        help="immutable Artifact store root",
    )
    parser.add_argument(
        "--worker",
        choices=("fake", "codex", "codex-server", "pi"),
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
        choices=("single", "exploration", "codex", "pi"),
        default=None,
        help="Planner implementation (Pi when --pi-config/--planner-model is supplied)",
    )
    parser.add_argument(
        "--planner-timeout-seconds",
        type=float,
        default=120.0,
        help="Codex Planner wall-clock timeout (default: 120)",
    )
    parser.add_argument("--planner-model", help="exact native Pi model ID for the Planner")
    parser.add_argument(
        "--planner-reasoning-effort",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        help="OpenAI reasoning effort for Pi Planner",
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=300.0,
        help="Codex Worker wall-clock timeout (default: 300)",
    )
    parser.add_argument(
        "--command-check-timeout-seconds",
        type=float,
        default=30.0,
        help="host Command Check timeout (default: 30)",
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
        help=(
            "repeat for executable checks or human:<question>: "
            + ", ".join(sorted(P1_COMPLETION_CRITERIA))
        ),
    )

    replan = commands.add_parser("replan-plan", help="create a new draft from an approved plan")
    replan.add_argument("--idempotency-key", required=True)
    replan.add_argument("--base-plan-revision-id", required=True)
    replan.add_argument("--source-run-id")
    replan.add_argument(
        "--criterion",
        action="append",
        required=True,
        help=(
            "repeat for executable checks or human:<question>: "
            + ", ".join(sorted(P1_COMPLETION_CRITERIA))
        ),
    )

    propose_process = commands.add_parser(
        "propose-process",
        help="generate a retained process draft for a running or paused Run",
    )
    propose_process.add_argument("--idempotency-key", required=True)
    propose_process.add_argument("--run-id", required=True)
    propose_process.add_argument("--reason", required=True)

    review_process = commands.add_parser(
        "review-process",
        help="independently review a retained process draft",
    )
    review_process.add_argument("--idempotency-key", required=True)
    review_process.add_argument("--draft-id", required=True)

    apply_process = commands.add_parser(
        "apply-process",
        help="apply a process draft covered by a preserving review",
    )
    apply_process.add_argument("--idempotency-key", required=True)
    apply_process.add_argument("--review-id", required=True)

    approve = commands.add_parser("approve-plan", help="confirm and approve a proposal")
    approve.add_argument("--idempotency-key", required=True)
    approve.add_argument("--plan-revision-id", required=True)
    approve.add_argument("--completion-contract-id", required=True)

    start = commands.add_parser("start-run", help="execute an approved plan")
    start.add_argument("--idempotency-key", required=True)
    start.add_argument("--plan-revision-id", required=True)

    decide_check = commands.add_parser(
        "decide-human-check",
        help="record an explicit human verdict for an open CheckRun",
    )
    decide_check.add_argument("--idempotency-key", required=True)
    decide_check.add_argument("--check-run-id", required=True)
    decide_check.add_argument("--request-token", required=True)
    verdict = decide_check.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--passed", action="store_true", help="approve the human Check")
    verdict.add_argument("--rejected", action="store_true", help="reject the human Check")
    decide_check.add_argument("--actor", required=True)
    decide_check.add_argument("--comment", required=True)

    reply_intervention = commands.add_parser(
        "reply-intervention",
        help="confirm that a Worker blocker is resolved within the approved boundary",
    )
    reply_intervention.add_argument("--idempotency-key", required=True)
    reply_intervention.add_argument("--intervention-id", required=True)
    reply_intervention.add_argument("--request-token", required=True)
    reply_intervention.add_argument("--actor", required=True)
    reply_intervention.add_argument("--message", required=True)

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

    run_plan_query = commands.add_parser(
        "get-run-plan", help="query the current execution graph for one Run"
    )
    run_plan_query.add_argument("--run-id", required=True)

    checks_query = commands.add_parser("get-run-checks", help="read a Run's CheckRuns")
    checks_query.add_argument("--run-id", required=True)

    adoptions_query = commands.add_parser(
        "get-run-adoptions", help="read a Run's accepted cross-Run result provenance"
    )
    adoptions_query.add_argument("--run-id", required=True)

    interventions_query = commands.add_parser(
        "get-run-interventions", help="read a Run's durable Worker interventions"
    )
    interventions_query.add_argument("--run-id", required=True)

    plan_query = commands.add_parser(
        "get-plan", help="read a stored plan without invoking a Planner"
    )
    plan_query.add_argument("--plan-revision-id", required=True)

    process_query = commands.add_parser(
        "get-process-revision", help="read one immutable process graph revision"
    )
    process_query.add_argument("--process-revision-id", required=True)

    process_draft_query = commands.add_parser(
        "get-process-draft", help="read one retained process Planner draft"
    )
    process_draft_query.add_argument("--draft-id", required=True)

    run_process_drafts_query = commands.add_parser(
        "get-run-process-drafts", help="read retained process Planner drafts for one Run"
    )
    run_process_drafts_query.add_argument("--run-id", required=True)

    process_review_query = commands.add_parser(
        "get-process-review", help="read one independent process review"
    )
    process_review_query.add_argument("--review-id", required=True)

    process_draft_reviews_query = commands.add_parser(
        "get-process-draft-reviews", help="read independent reviews for one process draft"
    )
    process_draft_reviews_query.add_argument("--draft-id", required=True)

    checks_query = commands.add_parser("get-plan-checks", help="read the plan's completion checks")
    checks_query.add_argument("--plan-revision-id", required=True)

    trace_query = commands.add_parser(
        "get-trace", help="read execution, Artifact and Agent evidence"
    )
    trace_query.add_argument("--run-id", required=True)

    result_query = commands.add_parser(
        "get-result", help="read retained code delivery and execution evidence"
    )
    result_query.add_argument("--run-id", required=True)

    execute = commands.add_parser(
        "execute-plan", help="authorize and execute an approved plan in the foreground"
    )
    execute.add_argument("--idempotency-key", required=True)
    execute.add_argument("--plan-revision-id", required=True)
    execute.add_argument("--execution-config", type=Path, required=True)
    execute.add_argument(
        "--authorize",
        action="store_true",
        help="explicitly authorize the exact execution configuration for this Run",
    )

    resume_session = commands.add_parser(
        "resume-session", help="resume a durable foreground execution session"
    )
    resume_session.add_argument("--run-id", required=True)

    discuss = commands.add_parser(
        "discuss-plan", help="discuss or revise a plan without executing it"
    )
    discuss.add_argument("--idempotency-key", required=True)
    discuss.add_argument("--goal-id", required=True)
    discuss.add_argument("--conversation-id")
    discuss.add_argument("--source-run-id")
    discuss.add_argument(
        "--criterion",
        action="append",
        help=(
            "explicit completion criterion; omit to let the Planner propose a command or "
            "human final Gate"
        ),
    )
    message = discuss.add_mutually_exclusive_group(required=True)
    message.add_argument("--message")
    message.add_argument("--message-file", type=Path)

    conversation = commands.add_parser("get-discussion", help="read durable planning discussion")
    conversation.add_argument("--conversation-id", required=True)

    inspect_agent = commands.add_parser(
        "inspect-agent", help="inspect the isolated Pi RPC backend without model calls"
    )
    inspect_agent.add_argument("--node", default="node", help="native Node executable")
    inspect_agent.add_argument(
        "--pi-cli", type=Path, required=True, help="installed Pi dist/bundle/cli.js"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI Command and emit exactly one JSON document."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = create_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect-agent":
            from ehai.interfaces.agent_backends import inspect_pi_backend

            print(json_dumps(asyncio.run(inspect_pi_backend(node=args.node, cli=args.pi_cli))))
            return 0
        if args.command in {
            "get-plan",
            "get-plan-checks",
            "get-run-plan",
            "get-run-checks",
            "get-run-adoptions",
            "get-run-interventions",
            "get-process-revision",
            "get-process-draft",
            "get-run-process-drafts",
            "get-process-review",
            "get-process-draft-reviews",
            "get-trace",
            "get-result",
            "get-discussion",
        }:
            print(json_dumps(_dispatch_query(args)))
            return 0
        if args.command in {"execute-plan", "resume-session"}:
            return _run_foreground(args)
        service = build_service(
            args.database,
            args.artifacts,
            worker_kind=args.worker,
            worker_workspace=args.worker_workspace,
            planner_kind=args.planner,
            planner_timeout_seconds=args.planner_timeout_seconds,
            planner_model=args.planner_model,
            planner_reasoning_effort=args.planner_reasoning_effort,
            command_check_argv=_parse_command_argv(args.command_check_argv),
            semantic_required_terms=tuple(args.semantic_required_term),
            command_check_timeout_seconds=args.command_check_timeout_seconds,
            worker_timeout_seconds=args.worker_timeout_seconds,
            codex_model=args.codex_model,
            codex_reasoning_effort=args.codex_reasoning_effort,
            pi_backend=None
            if args.pi_config is None
            else PiBackendConfig.from_path(args.pi_config),
        )
        if args.command == "discuss-plan":
            message = (
                args.message
                if args.message_file is None
                else args.message_file.read_text(encoding="utf-8")
            )
            output = public_json_value(
                service.discuss_plan(
                    DiscussPlan(
                        args.idempotency_key,
                        normalize_id(args.goal_id),
                        message,
                        tuple(args.criterion or ()),
                        None
                        if args.conversation_id is None
                        else normalize_id(args.conversation_id),
                        source_run_id=(
                            None if args.source_run_id is None else normalize_id(args.source_run_id)
                        ),
                    )
                )
            )
        else:
            output = _dispatch(service, args)
    except (
        ApplicationError,
        StateConflictError,
        PiPlannerError,
        CodexPlannerError,
        OrchestrationError,
        QueryNotFoundError,
        RecoveryError,
        RunControlError,
        SessionHostError,
        PiRpcError,
        ValueError,
        OSError,
        sqlite3.Error,
    ) as error:
        print(
            json_dumps({"error": str(error), "error_type": type(error).__name__}), file=sys.stderr
        )
        return 2
    print(json_dumps(output))
    return 0


def _run_foreground(args: argparse.Namespace) -> int:
    if args.command == "execute-plan" and not args.authorize:
        raise SessionHostError("execute-plan requires the explicit --authorize flag")
    if args.command == "execute-plan":
        config = ExecutionConfig.from_path(args.execution_config)
        app = _build_foreground_app(args, config)
        composition = app.state.runtime_composition
        run = prepare_execute_run(
            composition.execution_service,
            composition.database,
            normalize_id(args.plan_revision_id),
            args.idempotency_key,
            config,
        )
    else:
        database = SQLiteDatabase(args.database)
        config = load_execution_config(database, normalize_id(args.run_id))
        app = _build_foreground_app(args, config)
        composition = app.state.runtime_composition
        run, config = prepare_resume_run(
            composition.execution_service,
            database,
            normalize_id(args.run_id),
            process_adjustments=composition.process_adjustments,
        )
    composition = app.state.runtime_composition
    host = ForegroundSessionHost(composition, stderr=sys.stderr)
    try:
        output = asyncio.run(host.run(run.run_id))
    except KeyboardInterrupt:
        output = get_result_document(args.database, args.artifacts, run.run_id)
        output["notice"] = {
            "kind": "cli_interrupted",
            "message": "CLI interrupted; the Run was quiesced and paused for resume-session",
            "attempt_id": None,
        }
        print(json_dumps(output))
        return 130
    print(json_dumps(output))
    return 0


def _build_foreground_app(
    args: argparse.Namespace,
    config: ExecutionConfig | None,
) -> FastAPI:
    from ehai.interfaces.runtime import create_local_app

    if config is None:
        return create_local_app(
            args.database,
            args.artifacts,
            worker_kind="fake",
            worker_workspace=Path.cwd(),
            planner_kind="single",
            p2_runtime=True,
            runtime_autostart=False,
        )
    return create_local_app(
        args.database,
        args.artifacts,
        worker_kind=config.worker_kind,
        worker_workspace=config.workspace,
        planner_kind="single" if config.process_adjustment is None else "pi",
        planner_model=(
            None if config.process_adjustment is None else config.process_adjustment.model
        ),
        planner_reasoning_effort=(
            None
            if config.process_adjustment is None
            else config.process_adjustment.reasoning_effort
        ),
        command_check_timeout_seconds=config.command_timeout_seconds,
        agent_model=config.model if config.worker_kind == "pi" else None,
        agent_reasoning_effort=(config.reasoning_effort if config.worker_kind == "pi" else None),
        agent_allowed_commands=config.allowed_commands,
        available_shells=config.available_shells,
        git_permissions=tuple(config.git_permissions),
        worker_capacity=config.capacity,
        codex_model=config.model if config.worker_kind == "codex-server" else None,
        codex_reasoning_effort=(
            config.reasoning_effort if config.worker_kind == "codex-server" else None
        ),
        codex_server_executable=config.codex_server_executable,
        codex_server_approval_policy=config.codex_server_approval_policy,
        codex_server_sandbox=config.codex_server_sandbox,
        endpoint_capabilities=config.endpoint_capabilities,
        pi_backend=config.pi_backend,
        p2_runtime=True,
        runtime_autostart=False,
    )


def _dispatch_query(args: argparse.Namespace) -> JsonValue:
    database_path = Path(args.database)
    if not database_path.is_file():
        raise FileNotFoundError(f"database does not exist: {database_path}")
    database = SQLiteDatabase(database_path)
    queries = QueryService(
        read_session_factory=database.read_session,
        builtin_session_reader=SQLiteAgentTraceStore(database),
    )
    if args.command == "get-plan":
        return public_json_value(queries.get_plan_graph(normalize_id(args.plan_revision_id)))
    if args.command == "get-run-plan":
        return public_json_value(queries.get_run_plan(normalize_id(args.run_id)))
    if args.command == "get-process-revision":
        return public_json_value(
            queries.get_process_revision(normalize_id(args.process_revision_id))
        )
    if args.command == "get-process-draft":
        return public_json_value(queries.get_process_draft(normalize_id(args.draft_id)))
    if args.command == "get-run-process-drafts":
        return public_json_value(queries.get_run_process_drafts(normalize_id(args.run_id)))
    if args.command == "get-process-review":
        return public_json_value(queries.get_process_review(normalize_id(args.review_id)))
    if args.command == "get-process-draft-reviews":
        return public_json_value(queries.get_process_draft_reviews(normalize_id(args.draft_id)))
    if args.command == "get-plan-checks":
        return public_json_value(queries.list_check_specs(normalize_id(args.plan_revision_id)))
    if args.command == "get-run-checks":
        return public_json_value(queries.list_check_runs(normalize_id(args.run_id)))
    if args.command == "get-run-adoptions":
        return public_json_value(queries.list_result_adoptions(normalize_id(args.run_id)))
    if args.command == "get-run-interventions":
        return public_json_value(queries.list_interventions(normalize_id(args.run_id)))
    if args.command == "get-discussion":
        return public_json_value(
            queries.get_planning_conversation(normalize_id(args.conversation_id))
        )
    if args.command == "get-result":
        return get_result_document(
            database_path,
            Path(args.artifacts),
            normalize_id(args.run_id),
        )
    return public_json_value(queries.get_execution_trace(normalize_id(args.run_id)))


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
                None if args.source_run_id is None else normalize_id(args.source_run_id),
            )
        )
        return {
            "completion_contract_id": plan.completion_contract_id,
            "plan_revision_id": plan.plan_revision_id,
            "status": plan.status.value,
            "supersedes_plan_revision_id": plan.supersedes_plan_revision_id,
            "version": plan.version,
        }
    if command == "propose-process":
        draft = service.propose_process(
            ProposeProcess(
                args.idempotency_key,
                normalize_id(args.run_id),
                args.reason,
            )
        )
        return {
            "draft_id": draft.draft_id,
            "status": draft.status.value,
        }
    if command == "review-process":
        review = service.review_process(
            ReviewProcess(
                args.idempotency_key,
                normalize_id(args.draft_id),
            )
        )
        return {
            "review_id": review.review_id,
            "status": review.status.value,
            "preserves_boundary": review.preserves_boundary,
        }
    if command == "apply-process":
        process = service.apply_process(
            ApplyProcess(
                args.idempotency_key,
                normalize_id(args.review_id),
            )
        )
        return {
            "process_revision_id": process.process_revision_id,
            "version": process.version,
            "run_id": process.run_id,
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
    if command == "decide-human-check":
        run = service.decide_human_check(
            DecideHumanCheck(
                args.idempotency_key,
                normalize_id(args.check_run_id),
                args.request_token,
                args.passed,
                args.actor,
                args.comment,
            )
        )
        return {"run_id": run.run_id, "status": run.status.value}
    if command == "reply-intervention":
        run = service.reply_intervention(
            ReplyIntervention(
                args.idempotency_key,
                normalize_id(args.intervention_id),
                args.request_token,
                args.actor,
                args.message,
            )
        )
        return {"run_id": run.run_id, "status": run.status.value}
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
