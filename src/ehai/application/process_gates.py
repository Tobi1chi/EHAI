"""Bounded Gate-precondition proofs for approved process graphs.

This module computes only scheduling preconditions: which stable Gate IDs must
already have succeeded before a PlanNode can become ready or complete, and
which Gate combinations can lead to final success.  It deliberately does not
prove Gate scope, requiredness, semantic checks, evidence provenance, or
authorize publishing a ProcessRevision.  A caller must perform those checks
and make the release decision separately.

The analysis follows the execution semantics in ``orchestrator.ready_nodes``
and ``_is_final_completion`` together with branch-selection validation.  It
does not use runtime GateDecision IDs or freeze internal Branch IDs into the
proof.  Branch choices remain OR alternatives represented only by their
stable Gate sets.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ehai import ID, normalize_id
from ehai.domain.planning import (
    Branch,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanRevision,
    PlanRevisionStatus,
)

GateFamily = frozenset[frozenset[ID]]
"""An antichain of alternative sets of stable Gate IDs."""

MAX_FAMILY_SIZE: Final = 256

# The empty set of Gates is a sufficient precondition, while no alternatives
# at all represent an impossible proof.
TRUE: Final[GateFamily] = frozenset({frozenset()})
FALSE: Final[GateFamily] = frozenset()


class ProcessProofUnavailable(ValueError):
    """Raised when a bounded, sound Gate-precondition proof is unavailable."""


@dataclass(frozen=True, slots=True)
class GatePreconditions:
    """Immutable Gate families for readiness, completion, and final success."""

    ready: Mapping[ID, GateFamily]
    completed: Mapping[ID, GateFamily]
    final_success: GateFamily

    def __post_init__(self) -> None:
        object.__setattr__(self, "ready", _freeze_family_mapping(self.ready, "ready"))
        object.__setattr__(
            self,
            "completed",
            _freeze_family_mapping(self.completed, "completed"),
        )
        object.__setattr__(self, "final_success", _bounded_family(self.final_success))


@dataclass(frozen=True, slots=True)
class NodePreconditions:
    """Immutable node-marker families for structural obligation checks."""

    ready: Mapping[ID, GateFamily]
    completed: Mapping[ID, GateFamily]
    final_success: GateFamily

    def __post_init__(self) -> None:
        object.__setattr__(self, "ready", _freeze_family_mapping(self.ready, "ready"))
        object.__setattr__(
            self,
            "completed",
            _freeze_family_mapping(self.completed, "completed"),
        )
        object.__setattr__(self, "final_success", _bounded_family(self.final_success))


def family_implies(new: GateFamily, old: GateFamily) -> bool:
    """Return whether every new alternative contains an old alternative.

    This is the subset-based implication relation used by process publication:
    ``forall Snew exists Told: Told <= Snew``.  In particular, the empty
    family follows the mathematical relation vacuously; callers must reject a
    false final-success family before treating a proof as a successful route.
    """

    return all(any(old_set <= new_set for old_set in old) for new_set in new)


def analyze_gate_preconditions(
    plan: PlanRevision,
    gate_ids: Mapping[ID, ID],
) -> GatePreconditions:
    """Compute bounded stable-Gate families for one approved PlanRevision.

    ``gate_ids`` maps every node that owns one or more required Checks to the
    caller's stable Gate ID.  The mapping must contain exactly those owners,
    with no duplicate stable IDs.  Node and Branch runtime statuses are
    intentionally ignored: every structurally available branch is treated as
    a possible selection so a before/after process proof is status independent.

    The analyzer raises :class:`ProcessProofUnavailable` rather than returning
    an optimistic result when the graph uses execution semantics it cannot
    prove or when a family would exceed :data:`MAX_FAMILY_SIZE` alternatives.
    """

    if not isinstance(plan, PlanRevision):
        raise TypeError("plan must be a PlanRevision")
    if not isinstance(gate_ids, Mapping):
        raise TypeError("gate_ids must be a mapping")
    if plan.status is not PlanRevisionStatus.APPROVED:
        raise ProcessProofUnavailable("Gate proof requires an approved PlanRevision")

    nodes = tuple(plan.nodes)
    node_by_id = {node.plan_node_id: node for node in nodes}
    stable_gate_by_node = _validate_gate_ids(node_by_id, gate_ids)
    analyzer = _build_analyzer(plan, node_by_id, stable_gate_by_node)
    ready = {node.plan_node_id: analyzer.ready(node.plan_node_id) for node in nodes}
    completed = {node.plan_node_id: analyzer.completed(node.plan_node_id) for node in nodes}
    final_success = _final_success_family(plan, analyzer)
    return GatePreconditions(ready=ready, completed=completed, final_success=final_success)


def analyze_node_preconditions(plan: PlanRevision) -> NodePreconditions:
    """Compute structural node-marker families for one approved PlanRevision.

    Each PlanNode is treated as its own marker.  The result describes the
    structural prerequisites for a node to become ready or complete and does
    not assert anything about Artifact freshness, semantic validity, or
    authorization.
    """
    if not isinstance(plan, PlanRevision):
        raise TypeError("plan must be a PlanRevision")
    if plan.status is not PlanRevisionStatus.APPROVED:
        raise ProcessProofUnavailable("Node proof requires an approved PlanRevision")

    nodes = tuple(plan.nodes)
    node_by_id = {node.plan_node_id: node for node in nodes}
    markers = {node_id: node_id for node_id in node_by_id}
    analyzer = _build_analyzer(plan, node_by_id, markers)
    ready = {node.plan_node_id: analyzer.ready(node.plan_node_id) for node in nodes}
    completed = {node.plan_node_id: analyzer.completed(node.plan_node_id) for node in nodes}
    final_success = _final_success_family(plan, analyzer)
    return NodePreconditions(ready=ready, completed=completed, final_success=final_success)


def _build_analyzer(
    plan: PlanRevision,
    node_by_id: Mapping[ID, PlanNode],
    stable_gate_by_node: Mapping[ID, ID],
) -> _Analyzer:
    nodes = tuple(plan.nodes)
    _reject_unsupported_edges(plan)
    branch_groups, branch_by_node, branch_owner_by_node = _build_branch_groups(plan, node_by_id)
    evaluator_by_group, evaluator_group_by_node = _build_evaluator_index(
        nodes,
        branch_groups,
        branch_by_node,
    )
    _validate_merge_selection_inputs(
        nodes,
        node_by_id,
        branch_groups,
        branch_by_node,
        branch_owner_by_node,
        evaluator_by_group,
    )
    return _Analyzer(
        node_by_id=node_by_id,
        stable_gate_by_node=stable_gate_by_node,
        incoming_exploration=_edge_index(plan),
        branch_groups=branch_groups,
        branch_by_node=branch_by_node,
        evaluator_by_group=evaluator_by_group,
        evaluator_group_by_node=evaluator_group_by_node,
    )


@dataclass(frozen=True, slots=True)
class _BranchGroup:
    """A fork-to-merge sibling group, retaining Branches only internally."""

    fork_node_id: ID
    merge_node_id: ID
    branches: tuple[Branch, ...]

    @property
    def node_ids(self) -> frozenset[ID]:
        return frozenset(node_id for branch in self.branches for node_id in branch.node_ids)

    @property
    def terminal_ids(self) -> frozenset[ID]:
        return frozenset(branch.node_ids[-1] for branch in self.branches)


class _Analyzer:
    """Recursive DAG data-flow evaluator for one validated graph shape."""

    def __init__(
        self,
        *,
        node_by_id: Mapping[ID, PlanNode],
        stable_gate_by_node: Mapping[ID, ID],
        incoming_exploration: Mapping[ID, tuple[ID, ...]],
        branch_groups: Mapping[tuple[ID, ID], _BranchGroup],
        branch_by_node: Mapping[ID, _BranchGroup],
        evaluator_by_group: Mapping[tuple[ID, ID], ID],
        evaluator_group_by_node: Mapping[ID, _BranchGroup],
    ) -> None:
        self._node_by_id = node_by_id
        self._stable_gate_by_node = stable_gate_by_node
        self._incoming_exploration = incoming_exploration
        self._branch_groups = branch_groups
        self._branch_by_node = branch_by_node
        self._evaluator_by_group = evaluator_by_group
        self._evaluator_group_by_node = evaluator_group_by_node
        self._ready: dict[ID, GateFamily] = {}
        self._completed: dict[ID, GateFamily] = {}
        self._selection_success: dict[ID, GateFamily] = {}
        self._visiting: set[ID] = set()

    def ready(self, node_id: ID) -> GateFamily:
        """Return the Gate family needed before ``node_id`` can be scheduled."""
        cached = self._ready.get(node_id)
        if cached is not None:
            return cached
        self._enter(node_id)
        try:
            node = self._node(node_id)
            if node.kind is PlanNodeKind.EVALUATOR:
                group = self._evaluator_group_by_node.get(node_id)
                family = (
                    self._evaluator_ready(node, group)
                    if group is not None
                    else self._ordinary_ready(node)
                )
            elif node.kind is PlanNodeKind.MERGE:
                group = self._group_for_merge(node_id)
                family = (
                    self._merge_ready(node, group)
                    if group is not None
                    else self._ordinary_ready(node)
                )
            else:
                family = self._ordinary_ready(node)
            self._ready[node_id] = family
            return family
        finally:
            self._visiting.remove(node_id)

    def completed(self, node_id: ID) -> GateFamily:
        """Return the Gate family needed for successful completion of a node."""
        cached = self._completed.get(node_id)
        if cached is not None:
            return cached
        ready = self.ready(node_id)
        stable_gate = self._stable_gate_by_node.get(node_id)
        family = ready if stable_gate is None else _and(ready, _singleton(stable_gate))
        self._completed[node_id] = family
        return family

    def selection_success(self, evaluator_id: ID) -> GateFamily:
        """Return the raw Evaluator completion plus a viable Branch selection."""
        cached = self._selection_success.get(evaluator_id)
        if cached is not None:
            return cached
        group = self._evaluator_group_by_node.get(evaluator_id)
        if group is None:
            family = self.completed(evaluator_id)
        else:
            viable = FALSE
            for branch in group.branches:
                viable = _or(viable, self._branch_success(branch))
            family = _and(self.completed(evaluator_id), viable)
        self._selection_success[evaluator_id] = family
        return family

    def _dependency_completion(self, dependency_id: ID) -> GateFamily:
        """Use selection success when a dependency is a branch Evaluator."""
        if dependency_id in self._evaluator_group_by_node:
            return self.selection_success(dependency_id)
        return self.completed(dependency_id)

    def _ordinary_ready(self, node: PlanNode) -> GateFamily:
        family = TRUE
        for source_id in self._incoming_exploration_sources(node.plan_node_id):
            family = _and(family, self._dependency_completion(source_id))
        for dependency_id in node.required_dependency_ids:
            family = _and(family, self._dependency_completion(dependency_id))
        return family

    def _evaluator_ready(
        self,
        node: PlanNode,
        group: _BranchGroup,
    ) -> GateFamily:
        # ready_nodes permits an Evaluator after each Branch is terminal.  A
        # Branch is terminal either when every node completed (viable) or when
        # it contains a failure; the failure route stops at the failed node.
        family = TRUE
        for source_id in self._incoming_exploration_sources(node.plan_node_id):
            family = _and(family, self._dependency_completion(source_id))
        branch_nodes = group.node_ids
        for branch in group.branches:
            family = _and(family, self._branch_terminal(branch))
        for dependency_id in node.required_dependency_ids:
            if dependency_id not in branch_nodes:
                family = _and(family, self._dependency_completion(dependency_id))
        return family

    def _merge_ready(
        self,
        node: PlanNode,
        group: _BranchGroup,
    ) -> GateFamily:
        # Merge scheduling consumes the Evaluator's persisted selection.  Its
        # Gate prerequisites therefore include ordinary dependencies (most
        # importantly the Evaluator) and an OR over viable, all-completed
        # Branches.  Branch IDs never enter the proof family.
        family = self._ordinary_ready(node)
        viable = FALSE
        for branch in group.branches:
            viable = _or(viable, self._branch_success(branch))
        return _and(family, viable)

    def _branch_success(self, branch: Branch) -> GateFamily:
        family = TRUE
        for node_id in branch.node_ids:
            family = _and(family, self.completed(node_id))
        return family

    def _branch_terminal(self, branch: Branch) -> GateFamily:
        # A failed node contributes only its Ready family.  Appending its Gate
        # would incorrectly claim that a failed Gate must have passed before
        # the Evaluator can observe a terminal failed Branch.
        prefix = TRUE
        terminal = FALSE
        for node_id in branch.node_ids:
            terminal = _or(terminal, _and(prefix, self.ready(node_id)))
            prefix = _and(prefix, self.completed(node_id))
        return _or(terminal, self._branch_success(branch))

    def _group_for_merge(self, merge_node_id: ID) -> _BranchGroup | None:
        matches = tuple(
            group for group in self._branch_groups.values() if group.merge_node_id == merge_node_id
        )
        if len(matches) > 1:
            raise ProcessProofUnavailable(
                f"Merge {merge_node_id} has ambiguous exploration Branch groups"
            )
        return matches[0] if matches else None

    def _incoming_exploration_sources(self, node_id: ID) -> tuple[ID, ...]:
        # The caller has already validated the PlanRevision's edge/dependency
        # invariants; this list mirrors _exploration_dependencies_completed.
        return self._incoming_exploration.get(node_id, ())

    def _node(self, node_id: ID) -> PlanNode:
        try:
            return self._node_by_id[node_id]
        except KeyError as error:  # pragma: no cover - PlanRevision validates IDs
            raise ProcessProofUnavailable(f"unknown PlanNode {node_id}") from error

    def _enter(self, node_id: ID) -> None:
        if node_id in self._visiting:
            raise ProcessProofUnavailable(
                f"Gate precondition graph is cyclic at PlanNode {node_id}"
            )
        self._visiting.add(node_id)


def _final_success_family(plan: PlanRevision, analyzer: _Analyzer) -> GateFamily:
    outgoing = {node.plan_node_id: 0 for node in plan.nodes}
    for edge in plan.edges:
        outgoing[edge.source_node_id] += 1
    terminals = tuple(node for node in plan.nodes if outgoing[node.plan_node_id] == 0)
    if not terminals:
        raise ProcessProofUnavailable("PlanRevision has no terminal PlanNode")
    if any(
        node.kind is PlanNodeKind.EVALUATOR
        and node.plan_node_id in analyzer._evaluator_group_by_node
        for node in terminals
    ):
        raise ProcessProofUnavailable(
            "a terminal Evaluator with Branch selection has no sound final Gate proof"
        )
    family = TRUE
    for node in terminals:
        family = _and(family, analyzer.completed(node.plan_node_id))
    return family


def _validate_gate_ids(
    node_by_id: Mapping[ID, PlanNode],
    gate_ids: Mapping[ID, ID],
) -> dict[ID, ID]:
    owners = {node_id for node_id, node in node_by_id.items() if node.required_check_ids}
    normalized: dict[ID, ID] = {}
    for raw_node_id, raw_gate_id in gate_ids.items():
        try:
            node_id = normalize_id(raw_node_id)
            gate_id = normalize_id(raw_gate_id)
        except (TypeError, ValueError) as error:
            raise ProcessProofUnavailable(
                "Gate owner and stable Gate IDs must be valid IDs"
            ) from error
        if node_id in normalized:
            raise ProcessProofUnavailable(f"Gate owner {node_id} appears more than once")
        normalized[node_id] = gate_id
    if set(normalized) != owners:
        missing = sorted(str(node_id) for node_id in owners - set(normalized))
        extra = sorted(str(node_id) for node_id in set(normalized) - owners)
        raise ProcessProofUnavailable(
            f"stable Gate owner mapping must exactly cover required-Check owners; "
            f"missing={missing}, extra={extra}"
        )
    gate_owners: dict[ID, ID] = {}
    for node_id, gate_id in normalized.items():
        previous = gate_owners.get(gate_id)
        if previous is not None:
            raise ProcessProofUnavailable(
                f"stable Gate ID {gate_id} is assigned to PlanNodes {previous} and {node_id}"
            )
        gate_owners[gate_id] = node_id
    return normalized


def _reject_unsupported_edges(plan: PlanRevision) -> None:
    if any(edge.edge_type is EdgeType.CONDITIONAL for edge in plan.edges):
        raise ProcessProofUnavailable(
            "conditional Edges require a runtime condition proof not supported here"
        )


def _build_branch_groups(
    plan: PlanRevision,
    node_by_id: Mapping[ID, PlanNode],
) -> tuple[
    dict[tuple[ID, ID], _BranchGroup],
    dict[ID, _BranchGroup],
    dict[ID, Branch],
]:
    grouped: dict[tuple[ID, ID], list[Branch]] = defaultdict(list)
    branch_by_node: dict[ID, _BranchGroup] = {}
    branch_owner_by_node: dict[ID, Branch] = {}
    for branch in plan.branches:
        fork = node_by_id.get(branch.fork_node_id)
        merge = node_by_id.get(branch.merge_node_id)
        if fork is None or merge is None:
            raise ProcessProofUnavailable(
                f"Branch {branch.branch_id} references an unknown fork or merge node"
            )
        if fork.kind is not PlanNodeKind.FORK or merge.kind not in {
            PlanNodeKind.EVALUATOR,
            PlanNodeKind.MERGE,
        }:
            raise ProcessProofUnavailable(
                f"Branch {branch.branch_id} has unsupported fork/merge node kinds"
            )
        key = (branch.fork_node_id, branch.merge_node_id)
        grouped[key].append(branch)
    for key, branches in grouped.items():
        if len(branches) < 2:
            raise ProcessProofUnavailable(
                f"Branch group {key[0]}->{key[1]} requires at least two Branches"
            )
        group = _BranchGroup(key[0], key[1], tuple(branches))
        for branch in branches:
            for node_id in branch.node_ids:
                previous = branch_by_node.get(node_id)
                if previous is not None and previous != group:
                    raise ProcessProofUnavailable(
                        f"PlanNode {node_id} belongs to multiple exploration Branch groups"
                    )
                branch_by_node[node_id] = group
                previous_owner = branch_owner_by_node.get(node_id)
                if previous_owner is not None and previous_owner != branch:
                    raise ProcessProofUnavailable(
                        f"PlanNode {node_id} belongs to multiple exploration Branches"
                    )
                branch_owner_by_node[node_id] = branch
    return (
        {key: _BranchGroup(key[0], key[1], tuple(branches)) for key, branches in grouped.items()},
        _rebind_branch_groups(branch_by_node, grouped),
        branch_owner_by_node,
    )


def _rebind_branch_groups(
    branch_by_node: Mapping[ID, _BranchGroup],
    grouped: Mapping[tuple[ID, ID], list[Branch]],
) -> dict[ID, _BranchGroup]:
    """Bind node ownership to the final immutable group instances."""
    groups = {
        key: _BranchGroup(key[0], key[1], tuple(branches)) for key, branches in grouped.items()
    }
    # ``branch_by_node`` is only used for its validated keys; derive values by
    # looking up each branch's fork/merge pair so no Branch ID enters output.
    result: dict[ID, _BranchGroup] = {}
    for node_id, provisional in branch_by_node.items():
        result[node_id] = groups[(provisional.fork_node_id, provisional.merge_node_id)]
    return result


def _build_evaluator_index(
    nodes: tuple[PlanNode, ...],
    branch_groups: Mapping[tuple[ID, ID], _BranchGroup],
    branch_by_node: Mapping[ID, _BranchGroup],
) -> tuple[dict[tuple[ID, ID], ID], dict[ID, _BranchGroup]]:
    evaluator_by_group: dict[tuple[ID, ID], ID] = {}
    evaluator_group_by_node: dict[ID, _BranchGroup] = {}
    for node in nodes:
        if node.kind is not PlanNodeKind.EVALUATOR:
            continue
        dependencies = set(node.required_dependency_ids)
        matches = tuple(
            (key, group)
            for key, group in branch_groups.items()
            if group.terminal_ids.issubset(dependencies)
        )
        branch_dependencies = dependencies.intersection(branch_by_node)
        if not matches:
            if branch_dependencies:
                raise ProcessProofUnavailable(
                    f"Evaluator {node.plan_node_id} has incomplete Branch selection inputs"
                )
            continue
        if len(matches) != 1:
            raise ProcessProofUnavailable(
                f"Evaluator {node.plan_node_id} matches multiple exploration Branch groups"
            )
        key, group = matches[0]
        previous = evaluator_by_group.get(key)
        if previous is not None:
            raise ProcessProofUnavailable(
                f"Branch group {key[0]}->{key[1]} has multiple Evaluators"
            )
        evaluator_by_group[key] = node.plan_node_id
        evaluator_group_by_node[node.plan_node_id] = group
    return evaluator_by_group, evaluator_group_by_node


def _validate_merge_selection_inputs(
    nodes: tuple[PlanNode, ...],
    node_by_id: Mapping[ID, PlanNode],
    branch_groups: Mapping[tuple[ID, ID], _BranchGroup],
    branch_by_node: Mapping[ID, _BranchGroup],
    branch_owner_by_node: Mapping[ID, Branch],
    evaluator_by_group: Mapping[tuple[ID, ID], ID],
) -> None:
    for key, group in branch_groups.items():
        merge = node_by_id[group.merge_node_id]
        evaluator_id = evaluator_by_group.get(key)
        if evaluator_id is None:
            raise ProcessProofUnavailable(
                f"Branch group {key[0]}->{key[1]} has no validated Evaluator"
            )
        if merge.kind is PlanNodeKind.MERGE:
            if evaluator_id not in merge.required_dependency_ids:
                raise ProcessProofUnavailable(
                    f"Merge {merge.plan_node_id} does not depend on its Evaluator"
                )
            if set(merge.required_dependency_ids).intersection(group.node_ids):
                raise ProcessProofUnavailable(
                    f"Merge {merge.plan_node_id} depends on unselected Branch interior input"
                )
    for node in nodes:
        if node.kind in {PlanNodeKind.EVALUATOR, PlanNodeKind.MERGE}:
            continue
        owner = branch_owner_by_node.get(node.plan_node_id)
        for dependency_id in node.required_dependency_ids:
            dependency_owner = branch_owner_by_node.get(dependency_id)
            if owner is None:
                if dependency_owner is not None:
                    raise ProcessProofUnavailable(
                        f"PlanNode {node.plan_node_id} crosses an exploration Branch interior"
                    )
            elif dependency_owner is not None and dependency_owner.branch_id != owner.branch_id:
                raise ProcessProofUnavailable(
                    f"PlanNode {node.plan_node_id} crosses an exploration Branch interior"
                )


def _edge_index(plan: PlanRevision) -> dict[ID, tuple[ID, ...]]:
    return {
        node_id: tuple(
            edge.source_node_id
            for edge in plan.edges
            if edge.target_node_id == node_id and edge.edge_type is EdgeType.EXPLORATION
        )
        for node_id in (node.plan_node_id for node in plan.nodes)
    }


def _singleton(gate_id: ID) -> GateFamily:
    return frozenset({frozenset({gate_id})})


def _and(left: GateFamily, right: GateFamily) -> GateFamily:
    """Conjoin families with Cartesian union and antichain reduction."""
    if not left or not right:
        return FALSE
    if left == TRUE:
        return right
    if right == TRUE:
        return left
    result: set[frozenset[ID]] = set()
    for left_set in left:
        for right_set in right:
            _insert_minimal(result, left_set.union(right_set))
    return frozenset(result)


def _or(left: GateFamily, right: GateFamily) -> GateFamily:
    """Disjoin families and retain only minimal Gate alternatives."""
    if not left:
        return right
    if not right:
        return left
    result: set[frozenset[ID]] = set(left)
    for candidate in right:
        _insert_minimal(result, candidate)
    return frozenset(result)


def _insert_minimal(result: set[frozenset[ID]], candidate: frozenset[ID]) -> None:
    if any(existing <= candidate for existing in result):
        return
    supersets = {existing for existing in result if candidate < existing}
    result.difference_update(supersets)
    result.add(candidate)
    if len(result) > MAX_FAMILY_SIZE:
        raise ProcessProofUnavailable(
            f"Precondition family exceeds {MAX_FAMILY_SIZE} alternatives; "
            "this graph exceeds the bounded structural proof capability"
        )


def _bounded_family(family: GateFamily) -> GateFamily:
    result: set[frozenset[ID]] = set()
    for candidate in family:
        _insert_minimal(result, frozenset(candidate))
    return frozenset(result)


def _freeze_family_mapping(
    values: Mapping[ID, GateFamily],
    field_name: str,
) -> Mapping[ID, GateFamily]:
    try:
        frozen = {key: _bounded_family(value) for key, value in values.items()}
    except (TypeError, ValueError) as error:
        raise ProcessProofUnavailable(f"{field_name} must contain Gate families") from error
    return MappingProxyType(frozen)
