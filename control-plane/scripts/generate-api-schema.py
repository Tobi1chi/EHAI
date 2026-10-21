"""Refresh public execution configuration and OpenAPI from production assembly.

Run from the repository root with uv run control-plane/scripts/generate-api-schema.py.
No lifespan, model call, existing database or user configuration is accessed.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter

from ehai.application.plan_imports import plan_import_error_schema, plan_import_schema
from ehai.application.thread_queries import ThreadListView, ThreadSummary, ThreadTimelineView
from ehai.domain.events import EventType
from ehai.infrastructure.workspace_supervisor import WorkspaceSupervisor
from ehai.interfaces.event_consumers_api import (
    AcknowledgeEventConsumerRequest,
    ReadEventConsumerBatchRequest,
    RegisterEventConsumerRequest,
    event_consumer_schema_definitions,
)
from ehai.interfaces.http_models import (
    ApprovePlanRequest,
    ExecutionConfigRequest,
    ImportPlanRequest,
    IntegrateRunRequest,
    StartRunRequest,
    SuspendAttemptRequest,
)
from ehai.interfaces.notes_api import AddNoteMessageRequest, CreateNoteRequest, DecideNoteRequest
from ehai.interfaces.runtime import create_local_app
from ehai.interfaces.workspace_api import create_workspace_app

schema_root = Path(__file__).resolve().parents[2] / "schemas" / "v1"
commands_path = schema_root / "commands.schema.json"
commands = json.loads(commands_path.read_text(encoding="utf-8"))
for request_type in (
    RegisterEventConsumerRequest,
    ReadEventConsumerBatchRequest,
    AcknowledgeEventConsumerRequest,
):
    request_schema = request_type.model_json_schema()
    commands["$defs"].update(request_schema.pop("$defs", {}))
    commands["$defs"][request_type.__name__] = request_schema
notes_path = schema_root / "notes.schema.json"
notes = json.loads(notes_path.read_text(encoding="utf-8"))
for request_type in (CreateNoteRequest, AddNoteMessageRequest, DecideNoteRequest):
    notes["$defs"][request_type.__name__] = request_type.model_json_schema()
notes_path.write_text(json.dumps(notes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
queries_path = schema_root / "queries.schema.json"
queries = json.loads(queries_path.read_text(encoding="utf-8"))
queries["$defs"]["CodeIntegrationListResponse"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["data"],
    "properties": {
        "data": {"type": "array", "items": {"$ref": "#/$defs/CodeIntegration"}},
    },
}
queries["$defs"]["CodeIntegrationDiff"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["run_id", "integration_id", "content", "truncated"],
    "properties": {
        "run_id": {"$ref": "common.schema.json#/$defs/Id"},
        "integration_id": {"$ref": "common.schema.json#/$defs/Id"},
        "content": {
            "type": "string",
            "description": "Unified diff, UTF-8 with replacement for invalid bytes, "
            "from at most the first 1048576 source bytes (1 MiB).",
        },
        "truncated": {
            "type": "boolean",
            "description": "True if the retained patch exceeds 1048576 bytes; "
            "content is then a prefix, not a complete patch.",
        },
    },
}
queries["$defs"]["CodeIntegrationDiffResponse"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["data"],
    "properties": {"data": {"$ref": "#/$defs/CodeIntegrationDiff"}},
}


def thread_reference(value: object) -> object:
    if isinstance(value, list):
        return [thread_reference(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: thread_reference(item) for key, item in value.items()}
    if result.get("$ref") == "#/$defs/JsonValue":
        result["$ref"] = "commands.schema.json#/$defs/JsonValue"
    return result


for view_type, response_name in (
    (ThreadListView, "ThreadListResponse"),
    (ThreadSummary, "ThreadResponse"),
    (ThreadTimelineView, "ThreadTimelineResponse"),
):
    view_schema = TypeAdapter(view_type).json_schema()
    definitions = view_schema.pop("$defs", {})
    definitions.pop("JsonValue", None)
    definitions.pop("JsonScalar", None)
    definitions.pop("RunStatus", None)
    queries["$defs"].update(thread_reference(definitions))
    queries["$defs"][view_type.__name__] = thread_reference(view_schema)
    queries["$defs"][response_name] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["data"],
        "properties": {"data": {"$ref": "#/$defs/" + view_type.__name__}},
    }
queries["$defs"].update(event_consumer_schema_definitions())
queries["$defs"]["PlannerCapacity"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["capacity", "in_use", "available"],
    "properties": {
        "capacity": {"type": "integer", "minimum": 1},
        "in_use": {"type": "integer", "minimum": 0},
        "available": {"type": "integer", "minimum": 0},
    },
}
queries["$defs"]["PlannerCapacityResponse"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["data"],
    "properties": {"data": {"$ref": "#/$defs/PlannerCapacity"}},
}
queries["$defs"]["InboxKind"]["enum"] = [
    "intervention",
    "human_check",
    "note",
    "workflow_confirmation",
]
queries["$defs"]["InboxAction"]["properties"]["operation"]["enum"] = [
    "reply-intervention",
    "decide-human-check",
    "add-note-message",
    "decide-note",
    "decide-workflow",
]
for name in ("run_id", "plan_node_id", "attempt_id", "goal_id", "workflow_run_id"):
    queries["$defs"]["InboxOwner"]["properties"][name] = {"$ref": "#/$defs/NullableId"}
queries["$defs"]["InboxOwner"]["properties"]["run_status"] = {
    "anyOf": [{"$ref": "#/$defs/RunStatus"}, {"type": "null"}]
}
queries["$defs"]["InboxOwner"]["properties"]["goal_objective"] = {"type": ["string", "null"]}
if "workflow_run_id" not in queries["$defs"]["InboxOwner"]["required"]:
    queries["$defs"]["InboxOwner"]["required"].append("workflow_run_id")
events_path = schema_root / "events.schema.json"
events = json.loads(events_path.read_text(encoding="utf-8"))
events["$defs"]["EventType"]["enum"] = [event.value for event in EventType]
events_path.write_text(json.dumps(events, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
queries_path.write_text(json.dumps(queries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
commands["$defs"]["IntegrateRunRequest"] = IntegrateRunRequest.model_json_schema()
for request_type in (ApprovePlanRequest, StartRunRequest):
    request_schema = request_type.model_json_schema()
    commands["$defs"].update(request_schema.pop("$defs", {}))
    commands["$defs"][request_type.__name__] = request_schema
commands["$defs"]["ImportPlanRequest"] = ImportPlanRequest.model_json_schema()
commands["$defs"]["ImportPlanErrorResponse"] = plan_import_error_schema()
(schema_root / "plan-import.schema.json").write_text(
    json.dumps(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", **plan_import_schema()},
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
execution = ExecutionConfigRequest.model_json_schema()
commands["$defs"].update(execution.pop("$defs", {}))
commands["$defs"]["ExecutionConfigRequest"] = execution
suspension = SuspendAttemptRequest.model_json_schema()
commands["$defs"].update(suspension.pop("$defs", {}))
commands["$defs"]["SuspendAttemptRequest"] = suspension
commands["$defs"]["SuspendAttemptResponse"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["data"],
    "properties": {"data": {"$ref": "#/$defs/AttemptSuspensionResult"}},
}
commands["$defs"]["AttemptSuspensionResult"] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "attempt_id",
        "run_id",
        "review_id",
        "status",
        "attempt_status",
        "node_status",
        "intervention",
    ],
    "properties": {
        "attempt_id": {"type": "string", "format": "uuid"},
        "run_id": {"type": "string", "format": "uuid"},
        "review_id": {"type": "string", "format": "uuid"},
        "status": {"type": "string", "enum": ["requested", "suspended", "not_suspended"]},
        "attempt_status": {"$ref": "queries.schema.json#/$defs/AttemptStatus"},
        "node_status": {
            "anyOf": [{"$ref": "queries.schema.json#/$defs/PlanNodeStatus"}, {"type": "null"}]
        },
        "intervention": {
            "anyOf": [{"$ref": "queries.schema.json#/$defs/Intervention"}, {"type": "null"}]
        },
    },
}
commands_path.write_text(
    json.dumps(commands, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)

with TemporaryDirectory(prefix="ehai-openapi-") as temporary:
    root = Path(temporary)
    app = create_local_app(root / "state.sqlite", root / "artifacts", runtime_autostart=False)
    document = app.openapi()
(schema_root / "http-api.openapi.json").write_text(
    json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)

# The manager has its own contract. Its core proxy is represented by the existing
# core schema, so clients do not infer a second set of execution state rules.
with TemporaryDirectory(prefix="ehai-manager-openapi-") as temporary:
    manager = create_workspace_app(WorkspaceSupervisor(Path(temporary)))
    manager_document = manager.openapi()
(schema_root / "workspace-manager.openapi.json").write_text(
    json.dumps(manager_document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)


def manager_reference(value: object) -> object:
    if isinstance(value, list):
        return [manager_reference(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: manager_reference(item) for key, item in value.items()}
    reference = result.get("$ref")
    if reference in {
        "#/components/schemas/JsonValue-Input",
        "#/components/schemas/JsonValue-Output",
    }:
        # FastAPI splits recursive input/output aliases; the public JSON contract is shared.
        result["$ref"] = "commands.schema.json#/$defs/JsonValue"
    elif isinstance(reference, str) and reference.startswith("#/components/schemas/"):
        result["$ref"] = "#/$defs/" + reference.removeprefix("#/components/schemas/").replace(
            "-", "_"
        )
    return result


existing_definitions = {}
routing_definitions = {
    name.replace("-", "_"): manager_reference(definition)
    for name, definition in document["components"]["schemas"].items()
    if "Routing" in name
}
(schema_root / "routing.schema.json").write_text(
    json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.ehai.local/v1/routing.schema.json",
            "$defs": routing_definitions,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
connector_definitions = {
    name.replace("-", "_"): manager_reference(definition)
    for name, definition in document["components"]["schemas"].items()
    if name.startswith("Connector")
    or name
    in {
        "RegisterConnectorRequest",
        "InvokeConnectorRequest",
        "ClaimConnectorCallRequest",
        "ReportConnectorResultRequest",
        "ReconcileConnectorCallRequest",
        "SubmitConnectorEventRequest",
    }
}
(schema_root / "connectors.schema.json").write_text(
    json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.ehai.local/v1/connectors.schema.json",
            "$defs": connector_definitions,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
# Collect the concrete workflow contracts from the owning production router.
workflow_names = {
    "WorkflowExecutionView",
    "WorkflowExecutionRecord",
    "WorkflowContractRef",
    "WorkflowStepExecution",
    "WorkflowInvocationRecord",
    "WorkflowWaitRecord",
    "WorkflowCompatibilityStatus",
    "CreateRoutineRequest",
    "DecideWorkflowRequest",
    "LifeRoutine",
    "LifeTask",
    "LifeTaskInput",
    "LifeReview",
    "RoutineSchedulerStatus",
    "StartWorkflowRequest",
    "UpdateLifeTaskRequest",
    "UpdateRoutineRequest",
    "WorkflowDefinition",
    "WorkflowRun",
    "WorkflowDecision",
}
workflow_definitions = {
    name: manager_reference(definition)
    for name, definition in document["components"]["schemas"].items()
    if name in workflow_names or name.startswith("WorkflowResponse_")
}
(schema_root / "workflows.schema.json").write_text(
    json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.ehai.local/v1/workflows.schema.json",
            "$defs": workflow_definitions,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
for name in (
    "common.schema.json",
    "events.schema.json",
    "queries.schema.json",
    "commands.schema.json",
    "notes.schema.json",
    "project-configuration.schema.json",
    "workflows.schema.json",
    "connectors.schema.json",
    "routing.schema.json",
):
    existing = json.loads((schema_root / name).read_text(encoding="utf-8"))
    for definition in existing.get("$defs", {}):
        existing_definitions.setdefault(definition, name)
manager_definitions = {}
for name, definition in manager_document.get("components", {}).get("schemas", {}).items():
    manager_definitions[name] = (
        {"$ref": existing_definitions[name] + "#/$defs/" + name}
        if name in existing_definitions
        else manager_reference(definition)
    )
(schema_root / "workspace-manager.schema.json").write_text(
    json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://schemas.ehai.local/v1/workspace-manager.schema.json",
            "$defs": manager_definitions,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
