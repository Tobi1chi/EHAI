from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from ehai import ID, new_id
from ehai.application.planner import NON_EMPTY_ARTIFACT_CRITERION
from ehai.application.ports import StoredEvent
from ehai.application.queries import (
    ArtifactView,
    AttemptView,
    BranchSelectionView,
    CheckpointSummary,
    CheckResultView,
    CheckRunView,
    CheckSpecView,
    EventPage,
    ExecutionTraceView,
    GateDecisionView,
    PlanGraphView,
    PlanNodeView,
    RunView,
)
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckKind, CheckRunStatus
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.planning import PlanNodeKind, PlanNodeStatus, PlanRevisionStatus
from ehai.interfaces.api import create_app
from ehai.interfaces.http_models import (
    ApprovePlanRequest,
    CancelRunRequest,
    CreateGoalRequest,
    CreateProjectRequest,
    ProposePlanRequest,
    ReplanPlanRequest,
    RunActionRequest,
    StartRunRequest,
)
from ehai.interfaces.runtime import create_local_app

SCHEMA_DIRECTORY = Path(__file__).parents[2] / "schemas" / "v1"
NOW = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
RUN_ID = new_id()
GOAL_ID = new_id()
PLAN_ID = new_id()
NODE_ID = new_id()
ATTEMPT_ID = new_id()
CHECK_ID = new_id()
CHECK_RUN_ID = new_id()
ARTIFACT_ID = new_id()


def _load_document(name: str) -> dict[str, object]:
    decoded = json.loads((SCHEMA_DIRECTORY / name).read_text(encoding="utf-8"))
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


SCHEMAS = {
    name: _load_document(name)
    for name in (
        "common.schema.json",
        "events.schema.json",
        "commands.schema.json",
        "queries.schema.json",
    )
}
REGISTRY = Registry().with_resources(
    (
        cast(str, schema["$id"]),
        Resource.from_contents(schema),
    )
    for schema in SCHEMAS.values()
)


def _validator(schema_name: str, definition: str) -> Draft202012Validator:
    schema_id = cast(str, SCHEMAS[schema_name]["$id"])
    return Draft202012Validator(
        {"$ref": f"{schema_id}#/$defs/{definition}"},
        registry=REGISTRY,
    )


RUN = RunView(
    RUN_ID,
    GOAL_ID,
    PLAN_ID,
    RunStatus.RUNNING,
    NOW,
    NOW,
    None,
    None,
)
PLAN_GRAPH = PlanGraphView(
    PLAN_ID,
    GOAL_ID,
    1,
    new_id(),
    1,
    NOW,
    PlanRevisionStatus.APPROVED,
    NOW,
    None,
    (
        PlanNodeView(
            NODE_ID,
            "work",
            "produce evidence",
            PlanNodeKind.WORK,
            (),
            (CHECK_ID,),
            PlanNodeStatus.RUNNING,
        ),
    ),
    (),
    (),
)
ARTIFACT = ArtifactView(
    ARTIFACT_ID,
    ArtifactKind.EVIDENCE,
    "evidence.txt",
    "text/plain",
    8,
    "0" * 64,
    NOW,
    RUN_ID,
    NODE_ID,
    ATTEMPT_ID,
)
CHECK_SPEC = CheckSpecView(CHECK_ID, "evidence", CheckKind.ARTIFACT, "must exist", True)
CHECK_RESULT = CheckResultView(
    CHECK_ID,
    CHECK_RUN_ID,
    RUN_ID,
    NODE_ID,
    ATTEMPT_ID,
    True,
    NOW,
    (ARTIFACT_ID,),
    "passed",
    None,
)
CHECK_RUN = CheckRunView(
    CHECK_RUN_ID,
    RUN_ID,
    NODE_ID,
    ATTEMPT_ID,
    CHECK_ID,
    CheckRunStatus.COMPLETED,
    NOW,
    NOW,
    NOW,
    CHECK_RESULT,
    None,
)
GATE_DECISION = GateDecisionView(
    new_id(),
    RUN_ID,
    NODE_ID,
    ATTEMPT_ID,
    True,
    NOW,
    (CHECK_ID,),
    (),
    (ARTIFACT_ID,),
    None,
)
CHECKPOINT = CheckpointSummary(
    new_id(),
    PLAN_ID,
    RUN_ID,
    1,
    GATE_DECISION,
    (BranchSelectionView(new_id(), new_id()),),
    (ARTIFACT_ID,),
    NOW,
)
STORED_EVENT = StoredEvent(
    1,
    Event(
        type=EventType.RUN_STARTED,
        run_id=RUN_ID,
        correlation_id=RUN_ID,
        payload={"run_id": RUN_ID},
        occurred_at=NOW,
    ),
)
TRACE = ExecutionTraceView(
    RUN,
    (
        AttemptView(
            ATTEMPT_ID,
            RUN_ID,
            NODE_ID,
            1,
            AttemptStatus.SUCCEEDED,
            (ARTIFACT_ID,),
            NOW,
            NOW,
            NOW,
            None,
        ),
    ),
    (ARTIFACT,),
    (CHECK_RUN,),
    (CHECKPOINT,),
    (STORED_EVENT,),
)
EVENT_PAGE = EventPage((STORED_EVENT,), None, STORED_EVENT.event.id, 1, False)


class _CommandService:
    def _respond(self, command: object) -> dict[str, str]:
        return {"command": type(command).__name__}

    create_project = _respond
    create_goal = _respond
    propose_plan = _respond
    replan_plan = _respond
    approve_plan = _respond
    start_run = _respond
    pause_run = _respond
    resume_run = _respond
    cancel_run = _respond


class _QueryService:
    def get_run(self, run_id: ID) -> RunView:
        del run_id
        return RUN

    def get_plan_graph(self, plan_revision_id: ID) -> PlanGraphView:
        del plan_revision_id
        return PLAN_GRAPH

    def get_execution_trace(self, run_id: ID) -> ExecutionTraceView:
        del run_id
        return TRACE

    def list_check_specs(self, plan_revision_id: ID) -> tuple[CheckSpecView, ...]:
        del plan_revision_id
        return (CHECK_SPEC,)

    def list_check_runs(self, run_id: ID) -> tuple[CheckRunView, ...]:
        del run_id
        return (CHECK_RUN,)

    def list_checkpoints(self, run_id: ID) -> tuple[CheckpointSummary, ...]:
        del run_id
        return (CHECKPOINT,)

    def list_artifacts(self, run_id: ID) -> tuple[ArtifactView, ...]:
        del run_id
        return (ARTIFACT,)

    def get_artifact(self, artifact_id: ID) -> ArtifactView:
        del artifact_id
        return ARTIFACT

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int = 100,
    ) -> EventPage:
        del after_event_id, limit
        return EVENT_PAGE


def _client() -> TestClient:
    return TestClient(create_app(_CommandService(), _QueryService()))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("definition", "model"),
    [
        ("CreateProjectRequest", CreateProjectRequest(idempotency_key="p", name="project")),
        (
            "CreateGoalRequest",
            CreateGoalRequest(idempotency_key="g", project_id=new_id(), objective="goal"),
        ),
        (
            "ProposePlanRequest",
            ProposePlanRequest(
                idempotency_key="plan",
                goal_id=new_id(),
                criteria=[NON_EMPTY_ARTIFACT_CRITERION],
            ),
        ),
        (
            "ReplanPlanRequest",
            ReplanPlanRequest(
                idempotency_key="replan",
                base_plan_revision_id=new_id(),
                criteria=[NON_EMPTY_ARTIFACT_CRITERION],
            ),
        ),
        (
            "ApprovePlanRequest",
            ApprovePlanRequest(
                idempotency_key="approve",
                plan_revision_id=new_id(),
                completion_contract_id=new_id(),
            ),
        ),
        (
            "StartRunRequest",
            StartRunRequest(idempotency_key="start", plan_revision_id=new_id()),
        ),
        ("PauseRunRequest", RunActionRequest(idempotency_key="pause")),
        ("ResumeRunRequest", RunActionRequest(idempotency_key="resume")),
        ("CancelRunRequest", CancelRunRequest(idempotency_key="cancel", reason="stop")),
    ],
)
def test_command_request_schemas_match_http_models(definition: str, model: object) -> None:
    payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    _validator("commands.schema.json", definition).validate(payload)


def test_real_testclient_responses_match_query_and_command_contracts() -> None:
    client = _client()
    command = client.post(
        "/api/v1/projects",
        json={"idempotency_key": "project", "name": "contract"},
    )
    assert command.status_code == 201
    _validator("commands.schema.json", "DataResponse").validate(command.json())

    cases = (
        (f"/api/v1/runs/{RUN_ID}", "RunResponse"),
        (f"/api/v1/plans/{PLAN_ID}", "PlanGraphResponse"),
        (f"/api/v1/runs/{RUN_ID}/trace", "ExecutionTraceResponse"),
        (f"/api/v1/plans/{PLAN_ID}/checks", "CheckSpecListResponse"),
        (f"/api/v1/runs/{RUN_ID}/checks", "CheckRunListResponse"),
        (f"/api/v1/runs/{RUN_ID}/checkpoints", "CheckpointListResponse"),
        (f"/api/v1/runs/{RUN_ID}/artifacts", "ArtifactListResponse"),
        (f"/api/v1/artifacts/{ARTIFACT_ID}", "ArtifactResponse"),
        ("/api/v1/events", "EventPageResponse"),
    )
    for path, definition in cases:
        response = client.get(path)
        assert response.status_code == 200, response.text
        _validator("queries.schema.json", definition).validate(response.json())


def test_real_command_responses_match_typed_contracts(tmp_path: Path) -> None:
    client = TestClient(create_local_app(tmp_path / "state.sqlite3", tmp_path / "artifacts"))
    project = client.post(
        "/api/v1/projects",
        json={"idempotency_key": "project", "name": "contract"},
    )
    assert project.status_code == 201
    _validator("commands.schema.json", "ProjectResponse").validate(project.json())
    project_id = project.json()["data"]["project_id"]

    goal = client.post(
        "/api/v1/goals",
        json={
            "idempotency_key": "goal",
            "project_id": project_id,
            "objective": "typed command responses",
        },
    )
    assert goal.status_code == 201
    _validator("commands.schema.json", "GoalResponse").validate(goal.json())
    goal_id = goal.json()["data"]["goal_id"]

    proposed = client.post(
        "/api/v1/plans/propose",
        json={
            "idempotency_key": "propose",
            "goal_id": goal_id,
            "criteria": [NON_EMPTY_ARTIFACT_CRITERION],
        },
    )
    assert proposed.status_code == 201
    _validator("commands.schema.json", "PlanGraphResponse").validate(proposed.json())
    plan = proposed.json()["data"]

    approved = client.post(
        "/api/v1/plans/approve",
        json={
            "idempotency_key": "approve",
            "plan_revision_id": plan["plan_revision_id"],
            "completion_contract_id": plan["completion_contract_id"],
        },
    )
    assert approved.status_code == 200
    _validator("commands.schema.json", "PlanGraphResponse").validate(approved.json())

    replanned = client.post(
        "/api/v1/plans/replan",
        json={
            "idempotency_key": "replan",
            "base_plan_revision_id": plan["plan_revision_id"],
            "criteria": [NON_EMPTY_ARTIFACT_CRITERION],
        },
    )
    assert replanned.status_code == 201
    _validator("commands.schema.json", "PlanGraphResponse").validate(replanned.json())
    revised_plan = replanned.json()["data"]
    assert revised_plan["version"] == 2
    assert revised_plan["supersedes_plan_revision_id"] == plan["plan_revision_id"]

    approved_replan = client.post(
        "/api/v1/plans/approve",
        json={
            "idempotency_key": "approve-replan",
            "plan_revision_id": revised_plan["plan_revision_id"],
            "completion_contract_id": revised_plan["completion_contract_id"],
        },
    )
    assert approved_replan.status_code == 200
    _validator("commands.schema.json", "PlanGraphResponse").validate(approved_replan.json())

    run = client.post(
        "/api/v1/runs/start",
        json={
            "idempotency_key": "start",
            "plan_revision_id": revised_plan["plan_revision_id"],
        },
    )
    assert run.status_code == 201
    _validator("commands.schema.json", "RunResponse").validate(run.json())


def test_event_enum_boundaries_and_query_view_separation() -> None:
    event_defs = cast(dict[str, object], SCHEMAS["events.schema.json"]["$defs"])
    event_type = cast(dict[str, object], event_defs["EventType"])
    assert set(cast(list[str], event_type["enum"])) == {item.value for item in EventType}

    client = _client()
    plan = client.get(f"/api/v1/plans/{PLAN_ID}").json()["data"]
    trace = client.get(f"/api/v1/runs/{RUN_ID}/trace").json()["data"]
    artifact = client.get(f"/api/v1/artifacts/{ARTIFACT_ID}").json()["data"]
    with pytest.raises(ValidationError):
        _validator("queries.schema.json", "PlanGraph").validate({**plan, "attempts": []})
    with pytest.raises(ValidationError):
        _validator("queries.schema.json", "ExecutionTrace").validate({**trace, "nodes": []})
    assert "relative_path" not in artifact
    with pytest.raises(ValidationError):
        _validator("queries.schema.json", "Artifact").validate(
            {**artifact, "relative_path": "private/path"}
        )


def test_audited_openapi_routes_match_fastapi_surface() -> None:
    audited = _load_document("http-api.openapi.json")
    actual = create_app(_CommandService(), _QueryService()).openapi()  # type: ignore[arg-type]
    methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}

    def operations(document: dict[str, object]) -> dict[tuple[str, str], dict[str, object]]:
        paths = cast(dict[str, dict[str, dict[str, object]]], document["paths"])
        return {
            (path, method): operation
            for path, path_item in paths.items()
            for method, operation in path_item.items()
            if method in methods
        }

    def resolve_component(
        document: dict[str, object],
        value: dict[str, object],
        component_kind: str,
    ) -> dict[str, object]:
        reference = value.get("$ref")
        if reference is None:
            return value
        name = cast(str, reference).rsplit("/", maxsplit=1)[-1]
        components = cast(dict[str, dict[str, dict[str, object]]], document["components"])
        return components[component_kind][name]

    def parameter_keys(
        document: dict[str, object],
        operation: dict[str, object],
    ) -> set[tuple[str, str, bool]]:
        parameters = cast(list[dict[str, object]], operation.get("parameters", []))
        resolved = (
            resolve_component(document, parameter, "parameters") for parameter in parameters
        )
        return {
            (
                cast(str, parameter["name"]),
                cast(str, parameter["in"]),
                cast(bool, parameter.get("required", False)),
            )
            for parameter in resolved
        }

    audited_operations = operations(audited)
    actual_document = cast(dict[str, object], actual)
    actual_operations = operations(actual_document)
    assert set(audited_operations) == set(actual_operations)
    for key, audited_operation in audited_operations.items():
        actual_operation = actual_operations[key]
        assert actual_operation["operationId"] == audited_operation["operationId"]
        assert parameter_keys(actual_document, actual_operation) == parameter_keys(
            audited,
            audited_operation,
        )
        audited_responses = cast(dict[str, dict[str, object]], audited_operation["responses"])
        actual_responses = cast(dict[str, dict[str, object]], actual_operation["responses"])
        assert set(actual_responses) == set(audited_responses)
        for status, audited_response_ref in audited_responses.items():
            audited_response = resolve_component(
                audited,
                audited_response_ref,
                "responses",
            )
            actual_response = actual_responses[status]
            audited_content = cast(dict[str, object], audited_response.get("content", {}))
            actual_content = cast(dict[str, object], actual_response.get("content", {}))
            assert set(actual_content) == set(audited_content)
            if status.startswith("2"):
                assert actual_content == audited_content
            elif actual_content:
                schema = cast(
                    dict[str, str],
                    cast(dict[str, object], actual_content["application/json"])["schema"],
                )
                assert schema["$ref"].endswith("/ErrorResponse")


def test_request_schema_constraints_match_pydantic_normalization() -> None:
    uppercase_id = new_id().upper()
    _validator("common.schema.json", "IdInput").validate(uppercase_id)
    assert (
        CreateGoalRequest(
            idempotency_key=" goal ",
            project_id=uppercase_id,
            objective=" objective ",
        ).project_id
        == uppercase_id.lower()
    )

    braced_id = f"{{{new_id()}}}"
    with pytest.raises(ValidationError):
        _validator("common.schema.json", "IdInput").validate(braced_id)
    with pytest.raises(ValueError):
        CreateGoalRequest(
            idempotency_key="goal",
            project_id=braced_id,
            objective="objective",
        )

    with pytest.raises(ValidationError):
        _validator("common.schema.json", "Id").validate(uppercase_id)
    with pytest.raises(ValidationError):
        _validator("common.schema.json", "IdempotencyKey").validate("   ")
    with pytest.raises(ValidationError):
        _validator("commands.schema.json", "ProposePlanRequest").validate(
            {
                "idempotency_key": "plan",
                "goal_id": new_id(),
                "criteria": ["   "],
            }
        )
    with pytest.raises(ValueError):
        ProposePlanRequest(
            idempotency_key="plan",
            goal_id=new_id(),
            criteria=["   "],  # type: ignore[list-item]
        )


@pytest.mark.parametrize("schema", SCHEMAS.values())
def test_json_schemas_are_valid_draft_2020_12(schema: dict[str, object]) -> None:
    Draft202012Validator.check_schema(schema)
