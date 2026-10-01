"""Backend-neutral JSON documents describing Planner input and existing plan graphs."""

from __future__ import annotations

from ehai import JsonValue
from ehai.application.planner import ExplorationBudget, ReplanContext, check_spec_document
from ehai.domain.checking import CheckSpec
from ehai.domain.goal import Goal
from ehai.domain.planning import PlanRevision


def build_planner_input(
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
        document["base_plan_revision"] = base_plan_document(base)
    if checks:
        document["base_checks"] = [check_spec_document(check) for check in checks]
    if context is not None:
        if base is None:
            raise ValueError("ReplanContext requires a base PlanRevision")
        document["replan_context"] = context.to_dict()
    return document


def base_plan_document(base: PlanRevision) -> dict[str, JsonValue]:
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
