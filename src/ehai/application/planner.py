"""Planner Port and deterministic single-node I3 implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

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
    PlanRevision,
    PlanRevisionStatus,
)

NON_EMPTY_ARTIFACT_CRITERION = "artifact:non-empty"
COMMAND_EXIT_ZERO_CRITERION = "command:exit-zero"
SEMANTIC_REQUIRED_TERMS_CRITERION = "semantic:required-terms"
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

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_run_id": self.source_run_id,
            "source_run_status": self.source_run_status.value,
            "source_run_reason": self.source_run_reason,
            "failed_plan_node_ids": list(self.failed_plan_node_ids),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "failed_checks": [check.to_dict() for check in self.failed_checks],
            "consumed_attempt_count": self.consumed_attempt_count,
            "latest_checkpoint": (
                None if self.latest_checkpoint is None else self.latest_checkpoint.to_dict()
            ),
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
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "instruction", self.instruction.strip())
        object.__setattr__(self, "required_dependency_keys", dependencies)


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
class PlanTemplate:
    """Provider-neutral proposal content assembled into EHAI domain objects."""

    nodes: tuple[PlanNodeTemplate, ...]
    edges: tuple[EdgeTemplate, ...] = ()
    branches: tuple[BranchTemplate, ...] = ()
    budget: ExplorationBudget | None = None
    usage: ExplorationUsage | None = None
    planner_diagnostics: tuple[str, ...] = ()
    planner_event_types: tuple[str, ...] = ()
    design_document: str | None = None

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        branches = tuple(self.branches)
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
        if (self.budget is None) != (self.usage is None):
            raise ValueError("PlanTemplate budget and usage must be provided together")
        if self.budget is not None and self.usage is not None:
            self.usage.require_within(self.budget)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "branches", branches)
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
        if required_ids != set(self.contract.required_check_ids):
            raise ValueError(f"{owner} required CheckSpecs do not match its contract")
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
class ConversationalPlanner(Protocol):
    def discuss(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        message: str,
        history: tuple[dict[str, JsonValue], ...],
        base: PlanRevision | None,
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
                if not self.command_argv:
                    raise ValueError("Command Check requires configured argv before persistence")
                spec = replace(spec, command_argv=self.command_argv)
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
    ) -> PlanningReply:
        if not isinstance(self.planner, ConversationalPlanner):
            raise ValueError(
                "This Planner does not support discussion; select the Built-in Planner"
            )
        normalized = require_p1_criteria(criteria, "Planning discussion")
        if COMMAND_EXIT_ZERO_CRITERION in normalized and not self.command_argv:
            raise ValueError("Command Check requires configured argv before discussion")
        if SEMANTIC_REQUIRED_TERMS_CRITERION in normalized and not self.semantic_required_terms:
            raise ValueError("Semantic Check requires configured terms before discussion")
        reply = self.planner.discuss(goal, normalized, message, history, base)
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
    attempts = sum(bool(node.required_check_ids) for node in nodes)
    return ExplorationUsage(width=width, depth=depth, attempts=attempts)


def _planner_messages(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    snapshot = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in snapshot):
        raise ValueError(f"PlanProposal {field_name} must contain non-blank strings")
    return snapshot


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
    normalized_criteria = require_p1_criteria(criteria, "PlanProposalBuilder")
    proposed_at = clock()
    check_specs = tuple(
        CheckSpec(
            name=_check_name(criterion),
            kind=_check_kind(criterion),
            description=criterion,
            required=True,
            check_id=id_factory(),
        )
        for criterion in normalized_criteria
    )
    required_check_ids = tuple(check.check_id for check in check_specs)
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
            required_check_ids=required_check_ids if node.require_completion_checks else (),
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
        plan_revision_id=id_factory(),
        created_at=proposed_at,
        design_document=template.design_document,
    )
    return PlanProposal(
        contract=contract,
        plan_revision=revision,
        check_specs=check_specs,
        budget=template.budget,
        usage=template.usage,
        planner_diagnostics=template.planner_diagnostics,
        planner_event_types=template.planner_event_types,
    )


def require_p1_criteria(criteria: tuple[str, ...], owner: str) -> tuple[str, ...]:
    """Normalize and validate the currently supported P1 completion criteria."""
    normalized = tuple(criterion.strip() for criterion in criteria)
    if not normalized or any(not criterion for criterion in normalized):
        raise ValueError(f"{owner} requires non-empty completion criteria")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{owner} completion criteria must be unique")
    if not set(normalized).issubset(P1_COMPLETION_CRITERIA):
        raise ValueError(
            f"{owner} supports these executable completion criteria: "
            + ", ".join(sorted(P1_COMPLETION_CRITERIA))
        )
    return normalized


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


def _check_kind(criterion: str) -> CheckKind:
    if criterion == NON_EMPTY_ARTIFACT_CRITERION:
        return CheckKind.ARTIFACT
    if criterion == COMMAND_EXIT_ZERO_CRITERION:
        return CheckKind.COMMAND
    if criterion == SEMANTIC_REQUIRED_TERMS_CRITERION:
        return CheckKind.SEMANTIC
    raise ValueError(f"unsupported P1 completion criterion: {criterion}")


def _check_name(criterion: str) -> str:
    return {
        NON_EMPTY_ARTIFACT_CRITERION: "completion-artifact",
        COMMAND_EXIT_ZERO_CRITERION: "completion-command",
        SEMANTIC_REQUIRED_TERMS_CRITERION: "completion-semantic",
    }[criterion]


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
