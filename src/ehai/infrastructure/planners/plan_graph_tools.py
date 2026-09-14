"""In-memory PlanGraph construction Tools called directly by the Built-in Planner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import cast

from ehai import ID, JsonValue, normalize_id
from ehai.application.agent_contracts import ToolDefinition
from ehai.application.planner import (
    BranchTemplate,
    EdgeTemplate,
    ExplorationBudget,
    ExplorationUsage,
    NodeGateTemplate,
    PhaseTemplate,
    PlanNodeTemplate,
    PlanTemplate,
)
from ehai.domain.planning import EdgeType, PlanNodeKind, PlanRevision
from ehai.domain.process import (
    ProcessRevision,
    process_approval_identity,
    process_graph_definition,
)
from ehai.domain.workers import SessionPolicy, WorkerCapability

MAX_PLAN_OPERATIONS = 128
"""Maximum accepted graph mutations per Planner run; rejected mutations also count."""

MAX_VALIDATION_RETRIES = 4
"""finish_plan attempts whose full-validation diagnostics are returned for repair."""

_NODE_KINDS = tuple(kind.value for kind in PlanNodeKind)
_EDGE_TYPES = tuple(edge.value for edge in EdgeType)
_SESSION_POLICIES = tuple(policy.value for policy in SessionPolicy)

_MUTATION_TOOLS = (
    "add_plan_node",
    "update_plan_node",
    "remove_plan_node",
    "add_plan_edge",
    "remove_plan_edge",
    "set_plan_branch",
    "set_plan_phase",
    "set_node_gate",
    "set_final_gate",
    "move_process_gate",
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
    required_capabilities: frozenset[WorkerCapability]
    session_policy: SessionPolicy


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


@dataclass(slots=True)
class _DraftNodeGate:
    node_key: str
    name: str
    command_argv: tuple[str, ...]
    human_question: str | None


@dataclass(slots=True)
class _DraftPhase:
    key: str
    title: str
    nodes: tuple[str, ...]
    reviewer: str
    gate: str
    rework_nodes: tuple[str, ...]


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
        self._phases: dict[str, _DraftPhase] = {}
        self._node_gates: dict[str, _DraftNodeGate] = {}
        self._operations_used = 0
        self._validation_failures = 0
        self._finished_template: PlanTemplate | None = None
        self._final_gate_argv: tuple[str, ...] = ()
        self._final_human_question: str | None = None
        self._process_mode = False
        self._retained_check_ids: dict[str, tuple[ID, ...]] = {}
        self._retained_gate_owners: dict[ID, str] = {}
        self._process_final_gate_id: ID | None = None

    @classmethod
    def from_process(
        cls,
        process: ProcessRevision,
        current: PlanRevision,
        budget: ExplorationBudget,
        *,
        planner_event_types: tuple[str, ...] = (),
    ) -> PlanGraphToolRuntime:
        """Seed a process-mode draft from an existing ProcessRevision graph.

        Existing domain UUIDs become local Tool keys verbatim.  The process's
        stable Gate-to-owner bindings and original Check IDs are retained as
        immutable template metadata; no local command, Check, or domain ID is
        created by this runtime.
        """
        if not isinstance(process, ProcessRevision):
            raise TypeError("process must be a ProcessRevision")
        if not isinstance(current, PlanRevision):
            raise TypeError("current must be a PlanRevision")
        if process_approval_identity(process.graph) != process_approval_identity(
            current
        ) or process_graph_definition(process.graph) != process_graph_definition(current):
            raise ValueError("Process seed must match its frozen definition and approval identity")
        runtime = cls(
            budget,
            planner_event_types=planner_event_types,
            require_final_gate=True,
        )
        runtime._process_mode = True
        runtime._seed_process_graph(process, current)
        return runtime

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

    def _seed_process_graph(self, process: ProcessRevision, current: PlanRevision) -> None:
        """Copy an existing graph into local-key draft state without allocating IDs."""
        current_nodes = {str(node.plan_node_id): node for node in current.nodes}
        source_nodes = {node.plan_node_id: node for node in process.graph.nodes}
        self._nodes = {
            key: _DraftNode(
                key=key,
                title=node.title,
                instruction=node.instruction,
                kind=node.kind,
                required_capabilities=node.required_capabilities,
                session_policy=node.session_policy,
            )
            for key, node in current_nodes.items()
        }
        self._edges = {
            (
                str(edge.source_node_id),
                str(edge.target_node_id),
                edge.edge_type,
                None if edge.branch_id is None else str(edge.branch_id),
            ): _DraftEdge(
                source=str(edge.source_node_id),
                target=str(edge.target_node_id),
                edge_type=edge.edge_type,
                branch_key=None if edge.branch_id is None else str(edge.branch_id),
                condition=edge.condition,
            )
            for edge in current.edges
        }
        self._branches = {
            str(branch.branch_id): _DraftBranch(
                key=str(branch.branch_id),
                label=branch.label,
                fork=str(branch.fork_node_id),
                nodes=tuple(str(node_id) for node_id in branch.node_ids),
                merge=str(branch.merge_node_id),
            )
            for branch in current.branches
        }
        self._phases = {
            str(phase.phase_id): _DraftPhase(
                key=str(phase.phase_id),
                title=phase.title,
                nodes=tuple(str(node_id) for node_id in phase.node_ids),
                reviewer=str(phase.reviewer_node_id),
                gate=str(phase.gate_node_id),
                rework_nodes=tuple(str(node_id) for node_id in phase.rework_node_ids),
            )
            for phase in current.phases
        }
        retained_checks: dict[str, tuple[ID, ...]] = {}
        retained_owners: dict[ID, str] = {}
        for raw_gate_id, raw_owner_id in process.gate_owners.items():
            try:
                gate_id = normalize_id(raw_gate_id)
                owner_id = normalize_id(raw_owner_id)
            except ValueError as error:
                raise ValueError("Process Gate bindings must contain valid IDs") from error
            source_node = source_nodes.get(owner_id)
            owner_key = str(owner_id)
            if source_node is None or not source_node.required_check_ids:
                raise ValueError(
                    f"Process Gate {gate_id} has no frozen required Checks in its source graph"
                )
            if owner_key not in current_nodes:
                raise ValueError(f"Process Gate {gate_id} targets an unknown current node")
            if owner_key in retained_checks:
                raise ValueError(f"Process Gate owner {owner_key} is assigned more than once")
            retained_checks[owner_key] = tuple(source_node.required_check_ids)
            retained_owners[gate_id] = owner_key
        terminal_keys = self._terminal_node_keys()
        if len(terminal_keys) != 1:
            raise ValueError(
                "Process mode requires current graph to have exactly one terminal node"
            )
        final_gate_ids = tuple(
            gate_id
            for gate_id, owner_key in retained_owners.items()
            if owner_key == terminal_keys[0]
        )
        if len(final_gate_ids) != 1:
            raise ValueError(
                "Process mode requires the original final Gate to own the current terminal node"
            )
        self._retained_check_ids = retained_checks
        self._retained_gate_owners = retained_owners
        self._process_final_gate_id = final_gate_ids[0]
        self._node_gates = {}
        self._final_gate_argv = ()
        self._final_human_question = None

    def build_template(self) -> PlanTemplate:
        """Return the validated template after a successful finish_plan."""
        if self._finished_template is None:
            raise PlanGraphToolError("finish_plan must succeed before building a template")
        return self._finished_template

    def tool_definitions(self) -> tuple[ToolDefinition, ...]:
        order = _PROCESS_TOOL_ORDER if self._process_mode else _TOOL_ORDER
        return tuple(_TOOL_DEFINITIONS[name] for name in order)

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
            required_capabilities=_optional_capabilities(
                arguments, "required_capabilities", location
            ),
            session_policy=_optional_session_policy(arguments, "session_policy", location),
        )

    def _tool_update_plan_node(self, arguments: Mapping[str, JsonValue]) -> None:
        key = _required_key(arguments, "key", "nodes")
        location = f"nodes.{key}"
        node = self._nodes.get(key)
        if node is None:
            raise _ToolRejection(
                PlanIssue("UNKNOWN_NODE", location, f"Node {key!r} does not exist")
            )
        replacement = _DraftNode(
            key=node.key,
            title=_required_text(arguments, "title", location),
            instruction=_required_text(arguments, "instruction", location),
            kind=_required_node_kind(arguments, "kind", location),
            required_capabilities=_optional_capabilities(
                arguments,
                "required_capabilities",
                location,
                default=node.required_capabilities,
            ),
            session_policy=_optional_session_policy(
                arguments,
                "session_policy",
                location,
                default=node.session_policy,
            ),
        )
        if (
            self._process_mode
            and key in self._retained_check_ids
            and replacement.kind
            not in {
                PlanNodeKind.WORK,
                PlanNodeKind.MERGE,
                PlanNodeKind.REVIEWER,
            }
        ):
            raise _ToolRejection(
                PlanIssue(
                    "RETAINED_GATE_INVALID_KIND",
                    location,
                    "A retained process Gate owner must remain a work, merge, or reviewer node",
                    (key,),
                )
            )
        self._nodes[key] = replacement

    def _tool_remove_plan_node(self, arguments: Mapping[str, JsonValue]) -> None:
        key = _required_key(arguments, "key", "nodes")
        location = f"nodes.{key}"
        if key not in self._nodes:
            raise _ToolRejection(
                PlanIssue("UNKNOWN_NODE", location, f"Node {key!r} does not exist")
            )
        if self._process_mode and key in self._retained_check_ids:
            raise _ToolRejection(
                PlanIssue(
                    "NODE_GATE_MOVE_REQUIRED",
                    location,
                    "Move the retained process Gate before removing its owner node",
                    (key,),
                )
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
        phase_references = tuple(phase.key for phase in self._phases.values() if key in phase.nodes)
        if phase_references:
            raise _ToolRejection(
                PlanIssue(
                    "NODE_IN_USE",
                    location,
                    f"Node {key!r} is referenced by phases {phase_references}",
                    (key, *phase_references),
                )
            )
        del self._nodes[key]
        self._node_gates.pop(key, None)
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

    def _tool_set_plan_phase(self, arguments: Mapping[str, JsonValue]) -> None:
        phase_key = _required_key(arguments, "phase_key", "phases")
        location = f"phases.{phase_key}"
        existing = self._phases.get(phase_key)
        if arguments.get("remove") is True:
            if existing is None:
                raise _ToolRejection(
                    PlanIssue("UNKNOWN_PHASE", location, f"Phase {phase_key!r} does not exist")
                )
            del self._phases[phase_key]
            return
        title = _required_text(arguments, "title", location)
        node_keys = _required_node_keys(arguments, "node_keys", location)
        reviewer = _required_key(arguments, "reviewer_node_key", location)
        gate = _required_key(arguments, "gate_node_key", location)
        rework = _required_node_keys(arguments, "rework_node_keys", location)
        missing = tuple(
            key for key in (*node_keys, reviewer, gate, *rework) if key not in self._nodes
        )
        if missing:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_NODE",
                    location,
                    f"Phase references unknown node(s) {missing}",
                    missing,
                )
            )
        if reviewer not in node_keys or gate not in node_keys or reviewer != gate:
            raise _ToolRejection(
                PlanIssue(
                    "PHASE_GATE_INVALID",
                    location,
                    "Phase Reviewer and Gate must be the same node inside node_keys",
                    (phase_key, reviewer, gate),
                )
            )
        if self._nodes[reviewer].kind is not PlanNodeKind.REVIEWER:
            raise _ToolRejection(
                PlanIssue(
                    "PHASE_REVIEWER_INVALID",
                    location,
                    "reviewer_node_key must identify a reviewer node",
                    (phase_key, reviewer),
                )
            )
        if (
            reviewer in rework
            or not set(rework).issubset(node_keys)
            or any(
                self._nodes[key].kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}
                for key in rework
            )
        ):
            raise _ToolRejection(
                PlanIssue(
                    "PHASE_REWORK_INVALID",
                    location,
                    "Phase rework targets must be in-Phase work or merge nodes, not Reviewer",
                    (phase_key, *rework),
                )
            )
        self._phases[phase_key] = _DraftPhase(phase_key, title, node_keys, reviewer, gate, rework)

    def _tool_set_final_gate(self, arguments: Mapping[str, JsonValue]) -> None:
        if self._process_mode:
            raise _ToolRejection(
                PlanIssue(
                    "PROCESS_GATE_FROZEN",
                    "final_gate",
                    "set_final_gate is unavailable in process mode; move the retained Gate instead",
                )
            )
        argv = _required_gate_argv(arguments, "final_gate.argv")
        human_question = _optional_text(arguments, "human_question", "final_gate")
        if not argv and human_question is None:
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    "final_gate",
                    "final gate requires command argv or human_question",
                )
            )
        self._final_gate_argv = argv
        self._final_human_question = human_question

    def _tool_set_node_gate(self, arguments: Mapping[str, JsonValue]) -> None:
        if self._process_mode:
            raise _ToolRejection(
                PlanIssue(
                    "PROCESS_GATE_FROZEN",
                    "node_gates",
                    "set_node_gate is unavailable in process mode; move the retained Gate instead",
                )
            )
        node_key = _required_key(arguments, "node_key", "node_gates")
        location = f"node_gates.{node_key}"
        node = self._nodes.get(node_key)
        if node is None:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_NODE",
                    location,
                    f"Node {node_key!r} does not exist",
                    (node_key,),
                )
            )
        if node.kind not in {
            PlanNodeKind.WORK,
            PlanNodeKind.MERGE,
            PlanNodeKind.REVIEWER,
        }:
            raise _ToolRejection(
                PlanIssue(
                    "NODE_GATE_INVALID_KIND",
                    location,
                    "Node Gates may only be attached to work, merge, or reviewer nodes",
                    (node_key,),
                )
            )
        name = _required_text(arguments, "name", location)
        command_argv = _required_gate_argv(arguments, f"{location}.argv")
        human_question = _optional_text(arguments, "human_question", location)
        if not command_argv and human_question is None:
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    location,
                    "node Gate requires command argv or human_question",
                    (node_key,),
                )
            )
        self._node_gates[node_key] = _DraftNodeGate(
            node_key=node_key,
            name=name,
            command_argv=command_argv,
            human_question=human_question,
        )

    def _tool_move_process_gate(self, arguments: Mapping[str, JsonValue]) -> None:
        if not self._process_mode:
            raise _ToolRejection(
                PlanIssue(
                    "PROCESS_MODE_REQUIRED",
                    "move_process_gate",
                    "move_process_gate is available only in process mode",
                )
            )
        raw_gate_id = arguments.get("approved_gate_id")
        if not isinstance(raw_gate_id, str) or not raw_gate_id.strip():
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    "move_process_gate.approved_gate_id",
                    "approved_gate_id must be a non-blank UUID",
                )
            )
        try:
            approved_gate_id = normalize_id(raw_gate_id)
        except ValueError as error:
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_ARGUMENT",
                    "move_process_gate.approved_gate_id",
                    "approved_gate_id must be a valid UUID",
                )
            ) from error
        node_key = _required_key(arguments, "node_key", "move_process_gate")
        current_owner = self._retained_gate_owners.get(approved_gate_id)
        if current_owner is None:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_RETAINED_GATE",
                    "move_process_gate.approved_gate_id",
                    f"Retained process Gate {approved_gate_id} does not exist",
                    (str(approved_gate_id),),
                )
            )
        target = self._nodes.get(node_key)
        if target is None:
            raise _ToolRejection(
                PlanIssue(
                    "UNKNOWN_NODE",
                    "move_process_gate.node_key",
                    f"Node {node_key!r} does not exist",
                    (node_key,),
                )
            )
        if node_key == current_owner or node_key in self._retained_check_ids:
            raise _ToolRejection(
                PlanIssue(
                    "GATE_TARGET_OCCUPIED",
                    "move_process_gate.node_key",
                    "The target node already owns a retained process Gate",
                    (node_key,),
                )
            )
        if target.kind not in {
            PlanNodeKind.WORK,
            PlanNodeKind.MERGE,
            PlanNodeKind.REVIEWER,
        }:
            raise _ToolRejection(
                PlanIssue(
                    "RETAINED_GATE_INVALID_KIND",
                    "move_process_gate.node_key",
                    "A retained process Gate may move only to a work, merge, or reviewer node",
                    (node_key,),
                )
            )
        checks = self._retained_check_ids.get(current_owner)
        if checks is None:
            raise _ToolRejection(
                PlanIssue(
                    "RETAINED_GATE_CORRUPT",
                    "move_process_gate.approved_gate_id",
                    "The retained process Gate has no frozen Check group",
                    (str(approved_gate_id),),
                )
            )
        # Compute both replacements before assigning either mapping so a
        # rejected or malformed move cannot leave half of the binding changed.
        retained_checks = dict(self._retained_check_ids)
        retained_owners = dict(self._retained_gate_owners)
        del retained_checks[current_owner]
        retained_checks[node_key] = tuple(checks)
        retained_owners[approved_gate_id] = node_key
        self._retained_check_ids = retained_checks
        self._retained_gate_owners = retained_owners

    def _inspect(self) -> dict[str, JsonValue]:
        return {
            "nodes": [_node_document(node) for node in self._nodes.values()],
            "edges": [_edge_document(edge) for edge in self._edges.values()],
            "branches": [_branch_document(branch) for branch in self._branches.values()],
            "phases": [_phase_document(phase) for phase in self._phases.values()],
            "node_gates": [_node_gate_document(gate) for gate in self._node_gates.values()],
            "final_gate_argv": list(self._final_gate_argv),
            "final_human_question": self._final_human_question,
            "process_mode": self._process_mode,
            "retained_check_ids": {
                node_key: list(check_ids)
                for node_key, check_ids in self._retained_check_ids.items()
            },
            "retained_gate_owners": {
                str(gate_id): owner_key for gate_id, owner_key in self._retained_gate_owners.items()
            },
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
                "phases": len(self._phases),
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
                "phases": len(self._phases),
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
                        "The single terminal node must be a work, merge, or final reviewer node",
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
            if self._process_mode:
                issues.extend(self._validate_process_final_gate(terminal_keys[0]))
            else:
                final_gate = self._node_gates.get(terminal_keys[0])
                if final_gate is not None:
                    issues.append(
                        PlanIssue(
                            "NODE_GATE_FINAL_CONFLICT",
                            f"node_gates.{final_gate.node_key}",
                            "The final node uses the final Gate; it cannot also have a node Gate",
                            (final_gate.node_key,),
                        )
                    )
        for gate in self._node_gates.values():
            node = self._nodes.get(gate.node_key)
            if node is None:
                issues.append(
                    PlanIssue(
                        "NODE_GATE_UNKNOWN_NODE",
                        f"node_gates.{gate.node_key}",
                        f"Node Gate references unknown node {gate.node_key!r}",
                        (gate.node_key,),
                    )
                )
            elif node.kind not in {
                PlanNodeKind.WORK,
                PlanNodeKind.MERGE,
                PlanNodeKind.REVIEWER,
            }:
                issues.append(
                    PlanIssue(
                        "NODE_GATE_INVALID_KIND",
                        f"node_gates.{gate.node_key}",
                        "Node Gates may only be attached to work, merge, or reviewer nodes",
                        (gate.node_key,),
                    )
                )
        if (
            self._require_final_gate
            and not self._process_mode
            and not (self._final_gate_argv or self._final_human_question is not None)
        ):
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
            if node.kind is PlanNodeKind.REVIEWER:
                predecessors = tuple(
                    edge.source
                    for edge in self._edges.values()
                    if edge.target == node.key and edge.edge_type is EdgeType.DEPENDENCY
                )
                if not predecessors:
                    issues.append(
                        PlanIssue(
                            "REVIEWER_DEPENDENCY_REQUIRED",
                            f"nodes.{node.key}",
                            "A reviewer node must depend on the phase outputs it reviews",
                            (node.key,),
                        )
                    )
                is_final_reviewer = node.key in terminal_keys
                if not self._has_gate_owner(node.key) and not (
                    is_final_reviewer
                    and not self._process_mode
                    and (self._final_gate_argv or self._final_human_question is not None)
                ):
                    issues.append(
                        PlanIssue(
                            "REVIEWER_GATE_REQUIRED",
                            f"nodes.{node.key}",
                            "A reviewer node must own a node Gate, or the final Gate when it "
                            "is the final sink",
                            (node.key,),
                        )
                    )
        if self._branches:
            issues.extend(self._validate_exploration_structure())
        issues.extend(self._validate_phases())
        if self._process_mode:
            issues.extend(self._validate_retained_bindings())
        usage = self._usage()
        try:
            usage.require_within(self._budget)
        except ValueError as error:
            issues.append(PlanIssue("EXPLORATION_BUDGET_EXCEEDED", "graph", str(error)))
        return issues

    def _has_gate_owner(self, node_key: str) -> bool:
        return node_key in self._node_gates or node_key in self._retained_check_ids

    def _validate_process_final_gate(self, terminal_key: str) -> list[PlanIssue]:
        if self._process_final_gate_id is None:
            return [
                PlanIssue(
                    "FINAL_GATE_REQUIRED",
                    "final_gate",
                    "The original final Gate binding is missing in process mode",
                )
            ]
        owner = self._retained_gate_owners.get(self._process_final_gate_id)
        if owner != terminal_key:
            return [
                PlanIssue(
                    "FINAL_GATE_TERMINAL_REQUIRED",
                    "final_gate",
                    "The original final Gate must remain bound to the unique terminal node",
                    (terminal_key,),
                )
            ]
        return []

    def _validate_retained_bindings(self) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        owner_keys = set(self._retained_check_ids)
        bound_keys = set(self._retained_gate_owners.values())
        if owner_keys != bound_keys:
            issues.append(
                PlanIssue(
                    "RETAINED_GATE_BINDING_INCOMPLETE",
                    "retained_gate_owners",
                    "Retained Check and Gate bindings must cover the same node owners",
                )
            )
        seen_owners: set[str] = set()
        for gate_id, owner_key in self._retained_gate_owners.items():
            if owner_key in seen_owners:
                issues.append(
                    PlanIssue(
                        "RETAINED_GATE_OWNER_DUPLICATE",
                        f"retained_gate_owners.{gate_id}",
                        f"Retained Gate owner {owner_key!r} is assigned more than once",
                        (owner_key,),
                    )
                )
            seen_owners.add(owner_key)
            node = self._nodes.get(owner_key)
            if node is None:
                issues.append(
                    PlanIssue(
                        "RETAINED_GATE_UNKNOWN_NODE",
                        f"retained_gate_owners.{gate_id}",
                        f"Retained Gate owner {owner_key!r} is unknown",
                        (owner_key,),
                    )
                )
            elif node.kind not in {
                PlanNodeKind.WORK,
                PlanNodeKind.MERGE,
                PlanNodeKind.REVIEWER,
            }:
                issues.append(
                    PlanIssue(
                        "RETAINED_GATE_INVALID_KIND",
                        f"retained_gate_owners.{gate_id}",
                        "Retained Gate owners must be work, merge, or reviewer nodes",
                        (owner_key,),
                    )
                )
            checks = self._retained_check_ids.get(owner_key)
            if not checks:
                issues.append(
                    PlanIssue(
                        "RETAINED_CHECKS_MISSING",
                        f"retained_check_ids.{owner_key}",
                        "Every retained Gate owner must keep a non-empty frozen Check group",
                        (owner_key,),
                    )
                )
        for owner_key in owner_keys - bound_keys:
            if owner_key not in self._nodes:
                issues.append(
                    PlanIssue(
                        "RETAINED_CHECK_UNKNOWN_NODE",
                        f"retained_check_ids.{owner_key}",
                        f"Retained Check owner {owner_key!r} is unknown",
                        (owner_key,),
                    )
                )
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

    def _validate_phases(self) -> list[PlanIssue]:
        if not self._phases:
            return [
                PlanIssue(
                    "PHASES_REQUIRED",
                    "phases",
                    "The approved execution graph must define at least one Phase",
                )
            ]
        issues: list[PlanIssue] = []
        memberships: dict[str, int] = {}
        phase_index: dict[str, int] = {}
        for index, phase in enumerate(self._phases.values()):
            location = f"phases.{phase.key}"
            missing = tuple(key for key in phase.nodes if key not in self._nodes)
            if missing:
                issues.append(
                    PlanIssue(
                        "PHASE_UNKNOWN_NODE",
                        location,
                        f"Phase references unknown node(s) {missing}",
                        missing,
                    )
                )
                continue
            for node_key in phase.nodes:
                if node_key in memberships:
                    issues.append(
                        PlanIssue(
                            "PHASE_NODE_OVERLAP",
                            location,
                            f"Node {node_key!r} belongs to multiple Phases",
                            (node_key,),
                        )
                    )
                memberships[node_key] = index
                phase_index[node_key] = index
            reviewer = self._nodes.get(phase.reviewer)
            if reviewer is None or reviewer.kind is not PlanNodeKind.REVIEWER:
                issues.append(
                    PlanIssue(
                        "PHASE_REVIEWER_INVALID",
                        location,
                        "Phase reviewer_node_key must identify a reviewer node",
                        (phase.reviewer,),
                    )
                )
                continue
            invalid_rework = tuple(
                key
                for key in phase.rework_nodes
                if key not in phase.nodes
                or key not in self._nodes
                or self._nodes[key].kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}
            )
            if not phase.rework_nodes or invalid_rework:
                issues.append(
                    PlanIssue(
                        "PHASE_REWORK_INVALID",
                        location,
                        "Phase rework targets must be existing in-Phase work or merge nodes",
                        invalid_rework,
                    )
                )
            reachable = self._phase_reverse_reachable(phase.reviewer, set(phase.nodes))
            if reachable != set(phase.nodes):
                issues.append(
                    PlanIssue(
                        "PHASE_REVIEW_INCOMPLETE",
                        location,
                        "Every node in a Phase must feed its Reviewer Gate",
                        tuple(key for key in phase.nodes if key not in reachable),
                    )
                )
        omitted = tuple(key for key in self._nodes if key not in memberships)
        if omitted:
            issues.append(
                PlanIssue(
                    "PHASE_PARTITION_INCOMPLETE",
                    "phases",
                    f"Phases do not include nodes {omitted}",
                    omitted,
                )
            )
        for edge in self._edges.values():
            source_phase = phase_index.get(edge.source)
            target_phase = phase_index.get(edge.target)
            if (
                source_phase is not None
                and target_phase is not None
                and source_phase > target_phase
            ):
                issues.append(
                    PlanIssue(
                        "PHASE_BACK_EDGE",
                        f"edges.{edge.source}-to-{edge.target}",
                        "An edge cannot point back to an earlier Phase",
                        (edge.source, edge.target),
                    )
                )
            if (
                source_phase is not None
                and target_phase is not None
                and source_phase < target_phase
            ):
                source = tuple(self._phases.values())[source_phase]
                if edge.source != source.gate:
                    issues.append(
                        PlanIssue(
                            "PHASE_GATE_BYPASSED",
                            f"edges.{edge.source}-to-{edge.target}",
                            "Cross-Phase edges must originate at the source Phase Gate",
                            (edge.source, source.gate, edge.target),
                        )
                    )
        ordered = tuple(self._phases.values())
        for index, phase in enumerate(ordered[1:], start=1):
            phase_nodes = set(phase.nodes)
            internal_targets = {
                edge.target
                for edge in self._edges.values()
                if edge.source in phase_nodes and edge.target in phase_nodes
            }
            roots = phase_nodes - internal_targets
            previous_gate = ordered[index - 1].gate
            for root in roots:
                if not any(
                    edge.source == previous_gate
                    and edge.target == root
                    and edge.edge_type is EdgeType.DEPENDENCY
                    for edge in self._edges.values()
                ):
                    issues.append(
                        PlanIssue(
                            "PHASE_GATE_DEPENDENCY_REQUIRED",
                            f"phases.{phase.key}",
                            f"Phase root {root!r} must depend on the previous Phase Gate",
                            (previous_gate, root),
                        )
                    )
        return issues

    def _phase_reverse_reachable(self, target: str, allowed: set[str]) -> set[str]:
        incoming: dict[str, list[str]] = {key: [] for key in allowed}
        for edge in self._edges.values():
            if edge.source in allowed and edge.target in allowed:
                incoming[edge.target].append(edge.source)
        reached: set[str] = set()
        pending = [target]
        while pending:
            key = pending.pop()
            if key in reached:
                continue
            reached.add(key)
            pending.extend(incoming[key])
        return reached

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
                require_completion_checks=(
                    False if self._process_mode else node.key == final_node_key
                ),
                required_dependency_keys=tuple(dependencies.get(node.key, ())),
                required_capabilities=node.required_capabilities,
                session_policy=node.session_policy,
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
        phases = tuple(
            PhaseTemplate(
                key=phase.key,
                title=phase.title,
                node_keys=phase.nodes,
                reviewer_node_key=phase.reviewer,
                gate_node_key=phase.gate,
                rework_node_keys=phase.rework_nodes,
            )
            for phase in self._phases.values()
        )
        usage = self._usage()
        return PlanTemplate(
            nodes=nodes,
            edges=edges,
            branches=branches,
            phases=phases,
            node_gates=(
                ()
                if self._process_mode
                else tuple(
                    NodeGateTemplate(
                        node_key=gate.node_key,
                        name=gate.name,
                        command_argv=gate.command_argv,
                        human_question=gate.human_question,
                    )
                    for gate in self._node_gates.values()
                )
            ),
            budget=self._budget,
            usage=usage,
            planner_event_types=self._planner_event_types,
            final_node_key=final_node_key,
            final_gate_argv=() if self._process_mode else self._final_gate_argv,
            final_human_question=(None if self._process_mode else self._final_human_question),
            retained_check_ids=self._retained_check_ids,
            retained_gate_owners=self._retained_gate_owners,
        )


def _rejected(*issues: PlanIssue) -> dict[str, JsonValue]:
    return {"accepted": False, "issues": [issue.to_dict() for issue in issues]}


def _node_document(node: _DraftNode) -> dict[str, JsonValue]:
    return {
        "key": node.key,
        "title": node.title,
        "instruction": node.instruction,
        "kind": node.kind.value,
        "required_capabilities": cast(
            JsonValue, sorted(item.name for item in node.required_capabilities)
        ),
        "session_policy": node.session_policy.value,
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


def _phase_document(phase: _DraftPhase) -> dict[str, JsonValue]:
    return {
        "phase_key": phase.key,
        "title": phase.title,
        "node_keys": list(phase.nodes),
        "reviewer_node_key": phase.reviewer,
        "gate_node_key": phase.gate,
        "rework_node_keys": list(phase.rework_nodes),
    }


def _node_gate_document(gate: _DraftNodeGate) -> dict[str, JsonValue]:
    return {
        "node_key": gate.node_key,
        "name": gate.name,
        "command_argv": list(gate.command_argv),
        "human_question": gate.human_question,
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


def _optional_capabilities(
    arguments: Mapping[str, JsonValue],
    name: str,
    location: str,
    *,
    default: frozenset[WorkerCapability] = frozenset(),
) -> frozenset[WorkerCapability]:
    if name not in arguments or arguments[name] is None:
        return default
    value = arguments.get(name, [])
    if not isinstance(value, list):
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT",
                f"{location}.{name}",
                f"{name} must be an array of capability names",
            )
        )
    if len(value) > 32:
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT",
                f"{location}.{name}",
                f"{name} must contain at most 32 capability names",
            )
        )
    capabilities: list[WorkerCapability] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 128:
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_CAPABILITY",
                    f"{location}.{name}[{index}]",
                    "capability names must be non-blank strings of at most 128 characters",
                )
            )
        try:
            capabilities.append(WorkerCapability(item.strip()))
        except ValueError as error:
            raise _ToolRejection(
                PlanIssue(
                    "INVALID_CAPABILITY",
                    f"{location}.{name}[{index}]",
                    str(error),
                )
            ) from error
    if len(set(capabilities)) != len(capabilities):
        raise _ToolRejection(
            PlanIssue(
                "DUPLICATE_CAPABILITY",
                f"{location}.{name}",
                "capability names must be unique",
            )
        )
    return frozenset(capabilities)


def _optional_session_policy(
    arguments: Mapping[str, JsonValue],
    name: str,
    location: str,
    *,
    default: SessionPolicy = SessionPolicy.NEW,
) -> SessionPolicy:
    if name not in arguments or arguments[name] is None:
        return default
    value = arguments.get(name, SessionPolicy.NEW.value)
    if not isinstance(value, str) or value not in _SESSION_POLICIES:
        raise _ToolRejection(
            PlanIssue(
                "INVALID_SESSION_POLICY",
                f"{location}.{name}",
                f"{name} must be one of {list(_SESSION_POLICIES)}; use 'new' unless "
                "the selected Worker preset explicitly supports another policy",
            )
        )
    return SessionPolicy(value)


def _required_gate_argv(
    arguments: Mapping[str, JsonValue],
    location: str,
) -> tuple[str, ...]:
    value = arguments.get("argv")
    if not isinstance(value, list):
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT",
                location,
                "argv must be an array of command arguments",
            )
        )
    if len(value) > 128:
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT",
                location,
                "argv must contain at most 128 command arguments",
            )
        )
    if any(not isinstance(argument, str) for argument in value):
        raise _ToolRejection(
            PlanIssue("INVALID_ARGUMENT", location, "argv must contain only strings")
        )
    argv = tuple(argument for argument in value if isinstance(argument, str))
    if any(not argument or len(argument) > 4096 or "\x00" in argument for argument in argv):
        raise _ToolRejection(
            PlanIssue(
                "INVALID_ARGUMENT",
                location,
                "argv arguments must be non-empty, at most 4096 characters, and contain no "
                "null bytes",
            )
        )
    return argv


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
_GATE_ARG_PROPERTY: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 4096,
}
_HUMAN_QUESTION_PROPERTY: dict[str, JsonValue] = {
    "type": ["string", "null"],
    "minLength": 1,
    "maxLength": 4000,
}
_CAPABILITY_PROPERTY: dict[str, JsonValue] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
}

_NODE_PARAMETERS: dict[str, JsonValue] = {
    "key": _KEY_PROPERTY,
    "title": _LABEL_PROPERTY,
    "instruction": _INSTRUCTION_PROPERTY,
    "kind": _NODE_KIND_PROPERTY,
    "required_capabilities": {
        "type": ["array", "null"],
        "items": _CAPABILITY_PROPERTY,
        "maxItems": 32,
    },
    "session_policy": {"type": ["string", "null"], "enum": [*_SESSION_POLICIES, None]},
}

_TOOL_ORDER = (
    "add_plan_node",
    "update_plan_node",
    "remove_plan_node",
    "add_plan_edge",
    "remove_plan_edge",
    "set_plan_branch",
    "set_plan_phase",
    "set_node_gate",
    "set_final_gate",
    "inspect_plan",
    "finish_plan",
)
_PROCESS_TOOL_ORDER = (*_TOOL_ORDER[:-2], "move_process_gate", *_TOOL_ORDER[-2:])

_TOOL_DEFINITIONS: dict[str, ToolDefinition] = {
    "add_plan_node": ToolDefinition(
        "add_plan_node",
        "Add one plan node with a stable local key, title, instruction, kind, optional "
        "required capabilities, and optional Session policy. Omitted values mean no required "
        "capabilities and 'new'; send null for these defaults. Capabilities are routing "
        "requirements, not new Tool "
        "permissions; Session policy must match a selected Worker preset and does not itself "
        "create shared Phase Session behavior.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "key",
                "title",
                "instruction",
                "kind",
                "required_capabilities",
                "session_policy",
            ],
            "properties": _NODE_PARAMETERS,
        },
    ),
    "update_plan_node": ToolDefinition(
        "update_plan_node",
        "Replace the title, instruction, kind, and any supplied required capabilities or "
        "Session policy of an existing plan node. Omitted optional values preserve the existing "
        "node settings; send null to preserve them, or [] to clear capabilities. "
        "Capabilities are routing requirements, not new Tool permissions; "
        "Session policy must match a selected Worker preset and does not itself create shared "
        "Phase Session behavior.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "key",
                "title",
                "instruction",
                "kind",
                "required_capabilities",
                "session_policy",
            ],
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
                },
                "merge_node_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
                "remove": {"type": "boolean"},
            },
        },
    ),
    "set_plan_phase": ToolDefinition(
        "set_plan_phase",
        "Create, replace, or remove one ordered execution Phase ending in a Reviewer Gate.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "phase_key",
                "title",
                "node_keys",
                "reviewer_node_key",
                "gate_node_key",
                "rework_node_keys",
                "remove",
            ],
            "properties": {
                "phase_key": _KEY_PROPERTY,
                "title": {**_LABEL_PROPERTY, "type": ["string", "null"]},
                "node_keys": {
                    "type": ["array", "null"],
                    "items": _KEY_PROPERTY,
                    "minItems": 1,
                },
                "reviewer_node_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
                "gate_node_key": {**_KEY_PROPERTY, "type": ["string", "null"]},
                "rework_node_keys": {
                    "type": ["array", "null"],
                    "items": _KEY_PROPERTY,
                    "minItems": 1,
                },
                "remove": {"type": "boolean"},
            },
        },
    ),
    "set_final_gate": ToolDefinition(
        "set_final_gate",
        "Set the final Gate with command argv, a human question, or both.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["argv", "human_question"],
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 128,
                    "items": _GATE_ARG_PROPERTY,
                },
                "human_question": _HUMAN_QUESTION_PROPERTY,
            },
        },
    ),
    "set_node_gate": ToolDefinition(
        "set_node_gate",
        "Set or replace one Gate on a non-final work, merge, or reviewer node.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_key", "name", "argv", "human_question"],
            "properties": {
                "node_key": _KEY_PROPERTY,
                "name": _LABEL_PROPERTY,
                "argv": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 128,
                    "items": _GATE_ARG_PROPERTY,
                },
                "human_question": _HUMAN_QUESTION_PROPERTY,
            },
        },
    ),
    "move_process_gate": ToolDefinition(
        "move_process_gate",
        "Move one retained approved Gate as a whole to an existing ungated work, merge, or "
        "reviewer node. The approved Gate ID and its Check IDs remain unchanged; this does not "
        "create, delete, or edit Checks.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["approved_gate_id", "node_key"],
            "properties": {
                "approved_gate_id": {
                    "type": "string",
                    "pattern": (
                        r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
                    ),
                },
                "node_key": _KEY_PROPERTY,
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
