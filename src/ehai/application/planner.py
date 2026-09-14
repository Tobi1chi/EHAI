"""Planner Port and deterministic single-node I3 implementation."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ehai import ID, JsonValue, new_id, normalize_id, utc_now
from ehai.domain.checking import CheckKind, CheckRunStatus, CheckSpec
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import CompletionContract, Goal, GoalStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanPhase,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.process import ProcessRevision
from ehai.domain.workers import SessionPolicy, WorkerCapability

if TYPE_CHECKING:
    from ehai.application.process_review import ProcessBoundaryReport
    from ehai.application.process_review_context import ProcessReviewContext

NON_EMPTY_ARTIFACT_CRITERION = "artifact:non-empty"
COMMAND_EXIT_ZERO_CRITERION = "command:exit-zero"
SEMANTIC_REQUIRED_TERMS_CRITERION = "semantic:required-terms"
HUMAN_CRITERION_PREFIX = "human:"
P1_COMPLETION_CRITERIA = frozenset(
    {
        NON_EMPTY_ARTIFACT_CRITERION,
        COMMAND_EXIT_ZERO_CRITERION,
        SEMANTIC_REQUIRED_TERMS_CRITERION,
    }
)
MAX_REPLAN_ATTEMPT_SUMMARIES = 20
MAX_REPLAN_CHECK_SUMMARIES = 20
MAX_REPLAN_CHECKPOINT_ARTIFACT_IDS = 50
MAX_REPLAN_INTERVENTION_SUMMARIES = 10


def check_spec_document(check: CheckSpec) -> dict[str, JsonValue]:
    """Serialize one stored CheckSpec without applying host configuration defaults."""
    return {
        "check_id": check.check_id,
        "name": check.name,
        "kind": check.kind.value,
        "description": check.description,
        "required": check.required,
        "command_argv": list(check.command_argv),
        "semantic_required_terms": list(check.semantic_required_terms),
    }


@dataclass(frozen=True, slots=True)
class ReplanAttemptSummary:
    """Bounded provider-neutral facts about one source Run Attempt."""

    attempt_id: ID
    plan_node_id: ID
    sequence: int
    status: AttemptStatus
    reason: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "plan_node_id": self.plan_node_id,
            "sequence": self.sequence,
            "status": self.status.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReplanCheckSummary:
    """Bounded provider-neutral facts about one failed source Run Check."""

    check_run_id: ID
    check_id: ID
    plan_node_id: ID
    attempt_id: ID
    status: CheckRunStatus
    passed: bool | None
    reason: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "check_run_id": self.check_run_id,
            "check_id": self.check_id,
            "plan_node_id": self.plan_node_id,
            "attempt_id": self.attempt_id,
            "status": self.status.value,
            "passed": self.passed,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReplanCheckpointSummary:
    """Latest durable recovery boundary available to a replanning source Run."""

    checkpoint_id: ID
    event_offset: int
    artifact_ids: tuple[ID, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "event_offset": self.event_offset,
            "artifact_ids": list(self.artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class ReplanInterventionSummary:
    """A retained question and user reply, not approval to change the contract."""

    intervention_id: ID
    plan_node_id: ID
    attempt_id: ID
    source_process_revision_id: ID | None
    kind: str
    status: str
    reason: str | None
    evidence: str | None
    needed: str | None
    reply: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "intervention_id": self.intervention_id,
            "plan_node_id": self.plan_node_id,
            "attempt_id": self.attempt_id,
            "source_process_revision_id": self.source_process_revision_id,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "evidence": self.evidence,
            "needed": self.needed,
            "reply": self.reply,
        }


@dataclass(frozen=True, slots=True)
class ReplanContext:
    """Auditable failure evidence supplied to a Planner without transcript content."""

    source_run_id: ID
    source_run_status: RunStatus
    source_run_reason: str | None
    failed_plan_node_ids: tuple[ID, ...]
    attempts: tuple[ReplanAttemptSummary, ...]
    failed_checks: tuple[ReplanCheckSummary, ...]
    consumed_attempt_count: int
    latest_checkpoint: ReplanCheckpointSummary | None = None
    blocked_plan_node_ids: tuple[ID, ...] = ()
    interventions: tuple[ReplanInterventionSummary, ...] = ()
    intervention_count: int = 0
    approved_checks: tuple[CheckSpec, ...] = ()
    approved_design_document: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_run_id": self.source_run_id,
            "source_run_status": self.source_run_status.value,
            "source_run_reason": self.source_run_reason,
            "failed_plan_node_ids": list(self.failed_plan_node_ids),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "failed_checks": [check.to_dict() for check in self.failed_checks],
            "consumed_attempt_count": self.consumed_attempt_count,
            "blocked_plan_node_ids": list(self.blocked_plan_node_ids),
            "interventions": [item.to_dict() for item in self.interventions],
            "intervention_count": self.intervention_count,
            "approved_checks": [check_spec_document(item) for item in self.approved_checks],
            "latest_checkpoint": (
                None if self.latest_checkpoint is None else self.latest_checkpoint.to_dict()
            ),
            "approved_design_document": self.approved_design_document,
        }


@dataclass(frozen=True, slots=True)
class ExplorationBudget:
    """Hard limits for one bounded exploration proposal."""

    max_attempts: int
    max_width: int = 3
    max_depth: int = 2

    def __post_init__(self) -> None:
        for field_name in ("max_width", "max_depth", "max_attempts"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(f"ExplorationBudget {field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ExplorationUsage:
    """Planned exploration consumption before execution begins."""

    width: int
    depth: int
    attempts: int

    def __post_init__(self) -> None:
        for field_name in ("width", "depth", "attempts"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"ExplorationUsage {field_name} must be a non-negative integer")

    def require_within(self, budget: ExplorationBudget) -> None:
        """Fail closed when planned consumption exceeds any hard limit."""
        if self.width > budget.max_width:
            raise ValueError(f"exploration width {self.width} exceeds budget {budget.max_width}")
        if self.depth > budget.max_depth:
            raise ValueError(f"exploration depth {self.depth} exceeds budget {budget.max_depth}")
        if self.attempts > budget.max_attempts:
            raise ValueError(
                f"exploration attempts {self.attempts} exceeds budget {budget.max_attempts}"
            )


@dataclass(frozen=True, slots=True)
class ExplorationPlanRequest:
    """Immutable input for one deterministic nonlinear proposal."""

    goal: Goal
    criteria: tuple[str, ...]
    budget: ExplorationBudget

    def __post_init__(self) -> None:
        if not isinstance(self.goal, Goal):
            raise TypeError("ExplorationPlanRequest goal must be a Goal")
        if not isinstance(self.budget, ExplorationBudget):
            raise TypeError("ExplorationPlanRequest budget must be an ExplorationBudget")
        criteria = tuple(criterion.strip() for criterion in self.criteria)
        if not criteria or any(not criterion for criterion in criteria):
            raise ValueError("ExplorationPlanRequest criteria must not be empty")
        object.__setattr__(self, "criteria", criteria)


@dataclass(frozen=True, slots=True)
class PlanNodeTemplate:
    """Provider-neutral PlanNode content before EHAI IDs and Check IDs are assigned."""

    key: str
    title: str
    instruction: str
    kind: PlanNodeKind = PlanNodeKind.WORK
    required_dependency_keys: tuple[str, ...] = ()
    require_completion_checks: bool = True
    required_capabilities: frozenset[WorkerCapability] = frozenset()
    session_policy: SessionPolicy = SessionPolicy.NEW

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _template_key(self.key, "PlanNodeTemplate key"))
        object.__setattr__(self, "kind", PlanNodeKind(self.kind))
        dependencies = tuple(
            _template_key(value, "PlanNodeTemplate dependency key")
            for value in self.required_dependency_keys
        )
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(f"PlanNodeTemplate {self.key} contains duplicate dependencies")
        if self.key in dependencies:
            raise ValueError(f"PlanNodeTemplate {self.key} cannot depend on itself")
        if not self.title.strip() or not self.instruction.strip():
            raise ValueError(f"PlanNodeTemplate {self.key} requires title and instruction")
        if not isinstance(self.require_completion_checks, bool):
            raise ValueError("PlanNodeTemplate require_completion_checks must be a boolean")
        capabilities = frozenset(self.required_capabilities)
        if not all(isinstance(item, WorkerCapability) for item in capabilities):
            raise ValueError(
                f"PlanNodeTemplate {self.key} required_capabilities must contain "
                "WorkerCapability values"
            )
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "instruction", self.instruction.strip())
        object.__setattr__(self, "required_dependency_keys", dependencies)
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(self, "session_policy", SessionPolicy(self.session_policy))


@dataclass(frozen=True, slots=True)
class EdgeTemplate:
    """Provider-neutral edge between keyed PlanNode templates."""

    source_node_key: str
    target_node_key: str
    edge_type: EdgeType
    branch_key: str | None = None
    condition: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_node_key",
            _template_key(self.source_node_key, "EdgeTemplate source_node_key"),
        )
        object.__setattr__(
            self,
            "target_node_key",
            _template_key(self.target_node_key, "EdgeTemplate target_node_key"),
        )
        object.__setattr__(self, "edge_type", EdgeType(self.edge_type))
        if self.branch_key is not None:
            object.__setattr__(
                self,
                "branch_key",
                _template_key(self.branch_key, "EdgeTemplate branch_key"),
            )
        if self.condition is not None and not self.condition.strip():
            raise ValueError("EdgeTemplate condition must be non-blank when provided")


@dataclass(frozen=True, slots=True)
class BranchTemplate:
    """Provider-neutral exploration branch before EHAI IDs are assigned."""

    key: str
    label: str
    fork_node_key: str
    node_keys: tuple[str, ...]
    merge_node_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _template_key(self.key, "BranchTemplate key"))
        object.__setattr__(
            self,
            "fork_node_key",
            _template_key(self.fork_node_key, "BranchTemplate fork_node_key"),
        )
        object.__setattr__(
            self,
            "merge_node_key",
            _template_key(self.merge_node_key, "BranchTemplate merge_node_key"),
        )
        nodes = tuple(_template_key(value, "BranchTemplate node_key") for value in self.node_keys)
        if not nodes:
            raise ValueError(f"BranchTemplate {self.key} requires at least one node")
        if len(set(nodes)) != len(nodes):
            raise ValueError(f"BranchTemplate {self.key} contains duplicate node keys")
        if self.fork_node_key == self.merge_node_key:
            raise ValueError(f"BranchTemplate {self.key} fork and merge keys must differ")
        if self.fork_node_key in nodes or self.merge_node_key in nodes:
            raise ValueError(f"BranchTemplate {self.key} cannot include fork or merge nodes")
        if not self.label.strip():
            raise ValueError(f"BranchTemplate {self.key} requires a label")
        object.__setattr__(self, "label", self.label.strip())
        object.__setattr__(self, "node_keys", nodes)


@dataclass(frozen=True, slots=True)
class PhaseTemplate:
    """Provider-neutral phase structure before persistent IDs are assigned."""

    key: str
    title: str
    node_keys: tuple[str, ...]
    reviewer_node_key: str
    gate_node_key: str
    rework_node_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _template_key(self.key, "PhaseTemplate key"))
        title = self.title.strip()
        if not title:
            raise ValueError(f"PhaseTemplate {self.key} requires a title")
        object.__setattr__(self, "title", title)
        nodes = tuple(_template_key(value, "PhaseTemplate node key") for value in self.node_keys)
        if not nodes or len(set(nodes)) != len(nodes):
            raise ValueError(f"PhaseTemplate {self.key} requires unique node keys")
        object.__setattr__(self, "node_keys", nodes)
        object.__setattr__(
            self,
            "reviewer_node_key",
            _template_key(self.reviewer_node_key, "PhaseTemplate reviewer node key"),
        )
        object.__setattr__(
            self,
            "gate_node_key",
            _template_key(self.gate_node_key, "PhaseTemplate Gate node key"),
        )
        if self.reviewer_node_key not in nodes or self.gate_node_key not in nodes:
            raise ValueError(f"PhaseTemplate {self.key} Reviewer and Gate must belong to the Phase")
        if self.reviewer_node_key != self.gate_node_key:
            raise ValueError(f"PhaseTemplate {self.key} Gate must be owned by its Reviewer")
        rework = tuple(
            _template_key(value, "PhaseTemplate rework node key") for value in self.rework_node_keys
        )
        if not rework or len(set(rework)) != len(rework) or not set(rework).issubset(nodes):
            raise ValueError(f"PhaseTemplate {self.key} requires unique in-Phase rework nodes")
        if self.reviewer_node_key in rework:
            raise ValueError(f"PhaseTemplate {self.key} Reviewer cannot be a rework target")
        object.__setattr__(self, "rework_node_keys", rework)


@dataclass(frozen=True, slots=True)
class NodeGateTemplate:
    """A local behavioral gate bound to one intermediate execution node."""

    node_key: str
    name: str
    command_argv: tuple[str, ...] = ()
    human_question: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "node_key", _template_key(self.node_key, "NodeGateTemplate node_key")
        )
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("NodeGateTemplate requires a non-blank name")
        argv = _normalize_argv(self.command_argv, "NodeGateTemplate command_argv")
        question = _optional_template_text(
            self.human_question,
            "NodeGateTemplate human_question",
        )
        if not argv and question is None:
            raise ValueError("NodeGateTemplate requires command_argv or human_question")
        if len(argv) > 128 or any(len(argument) > 4096 for argument in argv):
            raise ValueError("NodeGateTemplate command_argv must contain at most 128 arguments")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "command_argv", argv)
        object.__setattr__(self, "human_question", question)


@dataclass(frozen=True, slots=True)
class PlanTemplate:
    """Provider-neutral proposal content assembled into EHAI domain objects."""

    nodes: tuple[PlanNodeTemplate, ...]
    edges: tuple[EdgeTemplate, ...] = ()
    branches: tuple[BranchTemplate, ...] = ()
    phases: tuple[PhaseTemplate, ...] = ()
    budget: ExplorationBudget | None = None
    usage: ExplorationUsage | None = None
    planner_diagnostics: tuple[str, ...] = ()
    planner_event_types: tuple[str, ...] = ()
    design_document: str | None = None
    final_node_key: str | None = None
    final_gate_argv: tuple[str, ...] = ()
    node_gates: tuple[NodeGateTemplate, ...] = ()
    final_human_question: str | None = None
    retained_check_ids: Mapping[str, tuple[ID, ...]] = field(default_factory=dict)
    retained_gate_owners: Mapping[ID, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        branches = tuple(self.branches)
        phases = tuple(self.phases)
        if not nodes:
            raise ValueError("PlanTemplate requires at least one node")
        node_keys = tuple(node.key for node in nodes)
        if len(set(node_keys)) != len(node_keys):
            raise ValueError("PlanTemplate contains duplicate node keys")
        branch_keys = tuple(branch.key for branch in branches)
        if len(set(branch_keys)) != len(branch_keys):
            raise ValueError("PlanTemplate contains duplicate branch keys")
        known_nodes = set(node_keys)
        known_branches = set(branch_keys)
        phase_keys = tuple(phase.key for phase in phases)
        if len(set(phase_keys)) != len(phase_keys):
            raise ValueError("PlanTemplate contains duplicate phase keys")
        for node in nodes:
            if any(key not in known_nodes for key in node.required_dependency_keys):
                raise ValueError(f"PlanTemplate node {node.key} references an unknown dependency")
        for edge in edges:
            if edge.source_node_key not in known_nodes or edge.target_node_key not in known_nodes:
                raise ValueError("PlanTemplate edge references an unknown node")
            if edge.branch_key is not None and edge.branch_key not in known_branches:
                raise ValueError("PlanTemplate edge references an unknown branch")
        for branch in branches:
            referenced = {branch.fork_node_key, branch.merge_node_key, *branch.node_keys}
            if not referenced.issubset(known_nodes):
                raise ValueError(f"PlanTemplate branch {branch.key} references an unknown node")
        phase_memberships = tuple(node_key for phase in phases for node_key in phase.node_keys)
        if phases and (
            set(phase_memberships) != known_nodes or len(phase_memberships) != len(known_nodes)
        ):
            raise ValueError("PlanTemplate phases must partition every node exactly once")
        for phase in phases:
            if not set(phase.node_keys).issubset(known_nodes):
                raise ValueError(f"PlanTemplate phase {phase.key} references an unknown node")
            reviewer = next(node for node in nodes if node.key == phase.reviewer_node_key)
            if reviewer.kind is not PlanNodeKind.REVIEWER:
                raise ValueError(f"PlanTemplate phase {phase.key} Reviewer has the wrong kind")
            if any(
                nodes_by_key.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}
                for nodes_by_key in nodes
                if nodes_by_key.key in phase.rework_node_keys
            ):
                raise ValueError(
                    f"PlanTemplate phase {phase.key} rework targets must be work or merge nodes"
                )
        final_node_key = self.final_node_key
        if final_node_key is not None:
            final_node_key = _template_key(final_node_key, "PlanTemplate final_node_key")
            if final_node_key not in known_nodes:
                raise ValueError(
                    f"PlanTemplate final_node_key {final_node_key!r} references an unknown node"
                )
            terminal_keys = tuple(
                key for key in node_keys if not any(edge.source_node_key == key for edge in edges)
            )
            if terminal_keys != (final_node_key,):
                raise ValueError(
                    "PlanTemplate final_node_key must be the graph's single terminal node"
                )
            if any(node.require_completion_checks for node in nodes if node.key != final_node_key):
                raise ValueError(
                    "Intermediate nodes cannot require final completion checks; use node_gates"
                )
        node_gates = tuple(self.node_gates)
        if any(not isinstance(gate, NodeGateTemplate) for gate in node_gates):
            raise TypeError("PlanTemplate node_gates must contain NodeGateTemplate values")
        if node_gates and final_node_key is None:
            raise ValueError("PlanTemplate node_gates require an explicit final_node_key")
        gate_nodes = tuple(gate.node_key for gate in node_gates)
        if len(set(gate_nodes)) != len(gate_nodes):
            raise ValueError("PlanTemplate contains multiple gates for the same node")
        nodes_by_key = {node.key: node for node in nodes}
        retained_check_ids = _retained_check_bindings(
            self.retained_check_ids,
            nodes_by_key,
        )
        retained_gate_owners = _retained_gate_bindings(
            self.retained_gate_owners,
            nodes_by_key,
        )
        if set(retained_check_ids) != set(retained_gate_owners.values()):
            raise ValueError(
                "PlanTemplate retained Check and Gate bindings must cover the same owners"
            )
        final_gate_argv = _normalize_argv(
            self.final_gate_argv,
            "PlanTemplate final_gate_argv",
        )
        final_human_question = _optional_template_text(
            self.final_human_question,
            "PlanTemplate final_human_question",
        )
        for gate in node_gates:
            owner = nodes_by_key.get(gate.node_key)
            if owner is None or owner.kind not in {
                PlanNodeKind.WORK,
                PlanNodeKind.MERGE,
                PlanNodeKind.REVIEWER,
            }:
                raise ValueError(
                    f"Gate {gate.name!r} requires an existing work, merge, or reviewer node"
                )
            if gate.node_key == final_node_key:
                raise ValueError("Use final_gate_argv for the final node, not node_gates")
        for node in nodes:
            if node.kind is PlanNodeKind.REVIEWER and (
                not node.required_dependency_keys
                or (
                    node.key not in gate_nodes
                    and node.key not in retained_gate_owners.values()
                    and not (
                        node.key == final_node_key
                        and (final_gate_argv or final_human_question is not None)
                    )
                )
            ):
                raise ValueError(
                    f"Reviewer node {node.key!r} requires dependencies and its own node Gate"
                )
        object.__setattr__(self, "node_gates", node_gates)
        if (self.budget is None) != (self.usage is None):
            raise ValueError("PlanTemplate budget and usage must be provided together")
        if self.budget is not None and self.usage is not None:
            self.usage.require_within(self.budget)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "branches", branches)
        object.__setattr__(self, "phases", phases)
        object.__setattr__(self, "final_node_key", final_node_key)
        object.__setattr__(self, "final_gate_argv", final_gate_argv)
        object.__setattr__(self, "final_human_question", final_human_question)
        object.__setattr__(self, "retained_check_ids", retained_check_ids)
        object.__setattr__(self, "retained_gate_owners", retained_gate_owners)
        object.__setattr__(
            self,
            "planner_diagnostics",
            _planner_messages(self.planner_diagnostics, "planner_diagnostics"),
        )
        object.__setattr__(
            self,
            "planner_event_types",
            _planner_messages(self.planner_event_types, "planner_event_types"),
        )


def _retained_check_bindings(
    values: Mapping[str, tuple[ID, ...]],
    nodes_by_key: Mapping[str, PlanNodeTemplate],
) -> Mapping[str, tuple[ID, ...]]:
    if not isinstance(values, Mapping):
        raise TypeError("PlanTemplate retained_check_ids must be a mapping")
    normalized: dict[str, tuple[ID, ...]] = {}
    for raw_key, raw_check_ids in values.items():
        if not isinstance(raw_key, str):
            raise ValueError("PlanTemplate retained Check owner keys must be strings")
        key = _template_key(raw_key, "PlanTemplate retained Check owner")
        if key in normalized:
            raise ValueError(f"PlanTemplate contains duplicate retained Check owner {key!r}")
        if key not in nodes_by_key:
            raise ValueError(f"PlanTemplate retained Check owner {key!r} is unknown")
        try:
            check_ids = tuple(normalize_id(check_id) for check_id in raw_check_ids)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"PlanTemplate retained Check IDs for {key!r} must be valid IDs"
            ) from error
        if not check_ids:
            raise ValueError(f"PlanTemplate retained Check owner {key!r} requires Check IDs")
        if len(set(check_ids)) != len(check_ids):
            raise ValueError(f"PlanTemplate retained Check IDs for {key!r} must be unique")
        normalized[key] = check_ids
    return MappingProxyType(normalized)


def _retained_gate_bindings(
    values: Mapping[ID, str],
    nodes_by_key: Mapping[str, PlanNodeTemplate],
) -> Mapping[ID, str]:
    if not isinstance(values, Mapping):
        raise TypeError("PlanTemplate retained_gate_owners must be a mapping")
    normalized: dict[ID, str] = {}
    owner_by_key: dict[str, ID] = {}
    allowed_kinds = {PlanNodeKind.WORK, PlanNodeKind.MERGE, PlanNodeKind.REVIEWER}
    for raw_gate_id, raw_owner_key in values.items():
        try:
            gate_id = normalize_id(raw_gate_id)
        except (TypeError, ValueError) as error:
            raise ValueError("PlanTemplate retained Gate IDs must be valid IDs") from error
        if gate_id in normalized:
            raise ValueError(f"PlanTemplate contains duplicate retained Gate ID {gate_id}")
        if not isinstance(raw_owner_key, str):
            raise ValueError("PlanTemplate retained Gate owner keys must be strings")
        owner_key = _template_key(raw_owner_key, "PlanTemplate retained Gate owner")
        owner = nodes_by_key.get(owner_key)
        if owner is None:
            raise ValueError(f"PlanTemplate retained Gate owner {owner_key!r} is unknown")
        if owner.kind not in allowed_kinds:
            raise ValueError(
                f"PlanTemplate retained Gate owner {owner_key!r} must be a work, merge, "
                "or reviewer node"
            )
        if owner_key in owner_by_key:
            raise ValueError(
                f"PlanTemplate retained Gate owner {owner_key!r} is assigned more than once"
            )
        normalized[gate_id] = owner_key
        owner_by_key[owner_key] = gate_id
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A Planner's unapproved graph, contract, and referenced Check definitions."""

    contract: CompletionContract
    plan_revision: PlanRevision
    check_specs: tuple[CheckSpec, ...]
    budget: ExplorationBudget | None = None
    usage: ExplorationUsage | None = None
    planner_diagnostics: tuple[str, ...] = ()
    planner_event_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_specs", tuple(self.check_specs))
        object.__setattr__(
            self,
            "planner_diagnostics",
            _planner_messages(self.planner_diagnostics, "planner_diagnostics"),
        )
        object.__setattr__(
            self,
            "planner_event_types",
            _planner_messages(self.planner_event_types, "planner_event_types"),
        )
        owner = f"PlanProposal {self.plan_revision.plan_revision_id}"
        if self.contract.is_confirmed:
            raise ValueError(f"{owner} contract must remain unconfirmed")
        if self.plan_revision.status is not PlanRevisionStatus.DRAFT:
            raise ValueError(f"{owner} PlanRevision must remain a draft")
        if (
            self.plan_revision.goal_id != self.contract.goal_id
            or self.plan_revision.completion_contract_id != self.contract.completion_contract_id
            or self.plan_revision.completion_contract_version != self.contract.version
        ):
            raise ValueError(f"{owner} PlanRevision does not reference its contract")
        if not self.check_specs:
            raise ValueError(f"{owner} must include at least one CheckSpec")
        check_ids = tuple(check.check_id for check in self.check_specs)
        if len(set(check_ids)) != len(check_ids):
            raise ValueError(f"{owner} contains duplicate CheckSpec IDs")
        required_ids = {check.check_id for check in self.check_specs if check.required}
        referenced_ids = {
            check_id for node in self.plan_revision.nodes for check_id in node.required_check_ids
        }
        if (
            not set(self.contract.required_check_ids).issubset(required_ids)
            or referenced_ids != required_ids
        ):
            raise ValueError(f"{owner} required CheckSpecs must cover its nodes and final contract")
        if (self.budget is None) != (self.usage is None):
            raise ValueError(f"{owner} budget and usage must be provided together")
        if self.budget is not None and self.usage is not None:
            self.usage.require_within(self.budget)
            actual_usage = _graph_usage(
                self.plan_revision.nodes,
                self.plan_revision.branches,
            )
            if actual_usage != self.usage:
                raise ValueError(f"{owner} usage does not match its PlanGraph")


@runtime_checkable
class Planner(Protocol):
    """Application Port for proposing, but never approving or executing, a plan."""

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        """Return an unapproved proposal for an open Goal."""
        ...

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        """Return a versioned replacement proposal for an approved base."""
        ...


@dataclass(frozen=True, slots=True)
class PlanningReply:
    text: str
    agent_session_ref_id: ID
    proposal: PlanProposal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 64_000:
            raise ValueError("Planning reply must contain 1-64000 characters")
        object.__setattr__(self, "agent_session_ref_id", normalize_id(self.agent_session_ref_id))


@runtime_checkable
class ProcessPlanner(Protocol):
    """Draft within an original approval; never review or publish the result."""

    def propose_process(
        self,
        goal: Goal,
        approved: PlanRevision,
        previous: ProcessRevision,
        current: PlanRevision,
        checks: tuple[CheckSpec, ...],
        reason: str,
        *,
        session_ref_id: ID | None = None,
        intervention_context: tuple[dict[str, JsonValue], ...] = (),
    ) -> ProcessRevision: ...


@runtime_checkable
class ProcessReviewer(Protocol):
    """Independently review a retained draft, without applying or approving it."""

    def review_process(
        self, context: ProcessReviewContext, *, session_ref_id: ID
    ) -> ProcessBoundaryReport: ...


@runtime_checkable
class AsyncProcessPlanner(Protocol):
    """Async process drafting capability backed by a genuinely async model call."""

    async def propose_process_async(
        self,
        goal: Goal,
        approved: PlanRevision,
        previous: ProcessRevision,
        current: PlanRevision,
        checks: tuple[CheckSpec, ...],
        reason: str,
        *,
        session_ref_id: ID | None = None,
        intervention_context: tuple[dict[str, JsonValue], ...] = (),
    ) -> ProcessRevision: ...


@runtime_checkable
class AsyncProcessReviewer(Protocol):
    """Async independent process review capability backed by a genuine async model call."""

    async def review_process_async(
        self, context: ProcessReviewContext, *, session_ref_id: ID
    ) -> ProcessBoundaryReport: ...


@runtime_checkable
class ConversationalPlanner(Protocol):
    def discuss(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        message: str,
        history: tuple[dict[str, JsonValue], ...],
        base: PlanRevision | None,
        *,
        checks: tuple[CheckSpec, ...] = (),
        source_context: ReplanContext | None = None,
        session_ref_id: ID | None = None,
    ) -> PlanningReply: ...


@dataclass(frozen=True, slots=True)
class ConfiguredCheckPlanner:
    """Bind host-approved Check configuration into each immutable proposal snapshot."""

    planner: Planner
    command_argv: tuple[str, ...] = ()
    semantic_required_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.planner, Planner):
            raise TypeError("planner must implement Planner")
        if isinstance(self.command_argv, str):
            raise ValueError("ConfiguredCheckPlanner command argv must not be shell text")
        argv = tuple(self.command_argv)
        if any(
            not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
        ):
            raise ValueError("ConfiguredCheckPlanner command argv is invalid")
        if isinstance(self.semantic_required_terms, str):
            raise ValueError("ConfiguredCheckPlanner semantic terms must be a sequence")
        terms = tuple(self.semantic_required_terms)
        if any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError("ConfiguredCheckPlanner semantic terms must not be blank")
        normalized_terms = tuple(term.strip().casefold() for term in terms)
        if len(set(normalized_terms)) != len(normalized_terms):
            raise ValueError("ConfiguredCheckPlanner semantic terms must not contain duplicates")
        object.__setattr__(self, "command_argv", argv)
        object.__setattr__(self, "semantic_required_terms", normalized_terms)

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        return self._bind(self.planner.propose(goal, criteria))

    def review_process(
        self, context: ProcessReviewContext, *, session_ref_id: ID
    ) -> ProcessBoundaryReport:
        if not isinstance(self.planner, ProcessReviewer):
            raise ValueError("Configured Planner cannot review processes; select Built-in Planner")
        return self.planner.review_process(context, session_ref_id=session_ref_id)

    async def review_process_async(
        self, context: ProcessReviewContext, *, session_ref_id: ID
    ) -> ProcessBoundaryReport:
        if not (
            isinstance(self.planner, AsyncProcessReviewer)
            and inspect.iscoroutinefunction(getattr(self.planner, "review_process_async", None))
        ):
            raise ValueError(
                "Configured Planner does not support async process reviews; "
                "select an async-capable Built-in Planner"
            )
        return await self.planner.review_process_async(context, session_ref_id=session_ref_id)

    def propose_process(
        self,
        goal: Goal,
        approved: PlanRevision,
        previous: ProcessRevision,
        current: PlanRevision,
        checks: tuple[CheckSpec, ...],
        reason: str,
        *,
        session_ref_id: ID | None = None,
        intervention_context: tuple[dict[str, JsonValue], ...] = (),
    ) -> ProcessRevision:
        if not isinstance(self.planner, ProcessPlanner):
            raise ValueError(
                "Configured Planner cannot draft processes; select the Built-in Planner"
            )
        # A process draft receives frozen stored Checks, never today's host defaults.
        return self.planner.propose_process(
            goal,
            approved,
            previous,
            current,
            checks,
            reason,
            session_ref_id=session_ref_id,
            intervention_context=intervention_context,
        )

    async def propose_process_async(
        self,
        goal: Goal,
        approved: PlanRevision,
        previous: ProcessRevision,
        current: PlanRevision,
        checks: tuple[CheckSpec, ...],
        reason: str,
        *,
        session_ref_id: ID | None = None,
        intervention_context: tuple[dict[str, JsonValue], ...] = (),
    ) -> ProcessRevision:
        if not (
            isinstance(self.planner, AsyncProcessPlanner)
            and inspect.iscoroutinefunction(getattr(self.planner, "propose_process_async", None))
        ):
            raise ValueError(
                "Configured Planner does not support async process drafts; "
                "select an async-capable Built-in Planner"
            )
        # A process draft receives frozen stored Checks, never today's host defaults.
        return await self.planner.propose_process_async(
            goal,
            approved,
            previous,
            current,
            checks,
            reason,
            session_ref_id=session_ref_id,
            intervention_context=intervention_context,
        )

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        return self._bind(self.planner.replan(goal, base, criteria, context))

    def _bind(self, proposal: PlanProposal) -> PlanProposal:
        configured: list[CheckSpec] = []
        for spec in proposal.check_specs:
            if spec.kind is CheckKind.COMMAND:
                final_check = spec.check_id in proposal.contract.required_check_ids
                if (
                    final_check
                    and self.command_argv
                    and spec.command_argv
                    and spec.command_argv != self.command_argv
                ):
                    raise ValueError(
                        "Planner command Check conflicts with explicit host command argv"
                    )
                if final_check and self.command_argv:
                    spec = replace(spec, command_argv=self.command_argv)
                elif not spec.command_argv:
                    raise ValueError("Command Check requires configured argv before persistence")
            elif spec.kind is CheckKind.SEMANTIC:
                if not self.semantic_required_terms:
                    raise ValueError(
                        "Semantic Check requires configured required terms before persistence"
                    )
                spec = replace(spec, semantic_required_terms=self.semantic_required_terms)
            configured.append(spec)
        return replace(proposal, check_specs=tuple(configured))

    def discuss(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        message: str,
        history: tuple[dict[str, JsonValue], ...],
        base: PlanRevision | None,
        *,
        checks: tuple[CheckSpec, ...] = (),
        source_context: ReplanContext | None = None,
        session_ref_id: ID | None = None,
    ) -> PlanningReply:
        if not isinstance(self.planner, ConversationalPlanner):
            raise ValueError(
                "This Planner does not support discussion; select the Built-in Planner"
            )
        normalized = normalize_discussion_criteria(criteria, "Planning discussion")
        if SEMANTIC_REQUIRED_TERMS_CRITERION in normalized and not self.semantic_required_terms:
            raise ValueError("Semantic Check requires configured terms before discussion")
        if source_context is not None:
            reply = self.planner.discuss(
                goal,
                normalized,
                message,
                history,
                base,
                checks=checks,
                source_context=source_context,
                session_ref_id=session_ref_id,
            )
        elif checks:
            reply = self.planner.discuss(
                goal,
                normalized,
                message,
                history,
                base,
                checks=checks,
                session_ref_id=session_ref_id,
            )
        else:
            reply = self.planner.discuss(
                goal, normalized, message, history, base, session_ref_id=session_ref_id
            )
        return replace(
            reply, proposal=None if reply.proposal is None else self._bind(reply.proposal)
        )


@dataclass(frozen=True, slots=True)
class DeterministicPlanner:
    """Create the repeatable single-work-node proposal used by the I3 slice."""

    id_factory: Callable[[], ID] = new_id
    clock: Callable[[], datetime] = utc_now

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        """Propose one required Artifact Check and one work node without executing it."""
        if goal.status is not GoalStatus.OPEN:
            raise ValueError(f"Goal {goal.goal_id} must be open before planning")
        if goal.completion_contract is not None:
            raise ValueError(f"Goal {goal.goal_id} already has a CompletionContract")
        normalized_criteria = require_p1_criteria(criteria, "DeterministicPlanner")
        return self._build_proposal(goal, normalized_criteria)

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        """Create a single-node replacement through the explicit GraphPatch boundary."""
        del context
        current_contract = require_replan_context(goal, base)
        normalized_criteria = require_p1_criteria(criteria, "DeterministicPlanner")
        template = self._build_proposal(goal, normalized_criteria)
        return _replan_from_template(base, current_contract, template)

    def _build_proposal(
        self,
        goal: Goal,
        normalized_criteria: tuple[str, ...],
    ) -> PlanProposal:
        return build_plan_proposal(
            goal,
            normalized_criteria,
            PlanTemplate(
                nodes=(
                    PlanNodeTemplate(
                        "work",
                        goal.objective,
                        f"Produce evidence that satisfies the Goal: {goal.objective}",
                    ),
                ),
            ),
            id_factory=self.id_factory,
            clock=self.clock,
        )


@dataclass(frozen=True, slots=True)
class DeterministicExplorationPlanner:
    """Create the bounded two-branch exploration graph used by I6."""

    id_factory: Callable[[], ID] = new_id
    clock: Callable[[], datetime] = utc_now

    def propose(self, request: ExplorationPlanRequest) -> PlanProposal:
        """Propose fork, branch work, evaluator, and merge nodes without executing them."""
        if not isinstance(request, ExplorationPlanRequest):
            raise TypeError("request must be an ExplorationPlanRequest")
        goal = request.goal
        if goal.status is not GoalStatus.OPEN:
            raise ValueError(f"Goal {goal.goal_id} must be open before planning")
        if goal.completion_contract is not None:
            raise ValueError(f"Goal {goal.goal_id} already has a CompletionContract")
        require_p1_criteria(request.criteria, "DeterministicExplorationPlanner")
        return self._build_proposal(request)

    def replan(
        self,
        request: ExplorationPlanRequest,
        base: PlanRevision,
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        """Create an exploration replacement through the explicit GraphPatch boundary."""
        del context
        current_contract = require_replan_context(request.goal, base)
        require_p1_criteria(request.criteria, "DeterministicExplorationPlanner")
        template = self._build_proposal(request)
        return _replan_from_template(base, current_contract, template)

    def _build_proposal(self, request: ExplorationPlanRequest) -> PlanProposal:
        goal = request.goal
        usage = ExplorationUsage(width=2, depth=1, attempts=5)
        usage.require_within(request.budget)
        return build_plan_proposal(
            goal,
            request.criteria,
            two_branch_plan_template(
                fork_title="Fork exploration",
                fork_instruction="Start two independent candidate approaches.",
                first_label="approach-a",
                first_title="Explore approach A",
                first_instruction=f"Explore the first approach for Goal: {goal.objective}",
                second_label="approach-b",
                second_title="Explore approach B",
                second_instruction=(
                    f"Explore an independent second approach for Goal: {goal.objective}"
                ),
                evaluator_title="Evaluate branch candidates",
                evaluator_instruction=(
                    "Compare both branch candidates using their persisted evidence."
                ),
                merge_title="Merge selected candidate",
                merge_instruction="Produce the final merged candidate from the evaluator decision.",
                budget=request.budget,
                usage=usage,
            ),
            id_factory=self.id_factory,
            clock=self.clock,
        )


@dataclass(frozen=True, slots=True)
class GraphPatch:
    """A validated replacement graph applied only to an approved base revision."""

    base_plan_revision_id: ID
    target_completion_contract_id: ID
    nodes: tuple[PlanNode, ...]
    edges: tuple[Edge, ...]
    branches: tuple[Branch, ...]
    budget: ExplorationBudget
    usage: ExplorationUsage
    phases: tuple[PlanPhase, ...] = ()
    design_document: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "base_plan_revision_id",
            normalize_id(self.base_plan_revision_id),
        )
        object.__setattr__(
            self,
            "target_completion_contract_id",
            normalize_id(self.target_completion_contract_id),
        )
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "branches", tuple(self.branches))
        object.__setattr__(self, "phases", tuple(self.phases))
        if not isinstance(self.budget, ExplorationBudget):
            raise TypeError("GraphPatch budget must be an ExplorationBudget")
        if not isinstance(self.usage, ExplorationUsage):
            raise TypeError("GraphPatch usage must be an ExplorationUsage")
        self.usage.require_within(self.budget)
        if _graph_usage(self.nodes, self.branches) != self.usage:
            raise ValueError("GraphPatch usage does not match its graph")
        if any(node.status is not PlanNodeStatus.PENDING for node in self.nodes):
            raise ValueError("GraphPatch nodes must start pending")
        if any(branch.status is not BranchStatus.ACTIVE for branch in self.branches):
            raise ValueError("GraphPatch branches must start active")

    def apply(
        self,
        base: PlanRevision,
        target_contract: CompletionContract,
        *,
        plan_revision_id: ID | None = None,
        created_at: datetime | None = None,
    ) -> PlanRevision:
        """Create a new draft revision while preserving the approved base unchanged."""
        if base.plan_revision_id != self.base_plan_revision_id:
            raise ValueError("GraphPatch does not reference the supplied base PlanRevision")
        if base.status is not PlanRevisionStatus.APPROVED:
            raise ValueError(f"base PlanRevision {base.plan_revision_id} must be approved")
        if target_contract.completion_contract_id != self.target_completion_contract_id:
            raise ValueError("GraphPatch does not reference the supplied target contract")
        if target_contract.goal_id != base.goal_id:
            raise ValueError("GraphPatch target contract belongs to another Goal")
        self.usage.require_within(self.budget)
        return base.revise(
            target_contract,
            self.nodes,
            self.edges,
            self.branches,
            self.phases,
            plan_revision_id=plan_revision_id,
            created_at=created_at,
            design_document=self.design_document,
        )


def _graph_usage(
    nodes: tuple[PlanNode, ...],
    branches: tuple[Branch, ...],
) -> ExplorationUsage:
    groups: dict[tuple[ID, ID], int] = {}
    for branch in branches:
        key = (branch.fork_node_id, branch.merge_node_id)
        groups[key] = groups.get(key, 0) + 1
    width = max(groups.values(), default=0)
    depth = max((len(branch.node_ids) for branch in branches), default=0)
    attempts = len(nodes)
    return ExplorationUsage(width=width, depth=depth, attempts=attempts)


def _planner_messages(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    snapshot = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in snapshot):
        raise ValueError(f"PlanProposal {field_name} must contain non-blank strings")
    return snapshot


def _normalize_argv(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise ValueError(f"{field_name} must be an argv sequence, not shell text")
    argv = tuple(value)
    if any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
    ):
        raise ValueError(f"{field_name} contains an invalid argument")
    return argv


def build_plan_proposal(
    goal: Goal,
    criteria: tuple[str, ...],
    template: PlanTemplate,
    *,
    id_factory: Callable[[], ID] = new_id,
    clock: Callable[[], datetime] = utc_now,
) -> PlanProposal:
    """Assemble provider-neutral template content into EHAI Planning domain objects."""
    if not isinstance(goal, Goal):
        raise TypeError("goal must be a Goal")
    if not isinstance(template, PlanTemplate):
        raise TypeError("template must be a PlanTemplate")
    if template.retained_check_ids or template.retained_gate_owners:
        raise ValueError(
            "PlanProposalBuilder cannot consume retained process Gates; use the process assembler"
        )
    normalized_criteria = require_p1_criteria(criteria, "PlanProposalBuilder")
    if template.final_gate_argv and COMMAND_EXIT_ZERO_CRITERION not in normalized_criteria:
        normalized_criteria = (*normalized_criteria, COMMAND_EXIT_ZERO_CRITERION)
    proposed_at = clock()
    final_check_specs = [
        CheckSpec(
            name=_check_name(criterion),
            kind=_check_kind(criterion),
            description=_human_question(criterion) or criterion,
            required=True,
            check_id=id_factory(),
            command_argv=(
                template.final_gate_argv if criterion == COMMAND_EXIT_ZERO_CRITERION else ()
            ),
        )
        for criterion in normalized_criteria
    ]
    if template.final_human_question is not None and not any(
        _human_question(criterion) == template.final_human_question
        for criterion in normalized_criteria
    ):
        final_check_specs.append(
            CheckSpec(
                name="completion-human",
                kind=CheckKind.HUMAN,
                description=template.final_human_question,
                required=True,
                check_id=id_factory(),
            )
        )
    check_specs = tuple(final_check_specs)
    required_check_ids = tuple(check.check_id for check in check_specs)
    local_checks: dict[str, tuple[CheckSpec, ...]] = {}
    for gate in template.node_gates:
        gate_specs: list[CheckSpec] = []
        if gate.command_argv:
            gate_specs.append(
                CheckSpec(
                    name=gate.name,
                    kind=CheckKind.COMMAND,
                    description=f"Node gate: {gate.name}",
                    required=True,
                    check_id=id_factory(),
                    command_argv=gate.command_argv,
                )
            )
        if gate.human_question is not None:
            gate_specs.append(
                CheckSpec(
                    name=f"{gate.name} (human)",
                    kind=CheckKind.HUMAN,
                    description=gate.human_question,
                    required=True,
                    check_id=id_factory(),
                )
            )
        local_checks[gate.node_key] = tuple(gate_specs)
    contract = CompletionContract.draft(
        goal_id=goal.goal_id,
        criteria=normalized_criteria,
        required_check_ids=required_check_ids,
        completion_contract_id=id_factory(),
        created_at=proposed_at,
    )
    node_ids = {node.key: id_factory() for node in template.nodes}
    nodes = tuple(
        PlanNode(
            plan_node_id=node_ids[node.key],
            title=node.title,
            instruction=node.instruction,
            kind=node.kind,
            required_dependency_ids=tuple(
                node_ids[dependency_key] for dependency_key in node.required_dependency_keys
            ),
            required_capabilities=node.required_capabilities,
            session_policy=node.session_policy,
            required_check_ids=(
                required_check_ids
                if (
                    (template.final_node_key is not None and node.key == template.final_node_key)
                    or (template.final_node_key is None and node.require_completion_checks)
                )
                else tuple(check.check_id for check in local_checks.get(node.key, ()))
            ),
        )
        for node in template.nodes
    )
    branch_ids = {branch.key: id_factory() for branch in template.branches}
    branches = tuple(
        Branch(
            branch_id=branch_ids[branch.key],
            label=branch.label,
            fork_node_id=node_ids[branch.fork_node_key],
            node_ids=tuple(node_ids[node_key] for node_key in branch.node_keys),
            merge_node_id=node_ids[branch.merge_node_key],
        )
        for branch in template.branches
    )
    phases = tuple(
        PlanPhase(
            phase_id=id_factory(),
            title=phase.title,
            node_ids=tuple(node_ids[node_key] for node_key in phase.node_keys),
            reviewer_node_id=node_ids[phase.reviewer_node_key],
            gate_node_id=node_ids[phase.gate_node_key],
            rework_node_ids=tuple(node_ids[node_key] for node_key in phase.rework_node_keys),
        )
        for phase in template.phases
    )
    edges = tuple(
        Edge(
            id_factory(),
            node_ids[edge.source_node_key],
            node_ids[edge.target_node_key],
            edge.edge_type,
            branch_id=None if edge.branch_key is None else branch_ids[edge.branch_key],
            condition=edge.condition,
        )
        for edge in template.edges
    )
    revision = PlanRevision.draft(
        goal_id=goal.goal_id,
        completion_contract=contract,
        nodes=nodes,
        edges=edges,
        branches=branches,
        phases=phases,
        plan_revision_id=id_factory(),
        created_at=proposed_at,
        design_document=template.design_document,
    )
    return PlanProposal(
        contract=contract,
        plan_revision=revision,
        check_specs=(
            *check_specs,
            *(check for checks in local_checks.values() for check in checks),
        ),
        budget=template.budget,
        usage=template.usage,
        planner_diagnostics=template.planner_diagnostics,
        planner_event_types=template.planner_event_types,
    )


def require_p1_criteria(criteria: tuple[str, ...], owner: str) -> tuple[str, ...]:
    """Normalize executable criteria and explicit ``human:<question>`` conditions."""
    normalized = tuple(criterion.strip() for criterion in criteria)
    if not normalized or any(not criterion for criterion in normalized):
        raise ValueError(f"{owner} requires non-empty completion criteria")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{owner} completion criteria must be unique")
    if any(
        criterion not in P1_COMPLETION_CRITERIA and _human_question(criterion) is None
        for criterion in normalized
    ):
        raise ValueError(
            f"{owner} supports these completion criteria: "
            + ", ".join((*sorted(P1_COMPLETION_CRITERIA), "human:<question>"))
        )
    return normalized


def normalize_discussion_criteria(criteria: tuple[str, ...], owner: str) -> tuple[str, ...]:
    """Allow discussion callers to omit criteria while preserving explicit validation."""
    return () if not criteria else require_p1_criteria(criteria, owner)


def discussion_proposal_criteria(
    criteria: tuple[str, ...], template: PlanTemplate
) -> tuple[str, ...]:
    """Use explicit discussion criteria or derive the model's final Gate condition."""
    normalized = normalize_discussion_criteria(criteria, "Planning discussion")
    if normalized:
        return normalized
    inferred: list[str] = []
    if template.final_gate_argv:
        inferred.append(COMMAND_EXIT_ZERO_CRITERION)
    if template.final_human_question is not None:
        inferred.append(f"{HUMAN_CRITERION_PREFIX}{template.final_human_question}")
    if not inferred:
        raise ValueError(
            "Discussion without explicit criteria requires a non-empty command or human final Gate"
        )
    return require_p1_criteria(tuple(inferred), "Planning discussion")


def two_branch_plan_template(
    *,
    fork_title: str,
    fork_instruction: str,
    first_label: str,
    first_title: str,
    first_instruction: str,
    second_label: str,
    second_title: str,
    second_instruction: str,
    evaluator_title: str,
    evaluator_instruction: str,
    merge_title: str,
    merge_instruction: str,
    budget: ExplorationBudget,
    usage: ExplorationUsage,
    planner_diagnostics: tuple[str, ...] = (),
    planner_event_types: tuple[str, ...] = (),
) -> PlanTemplate:
    """Return the fixed P2.1 bounded two-branch exploration graph template."""
    return PlanTemplate(
        nodes=(
            PlanNodeTemplate("fork", fork_title, fork_instruction, kind=PlanNodeKind.FORK),
            PlanNodeTemplate("first", first_title, first_instruction),
            PlanNodeTemplate("second", second_title, second_instruction),
            PlanNodeTemplate(
                "evaluator",
                evaluator_title,
                evaluator_instruction,
                kind=PlanNodeKind.EVALUATOR,
                required_dependency_keys=("first", "second"),
            ),
            PlanNodeTemplate(
                "merge",
                merge_title,
                merge_instruction,
                kind=PlanNodeKind.MERGE,
                required_dependency_keys=("evaluator",),
            ),
        ),
        branches=(
            BranchTemplate("first", first_label, "fork", ("first",), "merge"),
            BranchTemplate("second", second_label, "fork", ("second",), "merge"),
        ),
        edges=(
            EdgeTemplate("fork", "first", EdgeType.EXPLORATION, branch_key="first"),
            EdgeTemplate("first", "merge", EdgeType.MERGE, branch_key="first"),
            EdgeTemplate("fork", "second", EdgeType.EXPLORATION, branch_key="second"),
            EdgeTemplate("second", "merge", EdgeType.MERGE, branch_key="second"),
            EdgeTemplate("first", "evaluator", EdgeType.DEPENDENCY),
            EdgeTemplate("second", "evaluator", EdgeType.DEPENDENCY),
            EdgeTemplate("evaluator", "merge", EdgeType.DEPENDENCY),
        ),
        budget=budget,
        usage=usage,
        planner_diagnostics=planner_diagnostics,
        planner_event_types=planner_event_types,
    )


def _template_key(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text")
    return value.strip()


def _optional_template_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-blank text when provided")
    return value.strip()


def _check_kind(criterion: str) -> CheckKind:
    if _human_question(criterion) is not None:
        return CheckKind.HUMAN
    if criterion == NON_EMPTY_ARTIFACT_CRITERION:
        return CheckKind.ARTIFACT
    if criterion == COMMAND_EXIT_ZERO_CRITERION:
        return CheckKind.COMMAND
    if criterion == SEMANTIC_REQUIRED_TERMS_CRITERION:
        return CheckKind.SEMANTIC
    raise ValueError(f"unsupported P1 completion criterion: {criterion}")


def _check_name(criterion: str) -> str:
    if _human_question(criterion) is not None:
        return "completion-human"
    return {
        NON_EMPTY_ARTIFACT_CRITERION: "completion-artifact",
        COMMAND_EXIT_ZERO_CRITERION: "completion-command",
        SEMANTIC_REQUIRED_TERMS_CRITERION: "completion-semantic",
    }[criterion]


def _human_question(criterion: str) -> str | None:
    if not isinstance(criterion, str) or not criterion.startswith(HUMAN_CRITERION_PREFIX):
        return None
    question = criterion[len(HUMAN_CRITERION_PREFIX) :].strip()
    return question or None


def require_replan_context(goal: Goal, base: PlanRevision) -> CompletionContract:
    """Validate the approved base and return its confirmed current contract."""
    if goal.status is not GoalStatus.OPEN:
        raise ValueError(f"Goal {goal.goal_id} must be open before replanning")
    current = goal.completion_contract
    if current is None or not current.is_confirmed:
        raise ValueError(f"Goal {goal.goal_id} requires a confirmed CompletionContract")
    if base.status is not PlanRevisionStatus.APPROVED:
        raise ValueError(f"base PlanRevision {base.plan_revision_id} must be approved")
    if base.goal_id != goal.goal_id:
        raise ValueError(f"base PlanRevision {base.plan_revision_id} belongs to another Goal")
    if (
        base.completion_contract_id != current.completion_contract_id
        or base.completion_contract_version != current.version
    ):
        raise ValueError(
            f"base PlanRevision {base.plan_revision_id} is not aligned to the current "
            "CompletionContract"
        )
    return current


def _replan_from_template(
    base: PlanRevision,
    current_contract: CompletionContract,
    template: PlanProposal,
) -> PlanProposal:
    revised_contract = current_contract.revise(
        template.contract.criteria,
        template.contract.required_check_ids,
        completion_contract_id=template.contract.completion_contract_id,
        created_at=template.contract.created_at,
    )
    usage = template.usage or _graph_usage(
        template.plan_revision.nodes,
        template.plan_revision.branches,
    )
    budget = template.budget or ExplorationBudget(max_attempts=max(1, usage.attempts))
    patch = GraphPatch(
        base_plan_revision_id=base.plan_revision_id,
        target_completion_contract_id=revised_contract.completion_contract_id,
        nodes=template.plan_revision.nodes,
        edges=template.plan_revision.edges,
        branches=template.plan_revision.branches,
        phases=template.plan_revision.phases,
        design_document=template.plan_revision.design_document,
        budget=budget,
        usage=usage,
    )
    revised_plan = patch.apply(
        base,
        revised_contract,
        plan_revision_id=template.plan_revision.plan_revision_id,
        created_at=template.plan_revision.created_at,
    )
    return PlanProposal(
        contract=revised_contract,
        plan_revision=revised_plan,
        check_specs=template.check_specs,
        budget=template.budget,
        usage=template.usage,
        planner_diagnostics=template.planner_diagnostics,
        planner_event_types=template.planner_event_types,
    )


def replan_from_template(
    goal: Goal,
    base: PlanRevision,
    template: PlanProposal,
) -> PlanProposal:
    """Apply a validated Planner template through the versioned GraphPatch boundary."""
    current_contract = require_replan_context(goal, base)
    return _replan_from_template(base, current_contract, template)
