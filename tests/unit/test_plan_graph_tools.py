from __future__ import annotations

import pytest

from ehai.application.planner import (
    ExplorationBudget,
    ExplorationUsage,
    PlanNodeTemplate,
    PlanTemplate,
)
from ehai.domain.planning import PlanNodeKind
from ehai.infrastructure.planners.plan_graph_tools import (
    MAX_PLAN_OPERATIONS,
    MAX_VALIDATION_RETRIES,
    PlanGraphToolRuntime,
)

_BUDGET = ExplorationBudget(max_attempts=24, max_width=3, max_depth=4)


def _runtime(budget: ExplorationBudget | None = None) -> PlanGraphToolRuntime:
    return PlanGraphToolRuntime(budget or _BUDGET)


def _node(key: str, kind: str = "work") -> dict[str, object]:
    return {
        "key": key,
        "title": f"Node {key}",
        "instruction": f"Work on {key}",
        "kind": kind,
    }


def _edge(
    source: str,
    target: str,
    edge_type: str = "dependency",
    branch_key: str | None = None,
) -> dict[str, object]:
    return {
        "source": source,
        "target": target,
        "edge_type": edge_type,
        "branch_key": branch_key,
        "condition": None,
    }


def _branch(
    key: str,
    fork: str,
    nodes: list[str],
    merge: str,
) -> dict[str, object]:
    return {
        "branch_key": key,
        "label": key,
        "fork_node_key": fork,
        "node_keys": nodes,
        "merge_node_key": merge,
        "remove": False,
    }


def _build_exploration_graph(runtime: PlanGraphToolRuntime) -> None:
    """Shared node, two two-node branches, evaluator, merge, final verification."""
    for key, kind in (
        ("research", "work"),
        ("fork", "fork"),
        ("alpha-first", "work"),
        ("alpha-second", "work"),
        ("beta-first", "work"),
        ("beta-second", "work"),
        ("evaluator", "evaluator"),
        ("merge", "merge"),
        ("verify", "work"),
    ):
        assert runtime.execute("add_plan_node", _node(key, kind))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("research", "fork"))["accepted"] is True
    for branch, nodes in (
        ("alpha", ("alpha-first", "alpha-second")),
        ("beta", ("beta-first", "beta-second")),
    ):
        assert (
            runtime.execute("set_plan_branch", _branch(branch, "fork", list(nodes), "merge"))[
                "accepted"
            ]
            is True
        )
        assert (
            runtime.execute("add_plan_edge", _edge("fork", nodes[0], "exploration", branch))[
                "accepted"
            ]
            is True
        )
        assert (
            runtime.execute("add_plan_edge", _edge(nodes[0], nodes[1], "dependency", branch))[
                "accepted"
            ]
            is True
        )
        assert (
            runtime.execute("add_plan_edge", _edge(nodes[1], "merge", "merge", branch))["accepted"]
            is True
        )
        assert runtime.execute("add_plan_edge", _edge(nodes[1], "evaluator"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("evaluator", "merge"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("merge", "verify"))["accepted"] is True


def test_tools_build_shared_node_two_multi_node_branches_evaluator_merge() -> None:
    runtime = _runtime()
    _build_exploration_graph(runtime)

    result = runtime.execute("finish_plan", {})

    assert result["accepted"] is True
    template = runtime.build_template()
    assert tuple(node.key for node in template.nodes) == (
        "research",
        "fork",
        "alpha-first",
        "alpha-second",
        "beta-first",
        "beta-second",
        "evaluator",
        "merge",
        "verify",
    )
    by_key = {node.key: node for node in template.nodes}
    assert by_key["research"].kind is PlanNodeKind.WORK
    assert by_key["fork"].kind is PlanNodeKind.FORK
    assert by_key["evaluator"].kind is PlanNodeKind.EVALUATOR
    assert by_key["merge"].kind is PlanNodeKind.MERGE
    assert by_key["alpha-first"].required_dependency_keys == ()
    assert by_key["evaluator"].required_dependency_keys == ("alpha-second", "beta-second")
    assert by_key["merge"].required_dependency_keys == ("evaluator",)
    assert by_key["verify"].required_dependency_keys == ("merge",)
    assert tuple(branch.key for branch in template.branches) == ("alpha", "beta")
    assert template.branches[0].node_keys == ("alpha-first", "alpha-second")
    assert template.usage is not None
    assert template.usage.width == 2
    assert template.usage.depth == 2
    assert template.usage.attempts == 9
    assert all(node.require_completion_checks for node in template.nodes)


def test_linear_plan_finishes_without_exploration_structure() -> None:
    runtime = _runtime()
    for key in ("first", "second", "third"):
        assert runtime.execute("add_plan_node", _node(key))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("first", "second"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("second", "third"))["accepted"] is True

    result = runtime.execute("finish_plan", {})

    assert result["accepted"] is True
    template = runtime.build_template()
    assert len(template.nodes) == 3
    assert template.branches == ()
    third = {node.key: node for node in template.nodes}["third"]
    assert third.required_dependency_keys == ("second",)


def test_local_rejections_return_structured_issues() -> None:
    runtime = _runtime()
    assert runtime.execute("add_plan_node", _node("a"))["accepted"] is True

    duplicate = runtime.execute("add_plan_node", _node("a"))
    assert duplicate["accepted"] is False
    assert duplicate["issues"][0]["code"] == "DUPLICATE_NODE_KEY"
    assert duplicate["issues"][0]["location"] == "nodes.a"

    unknown_target = runtime.execute("add_plan_edge", _edge("a", "missing"))
    assert unknown_target["accepted"] is False
    assert unknown_target["issues"][0]["code"] == "UNKNOWN_NODE"
    assert unknown_target["issues"][0]["related_keys"] == ["missing"]

    self_loop = runtime.execute("add_plan_edge", _edge("a", "a"))
    assert self_loop["accepted"] is False
    assert self_loop["issues"][0]["code"] == "SELF_DEPENDENCY"

    bad_kind = runtime.execute("add_plan_node", _node("b", kind="task"))
    assert bad_kind["accepted"] is False
    assert bad_kind["issues"][0]["code"] == "INVALID_NODE_KIND"

    bad_type = runtime.execute("add_plan_edge", {**_edge("a", "b"), "edge_type": "blocking"})
    assert bad_type["accepted"] is False
    assert bad_type["issues"][0]["code"] == "INVALID_EDGE_TYPE"

    assert runtime.execute("add_plan_node", _node("b"))["accepted"] is True
    unknown_branch = runtime.execute("add_plan_edge", _edge("a", "b", "exploration", "ghost"))
    assert unknown_branch["accepted"] is False
    assert unknown_branch["issues"][0]["code"] == "UNKNOWN_BRANCH"

    assert runtime.execute("add_plan_edge", _edge("a", "b"))["accepted"] is True
    duplicate_edge = runtime.execute("add_plan_edge", _edge("a", "b"))
    assert duplicate_edge["accepted"] is False
    assert duplicate_edge["issues"][0]["code"] == "DUPLICATE_EDGE"

    missing_node = runtime.execute("remove_plan_node", {"key": "ghost"})
    assert missing_node["accepted"] is False
    assert missing_node["issues"][0]["code"] == "UNKNOWN_NODE"


def test_finish_plan_returns_cycle_unknown_and_budget_diagnostics_with_locations() -> None:
    cycle = _runtime()
    for key in ("implement", "inspect"):
        assert cycle.execute("add_plan_node", _node(key))["accepted"] is True
    assert cycle.execute("add_plan_edge", _edge("implement", "inspect"))["accepted"] is True
    assert cycle.execute("add_plan_edge", _edge("inspect", "implement"))["accepted"] is True

    cycle_result = cycle.execute("finish_plan", {})
    assert cycle_result["accepted"] is False
    cycle_issues = cycle_result["issues"]
    assert any(
        issue["code"] == "CYCLE_DETECTED"
        and issue["location"] in {"edges.implement-to-inspect", "edges.inspect-to-implement"}
        and sorted(issue["related_keys"]) == ["implement", "inspect"]
        for issue in cycle_issues
    )

    unknown = _runtime()
    assert unknown.execute("add_plan_node", _node("only"))["accepted"] is True
    unknown.execute("remove_plan_node", {"key": "only"})
    empty = unknown.execute("finish_plan", {})
    assert empty["accepted"] is False
    assert empty["issues"][0]["code"] == "EMPTY_GRAPH"

    budget = _runtime(ExplorationBudget(max_attempts=2, max_width=1, max_depth=1))
    for key in ("a", "b", "c"):
        assert budget.execute("add_plan_node", _node(key))["accepted"] is True
    budget_result = budget.execute("finish_plan", {})
    assert budget_result["accepted"] is False
    assert any(issue["code"] == "EXPLORATION_BUDGET_EXCEEDED" for issue in budget_result["issues"])


def test_operation_budget_allows_128th_mutation_and_rejects_129th() -> None:
    runtime = _runtime(ExplorationBudget(max_attempts=1024, max_width=8, max_depth=8))
    for index in range(MAX_PLAN_OPERATIONS):
        result = runtime.execute("add_plan_node", _node(f"node-{index}"))
        assert result["accepted"] is True
    assert runtime.operations_used == MAX_PLAN_OPERATIONS

    rejected = runtime.execute("add_plan_node", _node("overflow"))
    assert rejected["accepted"] is False
    assert rejected["issues"][0]["code"] == "PLAN_OPERATION_BUDGET_EXHAUSTED"
    assert runtime.operations_used == MAX_PLAN_OPERATIONS

    for index in range(MAX_PLAN_OPERATIONS):
        assert runtime.execute("update_plan_node", _node(f"node-{index}"))["accepted"] is False
    assert runtime.operations_used == MAX_PLAN_OPERATIONS

    inspected = runtime.execute("inspect_plan", {})
    assert inspected["operations_used"] == MAX_PLAN_OPERATIONS
    assert inspected["operations_remaining"] == 0
    finished = runtime.execute("finish_plan", {})
    assert finished["accepted"] is True
    assert runtime.build_template().usage == ExplorationUsage(width=0, depth=0, attempts=128)


def test_rejected_mutations_still_consume_the_operation_budget() -> None:
    runtime = _runtime()
    for _ in range(MAX_PLAN_OPERATIONS):
        runtime.execute(
            "add_plan_node", {"key": "same", "title": "t", "instruction": "i", "kind": "work"}
        )

    assert runtime.operations_used == MAX_PLAN_OPERATIONS
    result = runtime.execute("add_plan_node", _node("fresh"))
    assert result["accepted"] is False


def test_validation_failures_one_to_four_allow_repair_and_fifth_is_terminal() -> None:
    runtime = _runtime()
    assert runtime.execute("add_plan_node", _node("stray", kind="fork"))["accepted"] is True

    for failure in range(MAX_VALIDATION_RETRIES):
        result = runtime.execute("finish_plan", {})
        assert result["accepted"] is False
        assert result.get("budget_exhausted") is None
        assert result["issues"]
        assert runtime.validation_failures == failure + 1

    terminal = runtime.execute("finish_plan", {})
    assert terminal["accepted"] is False
    assert terminal["budget_exhausted"] is True
    codes = [issue["code"] for issue in terminal["issues"]]
    assert "VALIDATION_BUDGET_EXHAUSTED" in codes
    assert runtime.validation_failures == MAX_VALIDATION_RETRIES + 1


def test_inspect_and_finish_do_not_consume_mutation_budget() -> None:
    runtime = _runtime()
    _build_exploration_graph(runtime)
    operations_after_build = runtime.operations_used
    assert operations_after_build > 0

    for _ in range(5):
        inspection = runtime.execute("inspect_plan", {})
        assert inspection["operations_used"] == operations_after_build
    for _ in range(3):
        assert runtime.execute("finish_plan", {})["accepted"] is True
    assert runtime.operations_used == operations_after_build


def test_update_remove_node_edge_and_branch() -> None:
    runtime = _runtime()
    assert runtime.execute("add_plan_node", _node("a"))["accepted"] is True
    assert runtime.execute("add_plan_node", _node("b"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("a", "b"))["accepted"] is True

    updated = runtime.execute("update_plan_node", _node("a", kind="evaluator"))
    assert updated["accepted"] is True
    assert runtime.execute("inspect_plan", {})["nodes"][0]["kind"] == "evaluator"

    assert runtime.execute("remove_plan_edge", _edge("a", "b"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("a", "b"))["accepted"] is True

    assert runtime.execute("add_plan_node", _node("fork", kind="fork"))["accepted"] is True
    assert runtime.execute("add_plan_node", _node("merge", kind="merge"))["accepted"] is True
    assert (
        runtime.execute("set_plan_branch", _branch("one", "fork", ["a", "b"], "merge"))["accepted"]
        is True
    )
    in_use = runtime.execute("remove_plan_node", {"key": "a"})
    assert in_use["accepted"] is False
    assert in_use["issues"][0]["code"] == "NODE_IN_USE"

    removed = runtime.execute(
        "set_plan_branch", {**_branch("one", "fork", ["a"], "merge"), "remove": True}
    )
    assert removed["accepted"] is True
    assert runtime.execute("inspect_plan", {})["branches"] == []

    node_removal = runtime.execute("remove_plan_node", {"key": "b"})
    assert node_removal["accepted"] is True
    inspection = runtime.execute("inspect_plan", {})
    assert [node["key"] for node in inspection["nodes"]] == ["a", "fork", "merge"]
    assert inspection["edges"] == []


def test_unknown_tool_is_rejected_without_crashing() -> None:
    runtime = _runtime()
    result = runtime.execute("submit_plan", {})
    assert result["accepted"] is False
    assert result["issues"][0]["code"] == "UNKNOWN_TOOL"


def test_branch_structure_diagnostics() -> None:
    runtime = _runtime()
    for key, kind in (
        ("fork", "fork"),
        ("a", "work"),
        ("b", "work"),
        ("evaluator", "evaluator"),
        ("merge", "merge"),
    ):
        assert runtime.execute("add_plan_node", _node(key, kind))["accepted"] is True

    single = runtime.execute("set_plan_branch", _branch("only", "fork", ["a"], "merge"))
    assert single["accepted"] is True
    result = runtime.execute("finish_plan", {})
    codes = [issue["code"] for issue in result["issues"]]
    assert "INSUFFICIENT_BRANCHES" in codes
    assert "MISSING_EVALUATOR" in codes

    assert (
        runtime.execute("set_plan_branch", _branch("second", "fork", ["b"], "merge"))["accepted"]
        is True
    )
    assert (
        runtime.execute("add_plan_edge", _edge("fork", "a", "exploration", "only"))["accepted"]
        is True
    )
    result = runtime.execute("finish_plan", {})
    codes = [issue["code"] for issue in result["issues"]]
    assert "MISSING_EVALUATOR" in codes
    assert any(issue["code"] == "BRANCH_INCOMPLETE" for issue in result["issues"])

    cross = _runtime()
    for key, kind in (
        ("fork", "fork"),
        ("a1", "work"),
        ("a2", "work"),
        ("b1", "work"),
        ("b2", "work"),
        ("evaluator", "evaluator"),
        ("merge", "merge"),
    ):
        assert cross.execute("add_plan_node", _node(key, kind))["accepted"] is True
    assert (
        cross.execute("set_plan_branch", _branch("alpha", "fork", ["a1", "a2"], "merge"))[
            "accepted"
        ]
        is True
    )
    assert (
        cross.execute("set_plan_branch", _branch("beta", "fork", ["b1", "b2"], "merge"))["accepted"]
        is True
    )
    for edge in (
        _edge("fork", "a1", "exploration", "alpha"),
        _edge("a1", "a2", "dependency", "alpha"),
        _edge("a2", "merge", "merge", "alpha"),
        _edge("fork", "b1", "exploration", "beta"),
        _edge("b1", "b2", "dependency", "beta"),
        _edge("b2", "merge", "merge", "beta"),
        _edge("a2", "evaluator"),
        _edge("b2", "evaluator"),
        _edge("evaluator", "merge"),
        _edge("a2", "b1"),
    ):
        assert cross.execute("add_plan_edge", edge)["accepted"] is True
    result = cross.execute("finish_plan", {})
    assert any(
        issue["code"] == "CROSS_BRANCH_DEPENDENCY" and issue["location"] == "edges.a2-to-b1"
        for issue in result["issues"]
    )


def test_merge_cannot_depend_on_branch_interior_nodes() -> None:
    runtime = _runtime()
    for key, kind in (
        ("fork", "fork"),
        ("a", "work"),
        ("b", "work"),
        ("evaluator", "evaluator"),
        ("merge", "merge"),
    ):
        assert runtime.execute("add_plan_node", _node(key, kind))["accepted"] is True
    for branch, nodes in (("alpha", ("a",)), ("beta", ("b",))):
        assert (
            runtime.execute("set_plan_branch", _branch(branch, "fork", list(nodes), "merge"))[
                "accepted"
            ]
            is True
        )
        assert (
            runtime.execute("add_plan_edge", _edge("fork", nodes[0], "exploration", branch))[
                "accepted"
            ]
            is True
        )
        assert (
            runtime.execute("add_plan_edge", _edge(nodes[0], "merge", "merge", branch))["accepted"]
            is True
        )
        assert runtime.execute("add_plan_edge", _edge(nodes[0], "evaluator"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("evaluator", "merge"))["accepted"] is True
    assert runtime.execute("add_plan_edge", _edge("a", "merge"))["accepted"] is True

    result = runtime.execute("finish_plan", {})
    assert any(
        issue["code"] == "MERGE_UNSELECTED_INPUT" and issue["related_keys"] == ["a"]
        for issue in result["issues"]
    )


def test_template_matches_domain_validation() -> None:
    runtime = _runtime()
    _build_exploration_graph(runtime)
    assert runtime.execute("finish_plan", {})["accepted"] is True
    template = runtime.build_template()
    assert isinstance(template, PlanTemplate)
    assert all(isinstance(node, PlanNodeTemplate) for node in template.nodes)


def test_build_template_requires_successful_finish() -> None:
    from ehai.infrastructure.planners.plan_graph_tools import PlanGraphToolError

    runtime = _runtime()
    with pytest.raises(PlanGraphToolError):
        runtime.build_template()
