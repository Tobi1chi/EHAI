"""In-memory PlanGraph construction Tools called directly by the Built-in Planner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise

from ehai import JsonValue
from ehai.application.builtin_agent import ToolDefinition
from ehai.application.planner import (
    BranchTemplate,
    EdgeTemplate,
    ExplorationBudget,
    ExplorationUsage,
    PlanNodeTemplate,
    PlanTemplate,
)
from ehai.domain.planning import EdgeType, PlanNodeKind

MAX_PLAN_OPERATIONS = 128
"""Maximum accepted graph mutations per Planner run; rejected mutations also count."""

MAX_VALIDATION_RETRIES = 4
"""finish_plan attempts whose full-validation diagnostics are returned for repair."""

_NODE_KINDS = tuple(kind.value for kind in PlanNodeKind)
_EDGE_TYPES = tuple(edge.value for edge in EdgeType)

_MUTATION_TOOLS = (
    "add_plan_node",
    "update_plan_node",
    "remove_plan_node",
    "add_plan_edge",
    "remove_plan_edge",
    "set_plan_branch",
    "set_final_gate",
)


class PlanGraphToolError(RuntimeError):
    """Raised when the Tool runtime is misused outside the Planner Tool loop."""


@dataclass(frozen=True, slots=True)
class PlanIssue:
    """One structured diagnostic addressed with model-side local keys."""

    code: str
    location: str
    message: str
    related_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "location": self.location,
            "message": self.message,
            "related_keys": list(self.related_keys),
        }


class _ToolRejection(Exception):
    """Internal signal carrying locally detectable issues back to one Tool result."""

    def __init__(self, *issues: PlanIssue) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.code for issue in issues))


@dataclass(slots=True)
class _DraftNode:
    key: str
    title: str
    instruction: str
    kind: PlanNodeKind


@dataclass(slots=True)
class _DraftEdge:
    source: str
    target: str
    edge_type: EdgeType
    branch_key: str | None
    condition: str | None


@dataclass(slots=True)
class _DraftBranch:
    key: str
    label: str
    fork: str
    nodes: tuple[str, ...]
    merge: str


class PlanGraphToolRuntime:
    """Execute model Tool Calls against one in-memory draft PlanGraph.

    The draft uses stable local keys, never persists anything, and only a successful
    ``finish_plan`` yields a provider-neutral ``PlanTemplate`` for the application
    builder to assign EHAI IDs, CheckSpecs, and CompletionContract.
    """

    def __init__(
        self,
        budget: ExplorationBudget,
        *,
        planner_event_types: tuple[str, ...] = (),
        require_final_gate: bool = False,
    ) -> None:
        self._budget = budget
        self._planner_event_types = planner_event_types
        if not isinstance(require_final_gate, bool):
            raise TypeError("require_final_gate must be a boolean")
        self._require_final_gate = require_final_gate
        self._nodes: dict[str, _DraftNode] = {}
        self._edges: dict[tuple[str, str, EdgeType, str | None], _DraftEdge] = {}
        self._branches: dict[str, _DraftBranch] = {}
        self._operations_used = 0
        self._validation_failures = 0
        self._finished_template: PlanTemplate | None = None
        self._final_gate_argv: tuple[str, ...] = ()

    @property
    def operations_used(self) -> int:
        return self._operations_used

    @property
    def validation_failures(self) -> int:
        return self._validation_failures

    @property
    def finished(self) -> bool:
        return self._finished_template is not None

    @property
    def validation_budget_exhausted(self) -> bool:
        return self._validation_failures > MAX_VALIDATION_RETRIES

    def build_template(self) -> PlanTemplate:
        """Return the validated template after a successful finish_plan."""
        if self._finished_template is None:
            raise PlanGraphToolError("finish_plan must succeed before building a template")
        return self._finished_template

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(_TOOL_DEFINITIONS[name] for name in _TOOL_ORDER)

    def execute(self, name: str, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """Dispatch one Tool Call and return a structured result for the model."""
        if name == "inspect_plan":
            return self._inspect()
        if name == "finish_plan":
            return self._finish()
        if name in _MUTATION_TOOLS:
            return self._mutate(name, arguments)
        return _rejected(
            PlanIssue(
                "UNKNOWN_TOOL",
                f"tool.{name}",
                f"Tool {name!r} is not part of the plan graph ToolSet",
            )
        )

    def _mutate(self, name: str, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        if self._finished_template is not None:
            return _rejected(
                PlanIssue(
                    "PLAN_ALREADY_FINISHED",
                    f"tool.{name}",
                    "The plan was already accepted by finish_plan",
                )
            )
        if self._operations_used >= MAX_PLAN_OPERATIONS:
            return _rejected(
                PlanIssue(
                    "PLAN_OPERATION_BUDGET_EXHAUSTED",
                    f"tool.{name}",
                    f"Plan operation budget of {MAX_PLAN_OPERATIONS} modifications is exhausted; "
                    "call finish_plan with the current graph",
                )
            )
        self._operations_used += 1
        handler = getattr(self, f"_tool_{name}")
        try:
            handler(arguments)
        except _ToolRejection as rejection:
            return _rejected(*rejection.issues)
        return {
            "accepted": True,
            "operations_used": self._operations_used,
            "operations_remaining": MAX_PLAN_OPERATIONS - self._operations_used,
        }

    def _tool_add_plan_node(self, arguments: Mapping[str, JsonValue]) -> None:
        key = _required_key(arguments, "key", "nodes")
        location = f"nodes.{key}"
        if key in self._nodes:
            raise _ToolRejection(
                PlanIssue(
                    "DUPLICATE_NODE_KEY",
                    location,
                    f"Node key {key!r} already exists",
                    (key,),
                )
            )
        self._nodes[key] = _DraftNode(
            key=key,
            title=_required_text(arguments, "title", location),
            instruction=_required_text(arguments, "instruction", location),
            kind=_required_node_kind(arguments, "kind", location),
        )

    def _tool_update_plan_node(self, arguments: Mapping[str, JsonValue]) -> None:
        key = _required_key(arguments, "key", "nodes")
        location = f"nodes.{key}"
        node = self._nodes.get(key)
        if node is None:
            raise _ToolRejection(
                PlanIssue("UNKNOWN_NODE", location, f"Node {key!r} does not exist")
            )
        node.title = _required_text(arguments, "title", location)
        node.instruction = _required_text(arguments, "instruction", location)
        node.kind = _required_node_kind(arguments, "kind", location)

    def _tool_remove_plan_node(self, arguments: Mapping[str, JsonValue]) -> None:
        key = _required_key(arguments, "key", "nodes")
        location = f"nodes.{key}"
        if key not in self._nodes:
            raise _ToolRejection(
                PlanIssue("UNKNOWN_NODE", location, f"Node {key!r} does not exist")
            )
        referencing = [
            branch.key
            for branch in self._branches.values()
            if key == branch.fork or key == branch.merge or key in branch.nodes
        ]
        if referencing:
            raise _ToolRejection(
                PlanIssue(
                    "NODE_IN_USE",
                    location,
                    f"Node {key!r} is referenced by branches {referencing}",
                    (key, *referencing),
                )
            )
        del self._nodes[key]
        self._edges = {
            edge_key: edge
            for edge_key, edge in self._edges.items()
            if edge.source != key and edge.target != key
        }

    def _tool_add_plan_edge(self, arguments: Mapping[str, JsonValue]) -> None:
        source = _required_key(arguments, "source", "edges")
        target = _required_key(arguments, "target", "edges")
        edge_type = _required_edge_type(arguments, "edge_type")
        location = f"edges.{source}-to-{target}"
        branch_key = _optional_key(arguments, "branch_key")
        condition = _optional_text(arguments, "condition", location)
        missing = tuple(key for key in (source, target) if key not in self._nodes)
        if missing:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_NODE",
                    location,
                    f"Edge references unknown node(s) {missing}",
                    missing,
                )
            )
        if source == target:
            raise _ToolRejection(
                PlanIssue(
                    "SELF_DEPENDENCY",
                    location,
                    "An edge cannot connect a node to itself",
                    (source,),
                )
            )
        if branch_key is not None and branch_key not in self._branches:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_BRANCH",
                    location,
                    f"Branch {branch_key!r} does not exist",
                    (branch_key,),
                )
            )
        if edge_type in (EdgeType.EXPLORATION, EdgeType.MERGE) and branch_key is None:
            raise _ToolRejection(
                PlanIssue(
                    "EDGE_REQUIRES_BRANCH",
                    location,
                    f"{edge_type.value} edges require branch_key",
                    (source, target),
                )
            )
        if edge_type is EdgeType.CONDITIONAL and condition is None:
            raise _ToolRejection(
                PlanIssue("CONDITION_REQUIRED", location, "conditional edges require a condition")
            )
        if edge_type is not EdgeType.CONDITIONAL and condition is not None:
            raise _ToolRejection(
                PlanIssue(
                    "CONDITION_NOT_ALLOWED", location, "only conditional edges carry a condition"
                )
            )
        edge_key = (source, target, edge_type, branch_key)
        if edge_key in self._edges:
            raise _ToolRejection(PlanIssue("DUPLICATE_EDGE", location, "Edge already exists"))
        if edge_type is EdgeType.DEPENDENCY and any(
            edge.source == source
            and edge.target == target
            and edge.edge_type is EdgeType.DEPENDENCY
            for edge in self._edges.values()
        ):
            raise _ToolRejection(
                PlanIssue(
                    "DUPLICATE_EDGE",
                    location,
                    "A dependency edge between these nodes already exists",
                )
            )
        self._edges[edge_key] = _DraftEdge(source, target, edge_type, branch_key, condition)

    def _tool_remove_plan_edge(self, arguments: Mapping[str, JsonValue]) -> None:
        source = _required_key(arguments, "source", "edges")
        target = _required_key(arguments, "target", "edges")
        edge_type = _required_edge_type(arguments, "edge_type")
        location = f"edges.{source}-to-{target}"
        branch_key = _optional_key(arguments, "branch_key")
        edge_key = (source, target, edge_type, branch_key)
        if edge_key in self._edges:
            del self._edges[edge_key]
            return
        fuzzy = [
            key
            for key, edge in self._edges.items()
            if edge.source == source and edge.target == target and edge.edge_type is edge_type
        ]
        if fuzzy:
            del self._edges[fuzzy[0]]
            return
        raise _ToolRejection(
            PlanIssue(
                "UNKNOWN_EDGE",
                location,
                f"No {edge_type.value} edge {source!r} -> {target!r} exists",
                (source, target),
            )
        )

    def _tool_set_plan_branch(self, arguments: Mapping[str, JsonValue]) -> None:
        branch_key = _required_key(arguments, "branch_key", "branches")
        location = f"branches.{branch_key}"
        existing = self._branches.get(branch_key)
        if arguments.get("remove") is True:
            if existing is None:
                raise _ToolRejection(
                    PlanIssue("UNKNOWN_BRANCH", location, f"Branch {branch_key!r} does not exist")
                )
            if any(edge.branch_key == branch_key for edge in self._edges.values()):
                raise _ToolRejection(
                    PlanIssue(
                        "BRANCH_IN_USE",
                        location,
                        "Remove or re-target edges that reference this branch first",
                        (branch_key,),
                    )
                )
            del self._branches[branch_key]
            return
        label = _required_text(arguments, "label", location)
        fork = _required_key(arguments, "fork_node_key", location)
        merge = _required_key(arguments, "merge_node_key", location)
        node_keys = _required_node_keys(arguments, "node_keys", location)
        missing = tuple(key for key in (fork, merge, *node_keys) if key not in self._nodes)
        if missing:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_NODE",
                    location,
                    f"Branch references unknown node(s) {missing}",
                    missing,
                )
            )
        if fork == merge:
            raise _ToolRejection(
                PlanIssue("BRANCH_INVALID", location, "Branch fork and merge keys must differ")
            )
        if fork in node_keys or merge in node_keys:
            raise _ToolRejection(
                PlanIssue(
                    "BRANCH_INVALID",
                    location,
                    "Branch internal nodes must not include the fork or merge node",
                )
            )
        self._branches[branch_key] = _DraftBranch(branch_key, label, fork, node_keys, merge)

    def _tool_set_final_gate(self, arguments: Mapping[str, JsonValue]) -> None:
        value = arguments.get("argv")
        if not isinstance(value, list) or not value:
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    "final_gate.argv",
                    "argv must be a non-empty array of command arguments",
                )
            )
        if any(not isinstance(argument, str) for argument in value):
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    "final_gate.argv",
                    "argv must contain only strings",
                )
            )
        argv = tuple(argument for argument in value if isinstance(argument, str))
        if any(not argument or "\x00" in argument for argument in argv):
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    "final_gate.argv",
                    "argv must contain non-empty strings without null bytes",
                )
            )
        self._final_gate_argv = argv

    def _inspect(self) -> dict[str, JsonValue]:
        return {
            "nodes": [_node_document(node) for node in self._nodes.values()],
            "edges": [_edge_document(edge) for edge in self._edges.values()],
            "branches": [_branch_document(branch) for branch in self._branches.values()],
            "final_gate_argv": list(self._final_gate_argv),
            "require_final_gate": self._require_final_gate,
            "operations_used": self._operations_used,
            "operations_remaining": MAX_PLAN_OPERATIONS - self._operations_used,
            "validation_failures": self._validation_failures,
            "validation_attempts_remaining": MAX_VALIDATION_RETRIES + 1 - self._validation_failures,
        }

    def _finish(self) -> dict[str, JsonValue]:
        if self._finished_template is not None:
            return {
                "accepted": True,
                "nodes": len(self._nodes),
                "edges": len(self._edges),
                "branches": len(self._branches),
            }
        issues = self._validate_graph()
        if not issues:
            try:
                self._finished_template = self._compose_template()
            except ValueError as error:
                issues.append(
                    PlanIssue(
                        "PLAN_TEMPLATE_INVALID",
                        "graph",
                        f"PlanTemplate rejected the graph: {error}",
                    )
                )
        if not issues:
            return {
                "accepted": True,
                "nodes": len(self._nodes),
                "edges": len(self._edges),
                "branches": len(self._branches),
                "operations_used": self._operations_used,
            }
        self._validation_failures += 1
        payload_issues: list[JsonValue] = [issue.to_dict() for issue in issues]
        if self._validation_failures > MAX_VALIDATION_RETRIES:
            payload_issues.append(
                PlanIssue(
                    "VALIDATION_BUDGET_EXHAUSTED",
                    "tool.finish_plan",
                    f"finish_plan failed full validation {self._validation_failures} times; "
                    f"only {MAX_VALIDATION_RETRIES} repair attempts are allowed",
                ).to_dict()
            )
            return {"accepted": False, "budget_exhausted": True, "issues": payload_issues}
        return {
            "accepted": False,
            "issues": payload_issues,
            "operations_used": self._operations_used,
            "validation_attempts_remaining": MAX_VALIDATION_RETRIES + 1 - self._validation_failures,
        }

    def _validate_graph(self) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        if not self._nodes:
            return [
                PlanIssue(
                    "EMPTY_GRAPH",
                    "graph",
                    "The plan must contain at least one node before finishing",
                )
            ]
        for edge in self._edges.values():
            location = f"edges.{edge.source}-to-{edge.target}"
            if edge.source not in self._nodes or edge.target not in self._nodes:
                issues.append(
                    PlanIssue(
                        "UNKNOWN_REFERENCE",
                        location,
                        "Edge references a node that does not exist",
                        (edge.source, edge.target),
                    )
                )
            if edge.branch_key is not None and edge.branch_key not in self._branches:
                issues.append(
                    PlanIssue(
                        "UNKNOWN_REFERENCE",
                        location,
                        f"Edge references unknown branch {edge.branch_key!r}",
                        (edge.branch_key,),
                    )
                )
        for branch in self._branches.values():
            location = f"branches.{branch.key}"
            referenced = (branch.fork, branch.merge, *branch.nodes)
            missing = tuple(key for key in referenced if key not in self._nodes)
            if missing:
                issues.append(
                    PlanIssue(
                        "UNKNOWN_REFERENCE",
                        location,
                        f"Branch references unknown node(s) {missing}",
                        missing,
                    )
                )
        terminal_keys = self._terminal_node_keys()
        if len(terminal_keys) != 1:
            issues.append(
                PlanIssue(
                    "SINGLE_FINAL_SINK_REQUIRED",
                    "graph",
                    "The plan must have exactly one terminal integration node; "
                    f"found {list(terminal_keys)}",
                    terminal_keys,
                )
            )
        else:
            final_node = self._nodes[terminal_keys[0]]
            if final_node.kind in {PlanNodeKind.FORK, PlanNodeKind.EVALUATOR}:
                issues.append(
                    PlanIssue(
                        "FINAL_SINK_INVALID",
                        f"nodes.{final_node.key}",
                        "The single terminal node must be a work or merge integration node",
                        (final_node.key,),
                    )
                )
            if any(terminal_keys[0] in branch.nodes for branch in self._branches.values()):
                issues.append(
                    PlanIssue(
                        "FINAL_SINK_PRUNABLE",
                        f"nodes.{terminal_keys[0]}",
                        "The final integration node cannot be an exploration branch interior",
                        (terminal_keys[0],),
                    )
                )
        if self._require_final_gate and not self._final_gate_argv:
            issues.append(
                PlanIssue(
                    "FINAL_GATE_REQUIRED",
                    "final_gate",
                    "Set the final behavioral gate with set_final_gate before finishing",
                )
            )
        issues.extend(self._validate_cycle())
        fork_nodes = {branch.fork for branch in self._branches.values()}
        merge_nodes = {branch.merge for branch in self._branches.values()}
        for node in self._nodes.values():
            if node.kind is PlanNodeKind.FORK and node.key not in fork_nodes:
                issues.append(
                    PlanIssue(
                        "BRANCH_INCOMPLETE",
                        f"nodes.{node.key}",
                        "A fork node must be the fork of at least one branch",
                        (node.key,),
                    )
                )
            if node.kind is PlanNodeKind.MERGE and node.key not in merge_nodes:
                issues.append(
                    PlanIssue(
                        "BRANCH_INCOMPLETE",
                        f"nodes.{node.key}",
                        "A merge node must be the merge target of at least one branch",
                        (node.key,),
                    )
                )
            if not self._branches and node.kind is PlanNodeKind.EVALUATOR:
                issues.append(
                    PlanIssue(
                        "BRANCH_INCOMPLETE",
                        f"nodes.{node.key}",
                        "An evaluator node requires exploration branches",
                        (node.key,),
                    )
                )
        if self._branches:
            issues.extend(self._validate_exploration_structure())
        usage = self._usage()
        try:
            usage.require_within(self._budget)
        except ValueError as error:
            issues.append(PlanIssue("EXPLORATION_BUDGET_EXCEEDED", "graph", str(error)))
        return issues

    def _validate_cycle(self) -> list[PlanIssue]:
        incoming = {key: 0 for key in self._nodes}
        outgoing: dict[str, list[str]] = {key: [] for key in self._nodes}
        for edge in self._edges.values():
            if edge.source not in self._nodes or edge.target not in self._nodes:
                continue
            incoming[edge.target] += 1
            outgoing[edge.source].append(edge.target)
        available = [key for key, count in incoming.items() if count == 0]
        visited = 0
        while available:
            key = available.pop()
            visited += 1
            for target in outgoing[key]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    available.append(target)
        if visited == len(self._nodes):
            return []
        remaining = sorted(key for key, count in incoming.items() if count > 0)
        cycle_edge = next(
            (
                edge
                for edge in self._edges.values()
                if edge.source in remaining and edge.target in remaining
            ),
            None,
        )
        location = (
            "graph" if cycle_edge is None else f"edges.{cycle_edge.source}-to-{cycle_edge.target}"
        )
        related = tuple(remaining) if cycle_edge is None else (cycle_edge.source, cycle_edge.target)
        return [
            PlanIssue(
                "CYCLE_DETECTED",
                location,
                "The plan graph must stay acyclic; remove or re-target edges "
                "among the listed nodes",
                related,
            )
        ]

    def _validate_exploration_structure(self) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        groups: dict[tuple[str, str], list[_DraftBranch]] = {}
        for branch in self._branches.values():
            groups.setdefault((branch.fork, branch.merge), []).append(branch)
        for (fork_key, merge_key), group in groups.items():
            fork_node = self._nodes.get(fork_key)
            merge_node = self._nodes.get(merge_key)
            if fork_node is None or merge_node is None:
                continue
            if fork_node.kind is not PlanNodeKind.FORK:
                issues.append(
                    PlanIssue(
                        "BRANCH_INCOMPLETE",
                        f"nodes.{fork_key}",
                        "A branch fork node must have kind 'fork'",
                        (fork_key,),
                    )
                )
            if merge_node.kind is not PlanNodeKind.MERGE:
                issues.append(
                    PlanIssue(
                        "BRANCH_INCOMPLETE",
                        f"nodes.{merge_key}",
                        "A branch merge node must have kind 'merge'",
                        (merge_key,),
                    )
                )
            if len(group) < 2:
                issues.append(
                    PlanIssue(
                        "INSUFFICIENT_BRANCHES",
                        f"nodes.{fork_key}",
                        "Each fork must have at least two branches",
                        tuple(branch.key for branch in group),
                    )
                )
            for branch in group:
                location = f"branches.{branch.key}"
                expected = [
                    (fork_key, branch.nodes[0], EdgeType.EXPLORATION, branch.key),
                    *((a, b, EdgeType.DEPENDENCY, branch.key) for a, b in pairwise(branch.nodes)),
                    (branch.nodes[-1], merge_key, EdgeType.MERGE, branch.key),
                ]
                for source, target, edge_type, key in expected:
                    if (source, target, edge_type, key) not in self._edges:
                        issues.append(
                            PlanIssue(
                                "BRANCH_INCOMPLETE",
                                location,
                                f"Branch lacks its {edge_type.value} edge {source!r} -> {target!r}",
                                (source, target),
                            )
                        )
            terminals = {branch.nodes[-1] for branch in group}
            evaluator = next(
                (
                    node
                    for node in self._nodes.values()
                    if node.kind is PlanNodeKind.EVALUATOR
                    and terminals <= set(self._dependencies_of(node.key))
                ),
                None,
            )
            if evaluator is None:
                issues.append(
                    PlanIssue(
                        "MISSING_EVALUATOR",
                        f"nodes.{fork_key}",
                        "One evaluator node must depend on every branch terminal node",
                        tuple(sorted(terminals)),
                    )
                )
            else:
                merge_dependencies = set(self._dependencies_of(merge_key))
                if evaluator.key not in merge_dependencies:
                    issues.append(
                        PlanIssue(
                            "MISSING_MERGE",
                            f"nodes.{merge_key}",
                            f"The merge node must depend on the evaluator {evaluator.key!r}",
                            (merge_key, evaluator.key),
                        )
                    )
                interior = {node_key for branch in group for node_key in branch.nodes}
                unselected = merge_dependencies & interior
                if unselected:
                    issues.append(
                        PlanIssue(
                            "MERGE_UNSELECTED_INPUT",
                            f"nodes.{merge_key}",
                            "The merge node must depend on the evaluator, not on branch interior "
                            "nodes whose branch may not be selected",
                            tuple(sorted(unselected)),
                        )
                    )
        node_branch: dict[str, str] = {
            node_key: branch.key for branch in self._branches.values() for node_key in branch.nodes
        }
        branch_group = {
            branch.key: (branch.fork, branch.merge) for branch in self._branches.values()
        }
        for edge in self._edges.values():
            source_branch = node_branch.get(edge.source)
            target_branch = node_branch.get(edge.target)
            if (
                source_branch is not None
                and target_branch is not None
                and source_branch != target_branch
                and branch_group[source_branch] == branch_group[target_branch]
            ):
                issues.append(
                    PlanIssue(
                        "CROSS_BRANCH_DEPENDENCY",
                        f"edges.{edge.source}-to-{edge.target}",
                        "Edges must not cross between sibling branch interiors of one fork",
                        (edge.source, edge.target),
                    )
                )
            if edge.edge_type in (EdgeType.EXPLORATION, EdgeType.MERGE):
                edge_branch = self._branches.get(edge.branch_key or "")
                if edge_branch is None:
                    continue
                path_pair = (
                    (edge_branch.fork, edge_branch.nodes[0])
                    if edge.edge_type is EdgeType.EXPLORATION
                    else (edge_branch.nodes[-1], edge_branch.merge)
                )
                if (edge.source, edge.target) != path_pair:
                    issues.append(
                        PlanIssue(
                            "EDGE_BRANCH_CONFLICT",
                            f"edges.{edge.source}-to-{edge.target}",
                            f"{edge.edge_type.value} edges must match their branch path",
                            (edge.source, edge.target),
                        )
                    )
        group_terminals: list[frozenset[str]] = [
            frozenset(branch.nodes[-1] for branch in group) for group in groups.values()
        ]
        for node in self._nodes.values():
            if node.kind is not PlanNodeKind.EVALUATOR:
                continue
            dependencies = set(self._dependencies_of(node.key))
            if not any(terminals <= dependencies for terminals in group_terminals):
                issues.append(
                    PlanIssue(
                        "BRANCH_INCOMPLETE",
                        f"nodes.{node.key}",
                        "An evaluator node must depend on every terminal of one fork group",
                        (node.key,),
                    )
                )
        return issues

    def _dependencies_of(self, node_key: str) -> tuple[str, ...]:
        return tuple(
            edge.source
            for edge in self._edges.values()
            if edge.target == node_key and edge.edge_type is EdgeType.DEPENDENCY
        )

    def _terminal_node_keys(self) -> tuple[str, ...]:
        sources = {edge.source for edge in self._edges.values()}
        return tuple(key for key in self._nodes if key not in sources)

    def _usage(self) -> ExplorationUsage:
        groups: dict[tuple[str, str], int] = {}
        for branch in self._branches.values():
            key = (branch.fork, branch.merge)
            groups[key] = groups.get(key, 0) + 1
        width = max(groups.values(), default=0)
        depth = max((len(branch.nodes) for branch in self._branches.values()), default=0)
        return ExplorationUsage(width=width, depth=depth, attempts=len(self._nodes))

    def _compose_template(self) -> PlanTemplate:
        final_node_key = self._terminal_node_keys()[0]
        dependencies: dict[str, list[str]] = {}
        for edge in self._edges.values():
            if edge.edge_type is EdgeType.DEPENDENCY:
                dependencies.setdefault(edge.target, []).append(edge.source)
        nodes = tuple(
            PlanNodeTemplate(
                key=node.key,
                title=node.title,
                instruction=node.instruction,
                kind=node.kind,
                require_completion_checks=node.key == final_node_key,
                required_dependency_keys=tuple(dependencies.get(node.key, ())),
            )
            for node in self._nodes.values()
        )
        edges = tuple(
            EdgeTemplate(
                source_node_key=edge.source,
                target_node_key=edge.target,
                edge_type=edge.edge_type,
                branch_key=edge.branch_key,
                condition=edge.condition,
            )
            for edge in self._edges.values()
        )
        branches = tuple(
            BranchTemplate(
                key=branch.key,
                label=branch.label,
                fork_node_key=branch.fork,
                node_keys=branch.nodes,
                merge_node_key=branch.merge,
            )
            for branch in self._branches.values()
        )
        usage = self._usage()
        return PlanTemplate(
            nodes=nodes,
            edges=edges,
            branches=branches,
            budget=self._budget,
            usage=usage,
            planner_event_types=self._planner_event_types,
            final_node_key=final_node_key,
            final_gate_argv=self._final_gate_argv,
        )


def _rejected(*issues: PlanIssue) -> dict[str, JsonValue]:
    return {"accepted": False, "issues": [issue.to_dict() for issue in issues]}


def _node_document(node: _DraftNode) -> dict[str, JsonValue]:
    return {
        "key": node.key,
        "title": node.title,
        "instruction": node.instruction,
        "kind": node.kind.value,
    }


def _edge_document(edge: _DraftEdge) -> dict[str, JsonValue]:
    return {
        "source": edge.source,
        "target": edge.target,
        "edge_type": edge.edge_type.value,
        "branch_key": edge.branch_key,
        "condition": edge.condition,
    }


def _branch_document(branch: _DraftBranch) -> dict[str, JsonValue]:
    return {
        "branch_key": branch.key,
        "label": branch.label,
        "fork_node_key": branch.fork,
        "node_keys": list(branch.nodes),
        "merge_node_key": branch.merge,
    }


def _required_key(
    arguments: Mapping[str, JsonValue],
    name: str,
    location_prefix: str,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT", f"{location_prefix}.{name}", f"{name} must be non-blank text"
            )
        )
    return value.strip()


def _optional_key(arguments: Mapping[str, JsonValue], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _ToolRejection(
            PlanIssue("INVALID_ARGUMENT", f"edges.{name}", f"{name} must be non-blank text or null")
        )
    return value.strip()


def _required_text(
    arguments: Mapping[str, JsonValue],
    name: str,
    location: str,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _ToolRejection(
            PlanIssue("INVALID_ARGUMENT", f"{location}.{name}", f"{name} must be non-blank text")
        )
    return value.strip()


def _optional_text(
    arguments: Mapping[str, JsonValue],
    name: str,
    location: str,
) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT", f"{location}.{name}", f"{name} must be non-blank text or null"
            )
        )
    return value.strip()


def _required_node_kind(
    arguments: Mapping[str, JsonValue],
    name: str,
    location: str,
) -> PlanNodeKind:
    value = arguments.get(name)
    if not isinstance(value, str) or value not in _NODE_KINDS:
        raise _ToolRejection(
            PlanIssue(
                "INVALID_NODE_KIND",
                f"{location}.{name}",
                f"kind must be one of {list(_NODE_KINDS)}",
            )
        )
    return PlanNodeKind(value)


def _required_edge_type(arguments: Mapping[str, JsonValue], name: str) -> EdgeType:
    value = arguments.get(name)
    if not isinstance(value, str) or value not in _EDGE_TYPES:
        raise _ToolRejection(
            PlanIssue(
                "INVALID_EDGE_TYPE",
                f"edges.{name}",
                f"edge_type must be one of {list(_EDGE_TYPES)}",
            )
        )
    return EdgeType(value)


def _required_node_keys(
    arguments: Mapping[str, JsonValue],
    name: str,
    location: str,
) -> tuple[str, ...]:
    value = arguments.get(name)
    if not isinstance(value, list) or not value:
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT",
                f"{location}.{name}",
                f"{name} must be a non-empty array of node keys",
            )
        )
    keys = tuple(item.strip() if isinstance(item, str) and item.strip() else "" for item in value)
    if any(not key for key in keys):
        raise _ToolRejection(
            PlanIssue("INVALID_ARGUMENT", f"{location}.{name}", "node keys must be non-blank text")
        )
    if len(set(keys)) != len(keys):
        raise _ToolRejection(
            PlanIssue("DUPLICATE_REFERENCE", f"{location}.{name}", "node keys must be unique")
        )
    return keys


_KEY_PROPERTY: dict[str, JsonValue] = {"type": "string", "minLength": 1, "maxLength": 128}
_LABEL_PROPERTY: dict[str, JsonValue] = {"type": "string", "minLength": 1, "maxLength": 200}
_INSTRUCTION_PROPERTY: dict[str, JsonValue] = {"type": "string", "minLength": 1, "maxLength": 4000}
_NODE_KIND_PROPERTY: dict[str, JsonValue] = {"type": "string", "enum": list(_NODE_KINDS)}
_EDGE_TYPE_PROPERTY: dict[str, JsonValue] = {"type": "string", "enum": list(_EDGE_TYPES)}
_FINAL_GATE_ARG_PROPERTY: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
}

_NODE_PARAMETERS: dict[str, JsonValue] = {
    "key": _KEY_PROPERTY,
    "title": _LABEL_PROPERTY,
    "instruction": _INSTRUCTION_PROPERTY,
    "kind": _NODE_KIND_PROPERTY,
}

_TOOL_ORDER = (
    "add_plan_node",
    "update_plan_node",
    "remove_plan_node",
    "add_plan_edge",
    "remove_plan_edge",
    "set_plan_branch",
    "set_final_gate",
    "inspect_plan",
    "finish_plan",
)

_TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    "add_plan_node": ToolDefinition(
        "add_plan_node",
        "Add one plan node with a stable local key, title, instruction, and kind.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "title", "instruction", "kind"],
            "properties": _NODE_PARAMETERS,
        },
    ),
    "update_plan_node": ToolDefinition(
        "update_plan_node",
        "Replace the title, instruction, and kind of an existing plan node.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "title", "instruction", "kind"],
            "properties": _NODE_PARAMETERS,
        },
    ),
    "remove_plan_node": ToolDefinition(
        "remove_plan_node",
        "Remove a plan node that no branch references; its edges are removed with it.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["key"],
            "properties": {"key": _KEY_PROPERTY},
        },
    ),
    "add_plan_edge": ToolDefinition(
        "add_plan_edge",
        "Add one directed edge; dependency edges between nodes determine required dependencies.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["source", "target", "edge_type", "branch_key", "condition"],
            "properties": {
                "source": _KEY_PROPERTY,
                "target": _KEY_PROPERTY,
                "edge_type": _EDGE_TYPE_PROPERTY,
                "branch_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
                "condition": {**_LABEL_PROPERTY, "type": ["string", "null"]},
            },
        },
    ),
    "remove_plan_edge": ToolDefinition(
        "remove_plan_edge",
        "Remove one existing edge identified by its endpoints and type.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["source", "target", "edge_type", "branch_key"],
            "properties": {
                "source": _KEY_PROPERTY,
                "target": _KEY_PROPERTY,
                "edge_type": _EDGE_TYPE_PROPERTY,
                "branch_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
            },
        },
    ),
    "set_plan_branch": ToolDefinition(
        "set_plan_branch",
        "Create or replace one exploration branch, or remove it with remove=true.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "branch_key",
                "label",
                "fork_node_key",
                "node_keys",
                "merge_node_key",
                "remove",
            ],
            "properties": {
                "branch_key": _KEY_PROPERTY,
                "label": {**_LABEL_PROPERTY, "type": ["string", "null"]},
                "fork_node_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
                "node_keys": {
                    "type": ["array", "null"],
                    "items": _KEY_PROPERTY,
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "merge_node_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
                "remove": {"type": "boolean"},
            },
        },
    ),
    "set_final_gate": ToolDefinition(
        "set_final_gate",
        "Set the final behavioral Command Check as an argv array, never shell text.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["argv"],
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 128,
                    "items": _FINAL_GATE_ARG_PROPERTY,
                }
            },
        },
    ),
    "inspect_plan": ToolDefinition(
        "inspect_plan",
        "Return the current draft graph and remaining budgets without consuming operations.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {},
        },
    ),
    "finish_plan": ToolDefinition(
        "finish_plan",
        "Validate the whole graph and finish the plan; fix returned issues and call it again.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {},
        },
    ),
}
