"""Refresh public execution configuration and OpenAPI from production assembly.

Run from the repository root with uv run control-plane/scripts/generate-api-schema.py.
No lifespan, model call, existing database or user configuration is accessed.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ehai.application.plan_imports import plan_import_error_schema, plan_import_schema
from ehai.interfaces.http_models import (
    ExecutionConfigRequest,
    ImportPlanRequest,
    IntegrateRunRequest,
    SuspendAttemptRequest,
)
from ehai.interfaces.runtime import create_local_app

schema_root = Path(__file__).resolve().parents[2] / "schemas" / "v1"
commands_path = schema_root / "commands.schema.json"
commands = json.loads(commands_path.read_text(encoding="utf-8"))
commands["$defs"]["IntegrateRunRequest"] = IntegrateRunRequest.model_json_schema()
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
