"""Planner Port and deterministic single-node I3 implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ehai import ID, new_id, normalize_id, utc_now
from ehai.domain.checking import CheckKind, CheckSpec
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
    ) -> PlanProposal:
        """Return a versioned replacement proposal for an approved base."""
        ...


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
        normalized_criteria = tuple(criterion.strip() for criterion in criteria)
        if not normalized_criteria or any(not criterion for criterion in normalized_criteria):
            raise ValueError(f"Goal {goal.goal_id} requires non-empty completion criteria")
        if normalized_criteria != (NON_EMPTY_ARTIFACT_CRITERION,):
            raise ValueError(
                "DeterministicPlanner supports exactly one P1 completion criterion: "
                f"{NON_EMPTY_ARTIFACT_CRITERION}"
            )
        return self._build_proposal(goal, normalized_criteria)

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
    ) -> PlanProposal:
        """Create a single-node replacement through the explicit GraphPatch boundary."""
        current_contract = require_replan_context(goal, base)
        normalized_criteria = tuple(criterion.strip() for criterion in criteria)
        if normalized_criteria != (NON_EMPTY_ARTIFACT_CRITERION,):
            raise ValueError(
                "DeterministicPlanner supports exactly one P1 completion criterion: "
                f"{NON_EMPTY_ARTIFACT_CRITERION}"
            )
        template = self._build_proposal(goal, normalized_criteria)
        return _replan_from_template(base, current_contract, template)

    def _build_proposal(
        self,
        goal: Goal,
        normalized_criteria: tuple[str, ...],
    ) -> PlanProposal:
        proposed_at = self.clock()
        check_spec = CheckSpec(
            name="completion-artifact",
            kind=CheckKind.ARTIFACT,
            description=NON_EMPTY_ARTIFACT_CRITERION,
            required=True,
            check_id=self.id_factory(),
        )
        contract = CompletionContract.draft(
            goal_id=goal.goal_id,
            criteria=normalized_criteria,
            required_check_ids=(check_spec.check_id,),
            completion_contract_id=self.id_factory(),
            created_at=proposed_at,
        )
        node = PlanNode(
            plan_node_id=self.id_factory(),
            title=goal.objective,
            instruction=f"Produce evidence that satisfies the Goal: {goal.objective}",
            kind=PlanNodeKind.WORK,
            required_check_ids=(check_spec.check_id,),
        )
        revision = PlanRevision.draft(
            goal_id=goal.goal_id,
            completion_contract=contract,
            nodes=(node,),
            edges=(),
            plan_revision_id=self.id_factory(),
            created_at=proposed_at,
        )
        return PlanProposal(
            contract=contract,
            plan_revision=revision,
            check_specs=(check_spec,),
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
        if request.criteria != (NON_EMPTY_ARTIFACT_CRITERION,):
            raise ValueError(
                "DeterministicExplorationPlanner supports exactly one P1 completion "
                f"criterion: {NON_EMPTY_ARTIFACT_CRITERION}"
            )
        return self._build_proposal(request)

    def replan(
        self,
        request: ExplorationPlanRequest,
        base: PlanRevision,
    ) -> PlanProposal:
        """Create an exploration replacement through the explicit GraphPatch boundary."""
        current_contract = require_replan_context(request.goal, base)
        if request.criteria != (NON_EMPTY_ARTIFACT_CRITERION,):
            raise ValueError(
                "DeterministicExplorationPlanner supports exactly one P1 completion "
                f"criterion: {NON_EMPTY_ARTIFACT_CRITERION}"
            )
        template = self._build_proposal(request)
        return _replan_from_template(base, current_contract, template)

    def _build_proposal(self, request: ExplorationPlanRequest) -> PlanProposal:
        goal = request.goal
        usage = ExplorationUsage(width=2, depth=1, attempts=5)
        usage.require_within(request.budget)
        proposed_at = self.clock()
        check_spec = CheckSpec(
            name="completion-artifact",
            kind=CheckKind.ARTIFACT,
            description=NON_EMPTY_ARTIFACT_CRITERION,
            required=True,
            check_id=self.id_factory(),
        )
        contract = CompletionContract.draft(
            goal_id=goal.goal_id,
            criteria=request.criteria,
            required_check_ids=(check_spec.check_id,),
            completion_contract_id=self.id_factory(),
            created_at=proposed_at,
        )
        required_checks = (check_spec.check_id,)
        fork = PlanNode(
            plan_node_id=self.id_factory(),
            title="Fork exploration",
            instruction="Start two independent candidate approaches.",
            kind=PlanNodeKind.FORK,
            required_check_ids=required_checks,
        )
        first = PlanNode(
            plan_node_id=self.id_factory(),
            title="Explore approach A",
            instruction=f"Explore the first approach for Goal: {goal.objective}",
            required_check_ids=required_checks,
        )
        second = PlanNode(
            plan_node_id=self.id_factory(),
            title="Explore approach B",
            instruction=f"Explore an independent second approach for Goal: {goal.objective}",
            required_check_ids=required_checks,
        )
        evaluator = PlanNode(
            plan_node_id=self.id_factory(),
            title="Evaluate branch candidates",
            instruction="Compare both branch candidates using their persisted evidence.",
            kind=PlanNodeKind.EVALUATOR,
            required_dependency_ids=(first.plan_node_id, second.plan_node_id),
            required_check_ids=required_checks,
        )
        merge = PlanNode(
            plan_node_id=self.id_factory(),
            title="Merge selected candidate",
            instruction="Produce the final merged candidate from the evaluator decision.",
            kind=PlanNodeKind.MERGE,
            required_dependency_ids=(evaluator.plan_node_id,),
            required_check_ids=required_checks,
        )
        first_branch = Branch(
            branch_id=self.id_factory(),
            label="approach-a",
            fork_node_id=fork.plan_node_id,
            node_ids=(first.plan_node_id,),
            merge_node_id=merge.plan_node_id,
        )
        second_branch = Branch(
            branch_id=self.id_factory(),
            label="approach-b",
            fork_node_id=fork.plan_node_id,
            node_ids=(second.plan_node_id,),
            merge_node_id=merge.plan_node_id,
        )
        edges = (
            Edge(
                self.id_factory(),
                fork.plan_node_id,
                first.plan_node_id,
                EdgeType.EXPLORATION,
                branch_id=first_branch.branch_id,
            ),
            Edge(
                self.id_factory(),
                first.plan_node_id,
                merge.plan_node_id,
                EdgeType.MERGE,
                branch_id=first_branch.branch_id,
            ),
            Edge(
                self.id_factory(),
                fork.plan_node_id,
                second.plan_node_id,
                EdgeType.EXPLORATION,
                branch_id=second_branch.branch_id,
            ),
            Edge(
                self.id_factory(),
                second.plan_node_id,
                merge.plan_node_id,
                EdgeType.MERGE,
                branch_id=second_branch.branch_id,
            ),
            Edge(
                self.id_factory(),
                first.plan_node_id,
                evaluator.plan_node_id,
                EdgeType.DEPENDENCY,
            ),
            Edge(
                self.id_factory(),
                second.plan_node_id,
                evaluator.plan_node_id,
                EdgeType.DEPENDENCY,
            ),
            Edge(
                self.id_factory(),
                evaluator.plan_node_id,
                merge.plan_node_id,
                EdgeType.DEPENDENCY,
            ),
        )
        revision = PlanRevision.draft(
            goal_id=goal.goal_id,
            completion_contract=contract,
            nodes=(fork, first, second, evaluator, merge),
            edges=edges,
            branches=(first_branch, second_branch),
            plan_revision_id=self.id_factory(),
            created_at=proposed_at,
        )
        return PlanProposal(
            contract=contract,
            plan_revision=revision,
            check_specs=(check_spec,),
            budget=request.budget,
            usage=usage,
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
