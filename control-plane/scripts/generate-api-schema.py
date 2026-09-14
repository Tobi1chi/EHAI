"""Refresh public execution configuration and OpenAPI from production assembly.

Run from the repository root with uv run control-plane/scripts/generate-api-schema.py.
No lifespan, model call, existing database or user configuration is accessed.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ehai.interfaces.http_models import ExecutionConfigRequest
from ehai.interfaces.runtime import create_local_app

schema_root = Path(__file__).resolve().parents[2] / "schemas" / "v1"
commands_path = schema_root / "commands.schema.json"
commands = json.loads(commands_path.read_text(encoding="utf-8"))
execution = ExecutionConfigRequest.model_json_schema()
commands["$defs"].update(execution.pop("$defs", {}))
commands["$defs"]["ExecutionConfigRequest"] = execution
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
