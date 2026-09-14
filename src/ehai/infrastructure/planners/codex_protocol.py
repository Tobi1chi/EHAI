"""Strict prompt and structured-output protocol for the Codex Planner."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ehai import JsonValue, json_dumps
from ehai.application.planner import (
    MAX_REPLAN_ATTEMPT_SUMMARIES,
    MAX_REPLAN_CHECK_SUMMARIES,
    MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS,
    MAX_REPLAN_INTERVENTION_SUMMARIES,
    ExplorationBudget,
    ReplanContext,
    check_spec_document,
)
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.goal import Goal
from ehai.domain.planning import PlanNodeKind, PlanNodeStatus, PlanRevision

_OUTPUT_KEYS = frozenset({"summary", "fork", "branches", "evaluator", "merge"})
_STEP_KEYS = frozenset({"title", "instruction"})
_BRANCH_KEYS = frozenset({"label", "title", "instruction"})

_ID_SCHEMA: dict[str, JsonValue] = {"type": "string", "format": "uuid"}
_PLAN_NODE_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "plan_node_id",
        "title",
        "instruction",
        "kind",
        "required_dependency_ids",
        "required_check_ids",
        "status",
        "required_capabilities",
        "session_policy",
    ],
    "properties": {
        "plan_node_id": _ID_SCHEMA,
        "title": {"type": "string", "minLength": 1},
        "instruction": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": [item.value for item in PlanNodeKind]},
        "status": {"type": "string", "enum": [item.value for item in PlanNodeStatus]},
        "required_capabilities": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
        "session_policy": {"type": "string", "enum": ["new", "reuse", "fork"]},
        "required_dependency_ids": {"type": "array", "items": _ID_SCHEMA, "uniqueItems": True},
        "required_check_ids": {"type": "array", "items": _ID_SCHEMA, "uniqueItems": True},
    },
}
_EDGE_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "edge_id",
        "source_node_id",
        "target_node_id",
        "edge_type",
        "branch_id",
        "condition",
    ],
    "properties": {
        "edge_id": _ID_SCHEMA,
        "source_node_id": _ID_SCHEMA,
        "target_node_id": _ID_SCHEMA,
        "edge_type": {
            "type": "string",
            "enum": ["dependency", "exploration", "conditional", "merge"],
        },
        "branch_id": {"oneOf": [_ID_SCHEMA, {"type": "null"}]},
        "condition": {"oneOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
    },
}
_BRANCH_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["branch_id", "label", "fork_node_id", "node_ids", "merge_node_id"],
    "properties": {
        "branch_id": _ID_SCHEMA,
        "label": {"type": "string", "minLength": 1},
        "fork_node_id": _ID_SCHEMA,
        "node_ids": {"type": "array", "items": _ID_SCHEMA, "minItems": 1, "uniqueItems": True},
        "merge_node_id": _ID_SCHEMA,
    },
}
_PHASE_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "phase_id",
        "title",
        "node_ids",
        "reviewer_node_id",
        "gate_node_id",
        "rework_node_ids",
    ],
    "properties": {
        "phase_id": _ID_SCHEMA,
        "title": {"type": "string"},
        "node_ids": {"type": "array", "items": _ID_SCHEMA},
        "reviewer_node_id": _ID_SCHEMA,
        "gate_node_id": _ID_SCHEMA,
        "rework_node_ids": {"type": "array", "items": _ID_SCHEMA},
    },
}
_BASE_PLAN_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "plan_revision_id",
        "version",
        "completion_contract_id",
        "completion_contract_version",
        "nodes",
        "edges",
        "branches",
        "design_document",
        "phases",
    ],
    "properties": {
        "plan_revision_id": _ID_SCHEMA,
        "version": {"type": "integer", "minimum": 1},
        "completion_contract_id": _ID_SCHEMA,
        "completion_contract_version": {"type": "integer", "minimum": 1},
        "nodes": {"type": "array", "items": _PLAN_NODE_INPUT_SCHEMA, "minItems": 1},
        "edges": {"type": "array", "items": _EDGE_INPUT_SCHEMA},
        "branches": {"type": "array", "items": _BRANCH_INPUT_SCHEMA},
        "design_document": {"type": ["string", "null"]},
        "phases": {"type": "array", "items": _PHASE_INPUT_SCHEMA},
    },
}
_REPLAN_ATTEMPT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["attempt_id", "plan_node_id", "sequence", "status", "reason"],
    "properties": {
        "attempt_id": _ID_SCHEMA,
        "plan_node_id": _ID_SCHEMA,
        "sequence": {"type": "integer", "minimum": 1},
        "status": {
            "type": "string",
            "enum": [
                "pending",
                "running",
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
                "interrupted",
            ],
        },
        "reason": {"oneOf": [{"type": "string"}, {"type": "null"}]},
    },
}
_REPLAN_CHECK_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "check_run_id",
        "check_id",
        "plan_node_id",
        "attempt_id",
        "status",
        "passed",
        "reason",
    ],
    "properties": {
        "check_run_id": _ID_SCHEMA,
        "check_id": _ID_SCHEMA,
        "plan_node_id": _ID_SCHEMA,
        "attempt_id": _ID_SCHEMA,
        "status": {
            "type": "string",
            "enum": [
                "pending",
                "running",
                "completed",
                "failed",
                "timed_out",
                "cancelled",
                "interrupted",
            ],
        },
        "passed": {"oneOf": [{"type": "boolean"}, {"type": "null"}]},
        "reason": {"oneOf": [{"type": "string"}, {"type": "null"}]},
    },
}
_CHECK_SPEC_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "check_id",
        "name",
        "kind",
        "description",
        "required",
        "command_argv",
        "semantic_required_terms",
    ],
    "properties": {
        "check_id": _ID_SCHEMA,
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": [item.value for item in CheckKind]},
        "description": {"type": "string"},
        "required": {"type": "boolean"},
        "command_argv": {"type": "array", "items": {"type": "string"}},
        "semantic_required_terms": {"type": "array", "items": {"type": "string"}},
    },
}
_REPLAN_CHECKPOINT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checkpoint_id", "event_offset", "artifact_ids"],
    "properties": {
        "checkpoint_id": _ID_SCHEMA,
        "event_offset": {"type": "integer", "minimum": 1},
        "artifact_ids": {
            "type": "array",
            "items": _ID_SCHEMA,
            "maxItems": MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS,
            "uniqueItems": True,
        },
    },
}
_REPLAN_INTERVENTION_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intervention_id",
        "plan_node_id",
        "attempt_id",
        "source_process_revision_id",
        "kind",
        "status",
        "reason",
        "evidence",
        "needed",
        "reply",
    ],
    "properties": {
        "intervention_id": _ID_SCHEMA,
        "plan_node_id": _ID_SCHEMA,
        "attempt_id": _ID_SCHEMA,
        "source_process_revision_id": {"oneOf": [_ID_SCHEMA, {"type": "null"}]},
        "kind": {"type": "string", "enum": ["worker_blocked", "external_effects"]},
        "status": {"type": "string", "enum": ["open", "replied"]},
        **{key: {"type": ["string", "null"]} for key in ("reason", "evidence", "needed", "reply")},
    },
}
_REPLAN_CONTEXT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "source_run_id",
        "source_run_status",
        "source_run_reason",
        "failed_plan_node_ids",
        "attempts",
        "failed_checks",
        "consumed_attempt_count",
        "latest_checkpoint",
        "blocked_plan_node_ids",
        "interventions",
        "intervention_count",
        "approved_checks",
        "approved_design_document",
    ],
    "properties": {
        "source_run_id": _ID_SCHEMA,
        "approved_design_document": {"type": ["string", "null"]},
        "source_run_status": {"type": "string", "enum": ["paused", "failed", "cancelled"]},
        "source_run_reason": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "failed_plan_node_ids": {
            "type": "array",
            "items": _ID_SCHEMA,
            "uniqueItems": True,
        },
        "attempts": {
            "type": "array",
            "items": _REPLAN_ATTEMPT_SCHEMA,
            "maxItems": MAX_REPLAN_ATTEMPT_SUMMARIES,
        },
        "failed_checks": {
            "type": "array",
            "items": _REPLAN_CHECK_SCHEMA,
            "maxItems": MAX_REPLAN_CHECK_SUMMARIES,
        },
        "consumed_attempt_count": {"type": "integer", "minimum": 0},
        "latest_checkpoint": {"oneOf": [_REPLAN_CHECKPOINT_SCHEMA, {"type": "null"}]},
        "blocked_plan_node_ids": {"type": "array", "items": _ID_SCHEMA, "uniqueItems": True},
        "interventions": {
            "type": "array",
            "items": _REPLAN_INTERVENTION_SCHEMA,
            "maxItems": MAX_REPLAN_INTERVENTION_SUMMARIES,
        },
        "intervention_count": {"type": "integer", "minimum": 0},
        "approved_checks": {
            "type": "array",
            "items": _CHECK_SPEC_SCHEMA,
        },
    },
}
_INPUT_SCHEMA: dict[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "EHAI Codex Planner input",
    "description": (
        "Planner-only Goal, criteria, budget, and optional approved base PlanGraph; "
        "discussion may omit criteria for the model to define its final Gate."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": ["operation", "goal", "completion_criteria", "exploration_budget"],
    "properties": {
        "operation": {"type": "string", "enum": ["propose", "replan", "discuss"]},
        "goal": {
            "type": "object",
            "additionalProperties": False,
            "required": ["goal_id", "objective"],
            "properties": {
                "goal_id": _ID_SCHEMA,
                "objective": {"type": "string", "minLength": 1},
            },
        },
        "completion_criteria": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "exploration_budget": {
            "type": "object",
            "additionalProperties": False,
            "required": ["max_attempts", "max_width", "max_depth"],
            "properties": {
                "max_attempts": {"type": "integer", "minimum": 1},
                "max_width": {"type": "integer", "minimum": 1},
                "max_depth": {"type": "integer", "minimum": 1},
            },
        },
        "base_plan_revision": _BASE_PLAN_INPUT_SCHEMA,
        "replan_context": _REPLAN_CONTEXT_SCHEMA,
        "base_checks": {"type": "array", "items": _CHECK_SPEC_SCHEMA},
    },
    "oneOf": [
        {
            "properties": {"operation": {"const": "propose"}},
            "required": ["operation"],
            "not": {
                "anyOf": [
                    {"required": ["base_plan_revision"]},
                    {"required": ["replan_context"]},
                ]
            },
        },
        {
            "properties": {"operation": {"const": "replan"}},
            "required": ["operation", "base_plan_revision"],
        },
        {
            "properties": {"operation": {"const": "discuss"}},
            "required": ["operation"],
        },
    ],
    "allOf": [
        {
            "if": {
                "properties": {
                    "operation": {"enum": ["propose", "replan"]},
                },
            },
            "then": {
                "properties": {"completion_criteria": {"minItems": 1}},
            },
        },
    ],
}

_STEP_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "instruction"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "instruction": {"type": "string", "minLength": 1},
    },
}
_OUTPUT_SCHEMA: dict[str, JsonValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "EHAI Codex exploration plan result",
    "description": "A bounded two-branch P1 exploration plan proposal.",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "fork", "branches", "evaluator", "merge"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "fork": _STEP_SCHEMA,
        "branches": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "title", "instruction"],
                "properties": {
                    "label": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "instruction": {"type": "string", "minLength": 1},
                },
            },
        },
        "evaluator": _STEP_SCHEMA,
        "merge": _STEP_SCHEMA,
    },
}


class CodexPlannerProtocolError(ValueError):
    """Raised when a Codex Planner result violates the P1 protocol."""


@dataclass(frozen=True, slots=True)
class PlannerStep:
    """Validated title and instruction for one generated PlanNode."""

    title: str
    instruction: str


@dataclass(frozen=True, slots=True)
class PlannerBranch:
    """Validated branch label and work-node content."""

    label: str
    title: str
    instruction: str


@dataclass(frozen=True, slots=True)
class ParsedCodexPlan:
    """Protocol-only model, independent of Worker and planning domain construction."""

    summary: str
    fork: PlannerStep
    branches: tuple[PlannerBranch, PlannerBranch]
    evaluator: PlannerStep
    merge: PlannerStep


def build_codex_planner_input(
    goal: Goal,
    criteria: tuple[str, ...],
    budget: ExplorationBudget,
    base: PlanRevision | None = None,
    context: ReplanContext | None = None,
    *,
    discussion: bool = False,
    checks: tuple[CheckSpec, ...] = (),
) -> dict[str, JsonValue]:
    """Build one explicit Planner input document with optional bounded failure evidence."""
    if not isinstance(discussion, bool):
        raise TypeError("discussion must be a boolean")
    if any(not isinstance(check, CheckSpec) for check in checks):
        raise TypeError("checks must contain CheckSpec values")
    if checks and base is None:
        raise ValueError("base check snapshots require a base PlanRevision")
    if checks and base is not None:
        base_check_ids = tuple(
            dict.fromkeys(check_id for node in base.nodes for check_id in node.required_check_ids)
        )
        if len(checks) != len(base_check_ids) or {check.check_id for check in checks} != set(
            base_check_ids
        ):
            raise ValueError("base checks must match the Check IDs referenced by the base plan")
    document: dict[str, JsonValue] = {
        "operation": "discuss" if discussion else ("replan" if base is not None else "propose"),
        "goal": {
            "goal_id": goal.goal_id,
            "objective": goal.objective,
        },
        "completion_criteria": list(criteria),
        "exploration_budget": {
            "max_attempts": budget.max_attempts,
            "max_width": budget.max_width,
            "max_depth": budget.max_depth,
        },
    }
    if base is not None:
        document["base_plan_revision"] = _base_plan_document(base)
    if checks:
        document["base_checks"] = [check_spec_document(check) for check in checks]
    if context is not None:
        if base is None:
            raise ValueError("ReplanContext requires a base PlanRevision")
        document["replan_context"] = context.to_dict()
    return document


def codex_planner_input_schema_json() -> str:
    """Return the strict Schema for propose, replan, and discussion input documents."""
    return json_dumps(_INPUT_SCHEMA)


def build_codex_planner_prompt(input_document: dict[str, JsonValue]) -> str:
    """Serialize exactly one explicit input document into the independent prompt."""
    sections = (
        "EHAI CODEX PLANNER PROTOCOL v1",
        "Propose a plan only. Do not execute work and do not claim the Goal is complete.",
        "The graph shape is fixed: fork, two independent branches, evaluator, then merge.",
        "Checks and Gates outside this process retain all completion authority.",
        "For replan, use the supplied approved base PlanGraph as the revision context.",
        "When base_checks is present, it is the exact CheckSpec snapshot for the supplied base "
        "PlanRevision. The base may be an unapproved draft, so these facts are not approval or "
        "authorization. Use them to map each existing condition to its original node owner; a new "
        "PlanRevision may receive new Check IDs. When the discussion asks to retain a condition, "
        "carry its argv and semantic terms; an explicit request to change a condition is a "
        "proposed new condition requiring approval. base_checks is not host configuration and "
        "does not grant command permission.",
        "Interventions retain their source task, Attempt and process identity. Replies are "
        "continuation facts, not new approval. A changed route does not resolve external effects. "
        "Intervention summaries are bounded; compare intervention_count to the supplied list "
        "and do not treat omitted history as resolved or absent.",
        "The input contains no ExecutionTrace or Artifact content.",
        "--- PLANNER_INPUT_JSON ---",
        json_dumps(input_document),
        "--- OUTPUT_REQUIREMENTS ---",
        "Return exactly one JSON object matching the supplied output schema.",
        "Return exactly two Branch entries with distinct non-blank labels.",
        "Do not add fields outside the schema.",
    )
    return "\n".join(sections)


def map_codex_planner_event_types(provider_event_types: tuple[str, ...]) -> tuple[str, ...]:
    """Map provider JSONL facts into stable Planner-role event categories."""
    known = {
        "thread.started": "planner.provider.started",
        "turn.started": "planner.started",
        "turn.completed": "planner.completed",
        "turn.failed": "planner.failed",
    }
    mapped: list[str] = []
    for provider_type in provider_event_types:
        planner_type = known.get(provider_type, "planner.provider.event")
        if planner_type not in mapped:
            mapped.append(planner_type)
    return tuple(mapped)


def codex_planner_output_schema_json() -> str:
    """Return the canonical strict JSON Schema for a Planner result."""
    return json_dumps(_OUTPUT_SCHEMA)


def _base_plan_document(base: PlanRevision) -> dict[str, JsonValue]:
    nodes: list[JsonValue] = [
        {
            "plan_node_id": node.plan_node_id,
            "title": node.title,
            "instruction": node.instruction,
            "kind": node.kind.value,
            "status": node.status.value,
            "required_capabilities": [item.name for item in sorted(node.required_capabilities)],
            "session_policy": node.session_policy.value,
            "required_dependency_ids": list(node.required_dependency_ids),
            "required_check_ids": list(node.required_check_ids),
        }
        for node in base.nodes
    ]
    edges: list[JsonValue] = [
        {
            "edge_id": edge.edge_id,
            "source_node_id": edge.source_node_id,
            "target_node_id": edge.target_node_id,
            "edge_type": edge.edge_type.value,
            "branch_id": edge.branch_id,
            "condition": edge.condition,
        }
        for edge in base.edges
    ]
    branches: list[JsonValue] = [
        {
            "branch_id": branch.branch_id,
            "label": branch.label,
            "fork_node_id": branch.fork_node_id,
            "node_ids": list(branch.node_ids),
            "merge_node_id": branch.merge_node_id,
        }
        for branch in base.branches
    ]
    return {
        "plan_revision_id": base.plan_revision_id,
        "version": base.version,
        "completion_contract_id": base.completion_contract_id,
        "completion_contract_version": base.completion_contract_version,
        "nodes": nodes,
        "edges": edges,
        "branches": branches,
        "design_document": base.design_document,
        "phases": [
            {
                "phase_id": phase.phase_id,
                "title": phase.title,
                "node_ids": list(phase.node_ids),
                "reviewer_node_id": phase.reviewer_node_id,
                "gate_node_id": phase.gate_node_id,
                "rework_node_ids": list(phase.rework_node_ids),
            }
            for phase in base.phases
        ],
    }


def parse_codex_planner_result(document: str) -> ParsedCodexPlan:
    """Fail closed before any domain PlanGraph object is constructed."""
    if not isinstance(document, str):
        raise CodexPlannerProtocolError("Codex Planner result must be JSON text")
    decoded = _strict_json_decode(document)
    if not isinstance(decoded, dict):
        raise CodexPlannerProtocolError("Codex Planner result must be a JSON object")
    _require_exact_keys(decoded, _OUTPUT_KEYS, "Codex Planner result")

    summary = _required_text(decoded["summary"], "summary")
    fork = _parse_step(decoded["fork"], "fork")
    evaluator = _parse_step(decoded["evaluator"], "evaluator")
    merge = _parse_step(decoded["merge"], "merge")
    branch_documents = decoded["branches"]
    if not isinstance(branch_documents, list) or len(branch_documents) != 2:
        raise CodexPlannerProtocolError("branches must contain exactly two items")
    parsed_branches = tuple(
        _parse_branch(value, f"branch[{index}]") for index, value in enumerate(branch_documents)
    )
    first, second = parsed_branches
    if first.label == second.label:
        raise CodexPlannerProtocolError("Branch labels must be distinct")
    return ParsedCodexPlan(
        summary=summary,
        fork=fork,
        branches=(first, second),
        evaluator=evaluator,
        merge=merge,
    )


def _parse_step(value: object, owner: str) -> PlannerStep:
    if not isinstance(value, dict):
        raise CodexPlannerProtocolError(f"{owner} must be an object")
    _require_exact_keys(value, _STEP_KEYS, owner)
    return PlannerStep(
        title=_required_text(value["title"], f"{owner}.title"),
        instruction=_required_text(value["instruction"], f"{owner}.instruction"),
    )


def _parse_branch(value: object, owner: str) -> PlannerBranch:
    if not isinstance(value, dict):
        raise CodexPlannerProtocolError(f"{owner} must be an object")
    _require_exact_keys(value, _BRANCH_KEYS, owner)
    return PlannerBranch(
        label=_required_text(value["label"], f"{owner}.label"),
        title=_required_text(value["title"], f"{owner}.title"),
        instruction=_required_text(value["instruction"], f"{owner}.instruction"),
    )


def _strict_json_decode(document: str) -> object:
    def reject_constant(value: str) -> None:
        raise CodexPlannerProtocolError(f"non-standard JSON constant is not allowed: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CodexPlannerProtocolError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            document,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except CodexPlannerProtocolError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise CodexPlannerProtocolError(f"invalid Codex Planner JSON: {error}") from error


def _require_exact_keys(value: dict[str, object], expected: frozenset[str], owner: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CodexPlannerProtocolError(
            f"{owner} keys are invalid; missing={missing}, extra={extra}"
        )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodexPlannerProtocolError(f"{field_name} must be non-blank text")
    return value.strip()
