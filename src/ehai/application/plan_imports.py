"""External plan definitions share the Planner's graph and proposal boundaries."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from jsonschema import Draft202012Validator

from ehai import JsonValue
from ehai.application.plan_graph_tools import MAX_PLAN_OPERATIONS, PlanGraphToolRuntime
from ehai.application.planner import ExplorationBudget, PlanTemplate

_SECTIONS = {
    "nodes": ("add_plan_node", "key"),
    "branches": ("set_plan_branch", "branch_key"),
    "edges": ("add_plan_edge", None),
    "phases": ("set_plan_phase", "phase_key"),
    "node_gates": ("set_node_gate", "node_key"),
}


class PlanImportError(ValueError):
    """Structured, non-persisting rejection of an external plan definition."""

    def __init__(self, issues: list[JsonValue]) -> None:
        self.issues = issues
        super().__init__("External plan validation failed")


def plan_import_error_schema() -> dict[str, Any]:
    """HTTP errors may carry graph diagnostics in addition to the usual code/message."""
    text = {"type": "string"}
    issue = {
        "type": "object",
        "additionalProperties": False,
        "required": ["code", "location", "message"],
        "properties": {
            "code": text,
            "location": text,
            "message": text,
            "related_keys": {"type": "array", "items": text},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "message"],
                "properties": {
                    "code": text,
                    "message": text,
                    "issues": {"type": "array", "items": issue},
                },
            }
        },
    }


def plan_import_schema() -> dict[str, Any]:
    """Generate the public definition from the actual model-facing graph ToolSet."""
    graph = PlanGraphToolRuntime(ExplorationBudget(max_attempts=MAX_PLAN_OPERATIONS))
    tools = {item.name: item.input_schema for item in graph.tool_definitions()}
    properties: dict[str, Any] = {
        "schema_version": {"type": "integer", "const": 1},
        "design_document": {"type": "string", "minLength": 1, "maxLength": 60000},
        "final_gate": tools["set_final_gate"],
    }
    for section, (tool, _) in _SECTIONS.items():
        schema = tools[tool]
        if section in {"branches", "phases"}:
            schema = {
                **schema,
                "properties": {
                    key: value
                    for key, value in cast(dict[str, JsonValue], schema["properties"]).items()
                    if key != "remove"
                },
                "required": [key for key in cast(list[str], schema["required"]) if key != "remove"],
            }
            # Nullable placeholders are only meaningful for the tool's remove mode.
            for field_schema in cast(dict[str, dict[str, Any]], schema["properties"]).values():
                kinds = field_schema.get("type")
                if isinstance(kinds, list):
                    non_null = [kind for kind in kinds if kind != "null"]
                    field_schema["type"] = non_null[0] if len(non_null) == 1 else non_null
        properties[section] = {"type": "array", "items": schema, "maxItems": MAX_PLAN_OPERATIONS}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def import_plan_template(document: dict[str, JsonValue]) -> PlanTemplate:
    """Validate an immutable snapshot; never import statuses, approvals or retained IDs."""
    errors = list(Draft202012Validator(plan_import_schema()).iter_errors(document))
    if errors:
        raise PlanImportError(
            [
                {
                    "code": "INVALID_PLAN_DOCUMENT",
                    "location": ".".join(map(str, error.absolute_path)),
                    "message": error.message[:1000],
                }
                for error in errors[:20]
            ]
        )
    design = cast(str, document["design_document"])
    if not design.strip():
        raise PlanImportError(
            [
                {
                    "code": "DESIGN_REQUIRED",
                    "location": "design_document",
                    "message": "A reviewable design document is required",
                }
            ]
        )
    graph = PlanGraphToolRuntime(
        ExplorationBudget(max_attempts=MAX_PLAN_OPERATIONS),
        require_final_gate=True,
        planner_event_types=("plan.external.imported",),
    )

    def apply(tool: str, arguments: dict[str, JsonValue]) -> None:
        result = graph.execute(tool, arguments)
        if result.get("accepted") is not True:
            raise PlanImportError(cast(list[JsonValue], result["issues"]))

    for section, (tool, identity) in _SECTIONS.items():
        seen: set[object] = set()
        for item in cast(list[dict[str, JsonValue]], document[section]):
            key = (
                item[identity]
                if identity
                else tuple(item[field] for field in ("source", "target", "edge_type", "branch_key"))
            )
            if key in seen:
                raise PlanImportError(
                    [
                        {
                            "code": "DUPLICATE_DEFINITION",
                            "location": section,
                            "message": "A snapshot cannot define the same item twice",
                        }
                    ]
                )
            seen.add(key)
            apply(tool, {**item, "remove": False} if section in {"branches", "phases"} else item)
    apply("set_final_gate", cast(dict[str, JsonValue], document["final_gate"]))
    apply("finish_plan", {})
    return replace(graph.build_template(), design_document=design.strip())
