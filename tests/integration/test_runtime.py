import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from pytest import MonkeyPatch, raises

from ehai import new_id
from ehai.application.builtin_agent import AgentBudget
from ehai.application.execution_policy import ExecutionPolicy
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision, PlanRevisionStatus
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces import runtime
from ehai.interfaces.runtime import create_local_app

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_runtime_parser_accepts_standalone_builtin_configuration() -> None:
    args = runtime.create_parser().parse_args(
        [
            "--worker",
            "builtin",
            "--builtin-model",
            "gpt-5.6-luna",
            "--builtin-reasoning-effort",
            "high",
            "--builtin-capacity",
            "3",
            "--builtin-agent-max-steps",
            "48",
            "--builtin-agent-max-tool-calls",
            "96",
            "--builtin-agent-wall-clock-seconds",
            "720",
            "--builtin-agent-max-output-bytes",
            "1048576",
            "--builtin-allowed-command",
            '["uv","--version"]',
            "--p2-runtime",
        ]
    )

    assert args.worker == "builtin"
    assert args.builtin_model == "gpt-5.6-luna"
    assert args.builtin_reasoning_effort == "high"
    assert args.builtin_capacity == 3
    assert args.builtin_agent_max_steps == 48
    assert args.builtin_agent_max_tool_calls == 96
    assert args.builtin_agent_wall_clock_seconds == 720
    assert args.builtin_agent_max_output_bytes == 1_048_576
    assert args.builtin_allowed_command == ['["uv","--version"]']
    assert runtime._parse_allowed_command_argv(args.builtin_allowed_command) == (
        ("uv", "--version"),
    )
    assert args.p2_runtime


def test_runtime_builtin_command_is_disabled_by_default() -> None:
    args = runtime.create_parser().parse_args(["--worker", "builtin"])

    assert runtime._parse_allowed_command_argv(args.builtin_allowed_command) == ()

    with raises(ValueError, match="argv sequences"):
        runtime._normalize_allowed_command_argv(("uv",))


def test_builtin_runtime_wires_timeout_and_reports_supervisor_failure(
    tmp_path,
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    calls = 0

    class _CrashingConcurrentRuntime:
        def __init__(self, **options: object) -> None:
            captured.update(options)

        async def recover_startup(self) -> tuple[object, ...]:
            return ()

        async def run_until_idle(self) -> tuple[object, ...]:
            nonlocal calls
            calls += 1
            raise RuntimeError("injected runtime loop crash")

        def connector_for(self, worker_endpoint_id: object) -> None:
            del worker_endpoint_id

        async def cancel_attempt(self, attempt_id: object) -> None:
            del attempt_id

        async def quiesce_run(self, run_id: object) -> None:
            del run_id

        def resume_run_scheduling(self, run_id: object) -> None:
            del run_id

    class _CapturedConnector:
        def __init__(self, **options: object) -> None:
            captured["agent_budget"] = options["budget"]

        async def close(self) -> None:
            return None

    monkeypatch.setattr(runtime, "ConcurrentRuntime", _CrashingConcurrentRuntime)
    monkeypatch.setattr(runtime, "BuiltinAgentConnector", _CapturedConnector)
    budget = AgentBudget(48, 96, 700, 2 * 1024 * 1024)
    app = create_local_app(
        tmp_path / "supervisor.sqlite3",
        tmp_path / "supervisor-artifacts",
        worker_kind="builtin",
        worker_workspace=tmp_path,
        builtin_model="scripted",
        worker_timeout_seconds=17,
        builtin_agent_budget=budget,
        p2_runtime=True,
    )

    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        health = client.get("/api/v1/runtime/health").json()["data"]
        while health["status"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.02)
            health = client.get("/api/v1/runtime/health").json()["data"]
        assert health == {
            "status": "failed",
            "loop_active": False,
            "consecutive_failures": 3,
            "restart_count": 2,
            "last_error": "RuntimeError: Runtime iteration failed",
        }
        assert client.get("/api/v1/events").status_code == 200

    policy = captured["policy"]
    assert isinstance(policy, ExecutionPolicy)
    assert policy.absolute_attempt_timeout == timedelta(seconds=17)
    assert captured["agent_budget"] == budget
    assert calls == 3


def test_runtime_composes_codex_planner_with_independent_timeout(
    tmp_path,
    monkeypatch: MonkeyPatch,
) -> None:
    args = runtime.create_parser().parse_args(
        [
            "--planner",
            "codex",
            "--planner-timeout-seconds",
            "19",
        ]
    )
    captured: dict[str, object] = {}

    class _Service:
        def recover_startup(self) -> None:
            captured["recovered"] = True

    def build_service(*_args: object, **options: object) -> _Service:
        captured.update(options)
        return _Service()

    def create_app(service: object, query_service: object):
        captured["service"] = service
        captured["query_service"] = query_service
        return runtime.FastAPI()

    monkeypatch.setattr(runtime, "build_service", build_service)
    monkeypatch.setattr(runtime, "create_app", create_app)

    app = runtime.create_local_app(
        tmp_path / "state.sqlite3",
        tmp_path / "artifacts",
        planner_kind=args.planner,
        planner_timeout_seconds=args.planner_timeout_seconds,
    )

    assert isinstance(app, runtime.FastAPI)
    assert captured["planner_kind"] == "codex"
    assert captured["planner_timeout_seconds"] == 19
    assert captured["recovered"] is True


def test_local_runtime_shares_commands_queries_and_event_log(tmp_path) -> None:
    app = create_local_app(
        tmp_path / "state.sqlite3",
        tmp_path / "artifacts",
    )
    client = TestClient(app)

    created = client.post(
        "/api/v1/projects",
        json={"idempotency_key": "runtime-project", "name": "Runtime"},
    )
    events = client.get("/api/v1/events")

    assert created.status_code == 201
    assert created.json()["data"]["created_at"].endswith("Z")
    assert events.status_code == 200
    assert events.json()["data"]["events"][0]["event"]["type"] == "ProjectCreated"
    assert str(app.url_path_for("event-stream")) == "/api/v1/events/stream"


def test_local_runtime_recovers_interrupted_attempt_before_serving(tmp_path) -> None:
    database_path = tmp_path / "restart.sqlite3"
    database = SQLiteDatabase(database_path)
    project = Project.create("restart", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "recover", goal_id=new_id(), created_at=NOW)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal.goal_id,
        ("evidence",),
        (check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = (
        PlanNode(
            new_id(),
            "running",
            "interrupt me",
            required_check_ids=(check_id,),
        )
        .mark_ready()
        .start()
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
        run.run_id,
        node.plan_node_id,
        1,
        attempt_id=new_id(),
        created_at=NOW,
    ).start(at=NOW)
    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        uow.states.put_goal(goal)
        uow.states.put_completion_contract(contract)
        uow.states.put_plan_revision(plan)
        uow.states.put_run(run)
        uow.states.put_attempt(attempt)
        uow.commit()

    app = create_local_app(database_path, tmp_path / "restart-artifacts")
    client = TestClient(app)
    response = client.get(f"/api/v1/runs/{run.run_id}")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == RunStatus.PAUSED.value
    with database.read_session() as session:
        recovered_attempt = session.states.get_attempt(attempt.attempt_id)
        recovered_plan = session.states.get_plan_revision(plan.plan_revision_id)
    assert recovered_attempt is not None
    assert recovered_attempt.status is AttemptStatus.INTERRUPTED
    assert recovered_plan is not None
    assert recovered_plan.nodes[0].status is PlanNodeStatus.FAILED
