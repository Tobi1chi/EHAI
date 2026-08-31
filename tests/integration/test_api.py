from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ehai import ID, new_id
from ehai.application.checks import CheckRunner
from ehai.application.commands import (
    ApprovePlan,
    CancelRun,
    CreateGoal,
    CreateProject,
    PauseRun,
    ProposePlan,
    ResumeRun,
    StartRun,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION, DeterministicPlanner
from ehai.application.queries import QueryService
from ehai.application.run_control import RunController
from ehai.application.service import EntityNotFoundError, ExecutionService
from ehai.application.workers import CandidateArtifact, WorkerRequest, WorkerResult
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind
from ehai.domain.events import Event, EventType
from ehai.domain.execution import RunStatus
from ehai.domain.goal import GoalStatus
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.interfaces.api import create_app


class _CommandService:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def _record(self, command: object) -> dict[str, str]:
        self.calls.append(command)
        return {"command": type(command).__name__}

    def create_project(self, command: CreateProject) -> object:
        return self._record(command)

    def create_goal(self, command: CreateGoal) -> object:
        return self._record(command)

    def propose_plan(self, command: ProposePlan) -> object:
        return self._record(command)

    def approve_plan(self, command: ApprovePlan) -> object:
        return self._record(command)

    def start_run(self, command: StartRun) -> object:
        return self._record(command)

    def pause_run(self, command: PauseRun) -> object:
        return self._record(command)

    def resume_run(self, command: ResumeRun) -> object:
        return self._record(command)

    def cancel_run(self, command: CancelRun) -> object:
        return self._record(command)


class _QueryService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.missing_run = False

    def _record(self, *call: object) -> dict[str, object]:
        self.calls.append(call)
        return {"call": list(call)}

    def get_run(self, run_id: ID) -> object:
        if self.missing_run:
            raise EntityNotFoundError(f"Run {run_id} does not exist")
        return self._record("get_run", run_id)

    def get_plan_graph(self, plan_revision_id: ID) -> object:
        return self._record("get_plan_graph", plan_revision_id)

    def get_execution_trace(self, run_id: ID) -> object:
        return self._record("get_execution_trace", run_id)

    def list_check_specs(self, plan_revision_id: ID) -> object:
        return self._record("list_check_specs", plan_revision_id)

    def list_check_runs(self, run_id: ID) -> object:
        return self._record("list_check_runs", run_id)

    def list_checkpoints(self, run_id: ID) -> object:
        return self._record("list_checkpoints", run_id)

    def list_artifacts(self, run_id: ID) -> object:
        return self._record("list_artifacts", run_id)

    def get_artifact(self, artifact_id: ID) -> object:
        return self._record("get_artifact", artifact_id)

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int = 100,
    ) -> object:
        return self._record("list_events", after_event_id, limit)


class _LogOnlyWorker:
    def execute(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            artifacts=(
                CandidateArtifact(
                    ArtifactKind.LOG,
                    "worker.log",
                    "text/plain",
                    b"diagnostic only",
                ),
            ),
            summary="no candidate evidence",
        )

    def cancel(self, attempt_id: ID) -> None:
        del attempt_id


def _client() -> tuple[TestClient, _CommandService, _QueryService]:
    commands = _CommandService()
    queries = _QueryService()
    return TestClient(create_app(commands, queries)), commands, queries  # type: ignore[arg-type]


def test_all_command_routes_convert_models_and_preserve_idempotency() -> None:
    client, service, _ = _client()
    project_id = new_id()
    goal_id = new_id()
    plan_id = new_id()
    contract_id = new_id()
    run_id = new_id()
    requests = (
        ("/api/v1/projects", {"idempotency_key": "p-1", "name": "Project"}, 201),
        (
            "/api/v1/goals",
            {
                "idempotency_key": "g-1",
                "project_id": project_id,
                "objective": "Goal",
            },
            201,
        ),
        (
            "/api/v1/plans/propose",
            {"idempotency_key": "plan-1", "goal_id": goal_id, "criteria": ["done"]},
            201,
        ),
        (
            "/api/v1/plans/approve",
            {
                "idempotency_key": "approve-1",
                "plan_revision_id": plan_id,
                "completion_contract_id": contract_id,
            },
            200,
        ),
        (
            "/api/v1/runs/start",
            {"idempotency_key": "start-1", "plan_revision_id": plan_id},
            201,
        ),
        (f"/api/v1/runs/{run_id}/pause", {"idempotency_key": "pause-1"}, 200),
        (f"/api/v1/runs/{run_id}/resume", {"idempotency_key": "resume-1"}, 200),
        (
            f"/api/v1/runs/{run_id}/cancel",
            {"idempotency_key": "cancel-1", "reason": "stop"},
            200,
        ),
    )

    for path, body, expected_status in requests:
        response = client.post(path, json=body)
        assert response.status_code == expected_status, response.text
        assert response.json()["data"]["command"]

    assert [type(command) for command in service.calls] == [
        CreateProject,
        CreateGoal,
        ProposePlan,
        ApprovePlan,
        StartRun,
        PauseRun,
        ResumeRun,
        CancelRun,
    ]
    assert [command.idempotency_key for command in service.calls] == [
        "p-1",
        "g-1",
        "plan-1",
        "approve-1",
        "start-1",
        "pause-1",
        "resume-1",
        "cancel-1",
    ]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/projects", {"name": "Project"}),
        (
            "/api/v1/goals",
            {"project_id": new_id(), "objective": "Goal"},
        ),
        (
            "/api/v1/plans/propose",
            {"goal_id": new_id(), "criteria": ["done"]},
        ),
        (
            "/api/v1/plans/approve",
            {
                "plan_revision_id": new_id(),
                "completion_contract_id": new_id(),
            },
        ),
        ("/api/v1/runs/start", {"plan_revision_id": new_id()}),
        (f"/api/v1/runs/{new_id()}/pause", {}),
        (f"/api/v1/runs/{new_id()}/resume", {}),
        (f"/api/v1/runs/{new_id()}/cancel", {"reason": "stop"}),
    ],
)
def test_all_write_routes_require_idempotency_key(
    path: str,
    body: dict[str, object],
) -> None:
    client, service, _ = _client()

    response = client.post(path, json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.calls == []


def test_blank_request_text_is_rejected_as_invalid_input() -> None:
    client, service, _ = _client()

    response = client.post(
        "/api/v1/projects",
        json={"idempotency_key": "project", "name": "   "},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.calls == []


def test_query_routes_delegate_cursor_and_scope_without_domain_logic() -> None:
    client, _, queries = _client()
    run_id = new_id()
    plan_id = new_id()
    artifact_id = new_id()
    event_id = new_id()
    paths = (
        f"/api/v1/runs/{run_id}",
        f"/api/v1/plans/{plan_id}",
        f"/api/v1/runs/{run_id}/trace",
        f"/api/v1/plans/{plan_id}/checks",
        f"/api/v1/runs/{run_id}/checks",
        f"/api/v1/runs/{run_id}/checkpoints",
        f"/api/v1/runs/{run_id}/artifacts",
        f"/api/v1/artifacts/{artifact_id}",
        f"/api/v1/events?after_event_id={event_id}&limit=25",
    )

    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["call"]

    assert queries.calls == [
        ("get_run", run_id),
        ("get_plan_graph", plan_id),
        ("get_execution_trace", run_id),
        ("list_check_specs", plan_id),
        ("list_check_runs", run_id),
        ("list_checkpoints", run_id),
        ("list_artifacts", run_id),
        ("get_artifact", artifact_id),
        ("list_events", event_id, 25),
    ]


def test_errors_use_one_json_envelope_and_status_mapping() -> None:
    client, _, queries = _client()
    queries.missing_run = True

    response = client.get(f"/api/v1/runs/{new_id()}")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "not_found"
    assert "does not exist" in payload["error"]["message"]


def test_event_query_uses_canonical_envelope_and_utc_encoding(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "event-query.sqlite3")
    event = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=new_id(),
        payload={"project_id": new_id()},
        occurred_at=datetime(2026, 8, 31, 22, 30, tzinfo=UTC),
    )
    with database.unit_of_work() as uow:
        uow.events.append(event)
        uow.commit()
    client = TestClient(
        create_app(
            _CommandService(),  # type: ignore[arg-type]
            QueryService(read_session_factory=database.read_session),
        )
    )

    response = client.get("/api/v1/events")

    assert response.status_code == 200
    stored = response.json()["data"]["events"][0]
    assert stored == {"offset": 1, "event": event.to_dict()}
    assert stored["event"]["occurred_at"] == "2026-08-31T22:30:00.000000Z"
    assert "_payload_json" not in stored["event"]

    unknown = client.get(f"/api/v1/events/stream?after_event_id={new_id()}")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "unknown_event_cursor"

    unknown_page = client.get(f"/api/v1/events?after_event_id={new_id()}")
    assert unknown_page.status_code == 404
    assert unknown_page.json()["error"]["code"] == "unknown_event_cursor"


def _actual_service(tmp_path: Path) -> tuple[ExecutionService, SQLiteDatabase]:
    database = SQLiteDatabase(tmp_path / "api.sqlite3")
    worker = _LogOnlyWorker()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    check_runner = CheckRunner(
        {
            CheckKind.ARTIFACT: ArtifactCheckAdapter(
                artifact_store,
                {},
                default_rule=ArtifactCheckRule(),
            )
        }
    )
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifact_store,
        check_runner=check_runner,
        workspace=tmp_path,
    )
    return (
        ExecutionService(
            uow_factory=database.unit_of_work,
            planner=DeterministicPlanner(),
            orchestrator=orchestrator,
            run_controller=RunController(database.unit_of_work, worker),
        ),
        database,
    )


def test_api_cannot_start_unapproved_plan_or_bypass_final_gate(tmp_path: Path) -> None:
    service, database = _actual_service(tmp_path)
    queries = _QueryService()
    client = TestClient(create_app(service, queries))

    project = client.post(
        "/api/v1/projects",
        json={"idempotency_key": "project", "name": "API"},
    ).json()["data"]
    goal = client.post(
        "/api/v1/goals",
        json={
            "idempotency_key": "goal",
            "project_id": project["project_id"],
            "objective": "must pass Gate",
        },
    ).json()["data"]
    proposed = client.post(
        "/api/v1/plans/propose",
        json={
            "idempotency_key": "propose",
            "goal_id": goal["goal_id"],
            "criteria": [NON_EMPTY_ARTIFACT_CRITERION],
        },
    ).json()["data"]

    rejected = client.post(
        "/api/v1/runs/start",
        json={
            "idempotency_key": "unapproved-run",
            "plan_revision_id": proposed["plan_revision_id"],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "state_conflict"

    approved = client.post(
        "/api/v1/plans/approve",
        json={
            "idempotency_key": "approve",
            "plan_revision_id": proposed["plan_revision_id"],
            "completion_contract_id": proposed["completion_contract_id"],
        },
    )
    assert approved.status_code == 200
    gate_rejected = client.post(
        "/api/v1/runs/start",
        json={
            "idempotency_key": "approved-run",
            "plan_revision_id": proposed["plan_revision_id"],
        },
    )
    assert gate_rejected.status_code == 409

    with database.unit_of_work() as uow:
        stored_goal = uow.states.get_goal(goal["goal_id"])
        runs = uow.states.list_runs(goal["goal_id"])
    assert stored_goal is not None and stored_goal.status is GoalStatus.OPEN
    assert len(runs) == 1 and runs[0].status is RunStatus.FAILED

    invalid_pause = client.post(
        f"/api/v1/runs/{runs[0].run_id}/pause",
        json={"idempotency_key": "pause-failed"},
    )
    assert invalid_pause.status_code == 409
    assert invalid_pause.json()["error"]["code"] == "state_conflict"
