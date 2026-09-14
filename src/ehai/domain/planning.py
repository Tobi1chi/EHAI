"""Immutable P1 plan graph models and graph invariants."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Self

from ehai import ID, new_id, normalize_id, utc_now
from ehai.domain.goal import CompletionContract
from ehai.domain.workers import SessionPolicy, WorkerCapability

if TYPE_CHECKING:
    from ehai.domain.adoptions import ResultAdoption
    from ehai.domain.checking import GateDecision

_CONTROLLED_STATE = object()


class PlanInvariantError(ValueError):
    """Raised when a PlanGraph is structurally invalid."""


class PlanTransitionError(ValueError):
    """Raised when a Plan state transition is invalid."""


class EdgeType(StrEnum):
    """Supported P1 PlanGraph relationship types."""

    DEPENDENCY = "dependency"
    EXPLORATION = "exploration"
    CONDITIONAL = "conditional"
    MERGE = "merge"


class PlanNodeKind(StrEnum):
    """Structural roles a PlanNode can have in an approved execution graph."""

    WORK = "work"
    FORK = "fork"
    EVALUATOR = "evaluator"
    MERGE = "merge"
    REVIEWER = "reviewer"


class PlanNodeStatus(StrEnum):
    """PlanNode lifecycle states."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    CANDIDATE = "candidate"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    PRUNED = "pruned"


class BranchStatus(StrEnum):
    """Exploration branch selection states."""

    ACTIVE = "active"
    SELECTED = "selected"
    PRUNED = "pruned"


class PlanRevisionStatus(StrEnum):
    """Approval state for a PlanRevision."""

    DRAFT = "draft"
    APPROVED = "approved"


@dataclass(frozen=True, slots=True)
class PlanNode:
    """A schedulable or evaluative unit in a PlanGraph."""

    plan_node_id: ID
    title: str
    instruction: str
    kind: PlanNodeKind = PlanNodeKind.WORK
    required_dependency_ids: tuple[ID, ...] = ()
    required_check_ids: tuple[ID, ...] = ()
    required_capabilities: frozenset[WorkerCapability] = frozenset()
    session_policy: SessionPolicy = SessionPolicy.NEW
    status: PlanNodeStatus = PlanNodeStatus.PENDING
    _state_token: InitVar[object | None] = None

    def __post_init__(self, _state_token: object | None) -> None:
        object.__setattr__(self, "kind", PlanNodeKind(self.kind))
        object.__setattr__(self, "status", PlanNodeStatus(self.status))
        object.__setattr__(self, "session_policy", SessionPolicy(self.session_policy))
        object.__setattr__(
            self,
            "plan_node_id",
            _validated_id(self.plan_node_id, "PlanNode", "plan_node_id"),
        )
        owner = f"PlanNode {self.plan_node_id}"
        object.__setattr__(
            self,
            "required_dependency_ids",
            tuple(
                _validated_id(dependency_id, owner, "required_dependency_id")
                for dependency_id in self.required_dependency_ids
            ),
        )
        object.__setattr__(
            self,
            "required_check_ids",
            tuple(
                _validated_id(check_id, owner, "required_check_id")
                for check_id in self.required_check_ids
            ),
        )
        capabilities = frozenset(self.required_capabilities)
        if not all(isinstance(item, WorkerCapability) for item in capabilities):
            raise PlanInvariantError(
                f"{owner} required_capabilities must contain WorkerCapability values"
            )
        object.__setattr__(self, "required_capabilities", capabilities)
        if not self.title.strip() or not self.instruction.strip():
            raise PlanInvariantError(f"{owner} requires a title and instruction")
        if len(set(self.required_dependency_ids)) != len(self.required_dependency_ids):
            raise PlanInvariantError(f"{owner} contains duplicate dependencies")
        if self.plan_node_id in self.required_dependency_ids:
            raise PlanInvariantError(f"{owner} cannot depend on itself")
        if len(set(self.required_check_ids)) != len(self.required_check_ids):
            raise PlanInvariantError(f"{owner} contains duplicate required Checks")
        if self.status is PlanNodeStatus.COMPLETED and _state_token is not _CONTROLLED_STATE:
            raise PlanInvariantError(f"{owner} completed state requires complete() or rehydrate()")

    @classmethod
    def rehydrate(
        cls,
        *,
        plan_node_id: ID,
        title: str,
        instruction: str,
        kind: PlanNodeKind,
        required_dependency_ids: Iterable[ID],
        required_check_ids: Iterable[ID],
        status: PlanNodeStatus,
        required_capabilities: Iterable[WorkerCapability] = (),
        session_policy: SessionPolicy = SessionPolicy.NEW,
    ) -> Self:
        """Restore a validated PlanNode snapshot from trusted persistence data."""
        return cls(
            plan_node_id=plan_node_id,
            title=title,
            instruction=instruction,
            kind=kind,
            required_dependency_ids=tuple(required_dependency_ids),
            required_check_ids=tuple(required_check_ids),
            required_capabilities=frozenset(required_capabilities),
            session_policy=session_policy,
            status=status,
            _state_token=_CONTROLLED_STATE,
        )

    def mark_ready(self) -> Self:
        """Make a pending node schedulable after dependencies are satisfied."""
        return self._transition(PlanNodeStatus.READY, allowed_from=(PlanNodeStatus.PENDING,))

    def start(self) -> Self:
        """Start one Attempt for a ready node."""
        return self._transition(PlanNodeStatus.RUNNING, allowed_from=(PlanNodeStatus.READY,))

    def submit_candidate(self) -> Self:
        """Record that a Worker returned a candidate, not completion."""
        return self._transition(PlanNodeStatus.CANDIDATE, allowed_from=(PlanNodeStatus.RUNNING,))

    def begin_verification(self) -> Self:
        """Move a candidate into Check execution."""
        return self._transition(PlanNodeStatus.VERIFYING, allowed_from=(PlanNodeStatus.CANDIDATE,))

    def submit_adopted_candidate(self, adoption: ResultAdoption) -> Self:
        """Receive an accepted prior result after readiness, without claiming Worker execution."""
        if adoption.target_plan_node_id != self.plan_node_id:
            raise PlanTransitionError("Adopted result belongs to another target node")
        if self.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            raise PlanTransitionError("Only implementation results can bypass new Worker execution")
        if not adoption.evidence:
            raise PlanTransitionError("Adopted result requires accepted evidence")
        return self._transition(PlanNodeStatus.CANDIDATE, allowed_from=(PlanNodeStatus.READY,))

    def accept_intermediate(self) -> Self:
        """Accept an unchecked intermediate result without satisfying the Goal."""
        if self.required_check_ids:
            raise PlanTransitionError("A node with required Checks must pass its Gate")
        return self._transition(PlanNodeStatus.COMPLETED, allowed_from=(PlanNodeStatus.CANDIDATE,))

    def complete(self, decision: GateDecision) -> Self:
        """Complete a verifying node only from a passing, evidenced Gate."""
        owner = f"PlanNode {self.plan_node_id}"
        if not decision.passed:
            raise PlanTransitionError(
                f"{owner} cannot complete from failed Gate {decision.gate_id}"
            )
        if not decision.required_check_ids or not decision.evidence_artifact_ids:
            raise PlanTransitionError(f"{owner} Gate {decision.gate_id} lacks checks or evidence")
        if decision.plan_node_id != self.plan_node_id:
            raise PlanTransitionError(
                f"{owner} cannot use Gate {decision.gate_id} for node {decision.plan_node_id}"
            )
        if not set(self.required_check_ids).issubset(decision.required_check_ids):
            raise PlanTransitionError(f"{owner} Gate {decision.gate_id} omits a required Check")
        return self._transition(PlanNodeStatus.COMPLETED, allowed_from=(PlanNodeStatus.VERIFYING,))

    def fail(self) -> Self:
        """Fail a node whose execution or verification did not succeed."""
        return self._transition(
            PlanNodeStatus.FAILED,
            allowed_from=(
                PlanNodeStatus.RUNNING,
                PlanNodeStatus.CANDIDATE,
                PlanNodeStatus.VERIFYING,
            ),
        )

    def retry(self) -> Self:
        """Return a failed node to ready for another Attempt."""
        return self._transition(PlanNodeStatus.READY, allowed_from=(PlanNodeStatus.FAILED,))

    def block(self) -> Self:
        """Wait for a durable intervention, without treating the route as failed."""
        return self._transition(PlanNodeStatus.BLOCKED, allowed_from=(PlanNodeStatus.RUNNING,))

    def unblock(self) -> Self:
        """Recheck dependencies after the user resolves the corresponding intervention."""
        return self._transition(PlanNodeStatus.PENDING, allowed_from=(PlanNodeStatus.BLOCKED,))

    def reopen_for_rework(self) -> Self:
        """Reopen a node for an approved-boundary rework without changing its structure."""
        return self._transition(
            PlanNodeStatus.PENDING,
            allowed_from=(
                PlanNodeStatus.COMPLETED,
                PlanNodeStatus.FAILED,
                PlanNodeStatus.CANDIDATE,
                PlanNodeStatus.VERIFYING,
                PlanNodeStatus.READY,
                PlanNodeStatus.PRUNED,
                PlanNodeStatus.PENDING,
            ),
        )

    def prune(self) -> Self:
        """Prune an unexecuted node while preserving the immutable node record."""
        return self._transition(
            PlanNodeStatus.PRUNED,
            allowed_from=(PlanNodeStatus.PENDING, PlanNodeStatus.READY),
        )

    def _transition(
        self,
        target: PlanNodeStatus,
        *,
        allowed_from: tuple[PlanNodeStatus, ...],
    ) -> Self:
        if self.status not in allowed_from:
            raise PlanTransitionError(
                f"PlanNode {self.plan_node_id} cannot transition from {self.status} to {target}"
            )
        return replace(self, status=target, _state_token=_CONTROLLED_STATE)


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed PlanGraph relationship."""

    edge_id: ID
    source_node_id: ID
    target_node_id: ID
    edge_type: EdgeType
    branch_id: ID | None = None
    condition: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_type", EdgeType(self.edge_type))
        object.__setattr__(self, "edge_id", _validated_id(self.edge_id, "Edge", "edge_id"))
        object.__setattr__(
            self,
            "source_node_id",
            _validated_id(self.source_node_id, f"Edge {self.edge_id}", "source_node_id"),
        )
        object.__setattr__(
            self,
            "target_node_id",
            _validated_id(self.target_node_id, f"Edge {self.edge_id}", "target_node_id"),
        )
        owner = f"Edge {self.edge_id}"
        if self.branch_id is not None:
            object.__setattr__(
                self,
                "branch_id",
                _validated_id(self.branch_id, owner, "branch_id"),
            )
        if self.source_node_id == self.target_node_id:
            raise PlanInvariantError(f"{owner} cannot be a self-loop")
        if self.edge_type in (EdgeType.EXPLORATION, EdgeType.MERGE) and self.branch_id is None:
            raise PlanInvariantError(f"{owner} {self.edge_type} requires a Branch ID")
        if self.edge_type is EdgeType.CONDITIONAL:
            if self.condition is None or not self.condition.strip():
                raise PlanInvariantError(f"{owner} conditional relationship requires a condition")
        elif self.condition is not None:
            raise PlanInvariantError(
                f"{owner} condition is only valid on conditional relationships"
            )


@dataclass(frozen=True, slots=True)
class Branch:
    """One exploration path between a fork and a merge node."""

    branch_id: ID
    label: str
    fork_node_id: ID
    node_ids: tuple[ID, ...]
    merge_node_id: ID
    status: BranchStatus = BranchStatus.ACTIVE
    _state_token: InitVar[object | None] = None

    def __post_init__(self, _state_token: object | None) -> None:
        object.__setattr__(self, "status", BranchStatus(self.status))
        object.__setattr__(
            self,
            "branch_id",
            _validated_id(self.branch_id, "Branch", "branch_id"),
        )
        owner = f"Branch {self.branch_id}"
        object.__setattr__(
            self,
            "fork_node_id",
            _validated_id(self.fork_node_id, owner, "fork_node_id"),
        )
        object.__setattr__(
            self,
            "node_ids",
            tuple(_validated_id(node_id, owner, "node_id") for node_id in self.node_ids),
        )
        object.__setattr__(
            self,
            "merge_node_id",
            _validated_id(self.merge_node_id, owner, "merge_node_id"),
        )
        if not self.label.strip():
            raise PlanInvariantError(f"{owner} requires a label")
        if not self.node_ids:
            raise PlanInvariantError(f"{owner} must contain at least one exploration node")
        if len(set(self.node_ids)) != len(self.node_ids):
            raise PlanInvariantError(f"{owner} contains duplicate PlanNode IDs")
        if self.fork_node_id == self.merge_node_id:
            raise PlanInvariantError(f"{owner} must have different fork and merge nodes")
        if self.fork_node_id in self.node_ids or self.merge_node_id in self.node_ids:
            raise PlanInvariantError(f"{owner} cannot include its fork or merge in branch nodes")
        if self.status is not BranchStatus.ACTIVE and _state_token is not _CONTROLLED_STATE:
            raise PlanInvariantError(
                f"{owner} selected/pruned state requires a transition or rehydrate()"
            )

    @classmethod
    def rehydrate(
        cls,
        *,
        branch_id: ID,
        label: str,
        fork_node_id: ID,
        node_ids: Iterable[ID],
        merge_node_id: ID,
        status: BranchStatus,
    ) -> Self:
        """Restore a validated Branch snapshot from trusted persistence data."""
        return cls(
            branch_id=branch_id,
            label=label,
            fork_node_id=fork_node_id,
            node_ids=tuple(node_ids),
            merge_node_id=merge_node_id,
            status=status,
            _state_token=_CONTROLLED_STATE,
        )

    def select(self) -> Self:
        """Select an active branch without deleting its path."""
        return self._transition(BranchStatus.SELECTED)

    def prune(self) -> Self:
        """Prune an active branch while preserving every historical node reference."""
        return self._transition(BranchStatus.PRUNED)

    def reopen_selection(self) -> Self:
        """Invalidate a prior branch selection and return the branch to the active set."""
        if self.status is BranchStatus.ACTIVE:
            return self
        return replace(self, status=BranchStatus.ACTIVE, _state_token=_CONTROLLED_STATE)

    def _transition(self, target: BranchStatus) -> Self:
        if self.status is not BranchStatus.ACTIVE:
            raise PlanTransitionError(
                f"Branch {self.branch_id} cannot transition from {self.status} to {target}"
            )
        return replace(self, status=target, _state_token=_CONTROLLED_STATE)


@dataclass(frozen=True, slots=True)
class PlanPhase:
    """One approved execution phase ending in a Reviewer-owned Gate."""

    phase_id: ID
    title: str
    node_ids: tuple[ID, ...]
    reviewer_node_id: ID
    gate_node_id: ID
    rework_node_ids: tuple[ID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase_id", normalize_id(self.phase_id))
        owner = f"PlanPhase {self.phase_id}"
        title = self.title.strip()
        if not title:
            raise PlanInvariantError(f"{owner} requires a title")
        object.__setattr__(self, "title", title)
        node_ids = tuple(_validated_id(node_id, owner, "node_id") for node_id in self.node_ids)
        if not node_ids or len(set(node_ids)) != len(node_ids):
            raise PlanInvariantError(f"{owner} requires unique PlanNode IDs")
        object.__setattr__(self, "node_ids", node_ids)
        object.__setattr__(
            self,
            "reviewer_node_id",
            _validated_id(self.reviewer_node_id, owner, "reviewer_node_id"),
        )
        object.__setattr__(
            self,
            "gate_node_id",
            _validated_id(self.gate_node_id, owner, "gate_node_id"),
        )
        if self.reviewer_node_id not in node_ids or self.gate_node_id not in node_ids:
            raise PlanInvariantError(f"{owner} Reviewer and Gate nodes must belong to the Phase")
        if self.reviewer_node_id != self.gate_node_id:
            raise PlanInvariantError(f"{owner} Gate must be owned by its Reviewer node")
        rework = tuple(
            _validated_id(node_id, owner, "rework_node_id") for node_id in self.rework_node_ids
        )
        if not rework or len(set(rework)) != len(rework) or not set(rework).issubset(node_ids):
            raise PlanInvariantError(f"{owner} requires unique in-Phase rework nodes")
        if self.reviewer_node_id in rework:
            raise PlanInvariantError(f"{owner} Reviewer cannot be a rework target")
        object.__setattr__(self, "rework_node_ids", rework)


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """An immutable, versioned PlanGraph tied to a CompletionContract version."""

    plan_revision_id: ID
    goal_id: ID
    version: int
    completion_contract_id: ID
    completion_contract_version: int
    nodes: tuple[PlanNode, ...]
    edges: tuple[Edge, ...]
    branches: tuple[Branch, ...]
    created_at: datetime
    phases: tuple[PlanPhase, ...] = ()
    status: PlanRevisionStatus = PlanRevisionStatus.DRAFT
    approved_at: datetime | None = None
    supersedes_plan_revision_id: ID | None = None
    design_document: str | None = None
    _state_token: InitVar[object | None] = None

    def __post_init__(self, _state_token: object | None) -> None:
        object.__setattr__(self, "status", PlanRevisionStatus(self.status))
        object.__setattr__(
            self,
            "plan_revision_id",
            _validated_id(self.plan_revision_id, "PlanRevision", "plan_revision_id"),
        )
        object.__setattr__(
            self,
            "goal_id",
            _validated_id(self.goal_id, f"PlanRevision {self.plan_revision_id}", "goal_id"),
        )
        object.__setattr__(
            self,
            "completion_contract_id",
            _validated_id(
                self.completion_contract_id,
                f"PlanRevision {self.plan_revision_id}",
                "completion_contract_id",
            ),
        )
        owner = f"PlanRevision {self.plan_revision_id}"
        if self.design_document is not None:
            if not isinstance(self.design_document, str) or not self.design_document.strip():
                raise PlanInvariantError(f"{owner} design document must not be blank")
            if len(self.design_document) > 64_000:
                raise PlanInvariantError(f"{owner} design document exceeds 64000 characters")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        object.__setattr__(self, "branches", tuple(self.branches))
        object.__setattr__(self, "phases", tuple(self.phases))
        if self.supersedes_plan_revision_id is not None:
            object.__setattr__(
                self,
                "supersedes_plan_revision_id",
                _validated_id(
                    self.supersedes_plan_revision_id,
                    owner,
                    "supersedes_plan_revision_id",
                ),
            )
        object.__setattr__(self, "created_at", _utc(self.created_at, owner))
        if self.approved_at is not None:
            object.__setattr__(self, "approved_at", _utc(self.approved_at, owner))
        if self.version < 1:
            raise PlanInvariantError(f"{owner} must have a positive version")
        if self.completion_contract_version < 1:
            raise PlanInvariantError(f"{owner} must reference a positive contract version")
        if self.version == 1 and self.supersedes_plan_revision_id is not None:
            raise PlanInvariantError(f"{owner} version 1 cannot supersede another revision")
        if self.version > 1 and self.supersedes_plan_revision_id is None:
            raise PlanInvariantError(
                f"{owner} version {self.version} must identify its predecessor"
            )
        if self.status is PlanRevisionStatus.DRAFT and self.approved_at is not None:
            raise PlanInvariantError(f"{owner} draft cannot have approval time")
        if self.status is PlanRevisionStatus.APPROVED and self.approved_at is None:
            raise PlanInvariantError(f"{owner} approved state requires approval time")
        if self.status is PlanRevisionStatus.APPROVED and _state_token is not _CONTROLLED_STATE:
            raise PlanInvariantError(f"{owner} approved state requires approve() or rehydrate()")
        if self.approved_at is not None and self.approved_at < self.created_at:
            raise PlanInvariantError(f"{owner} cannot be approved before creation")
        self.validate()

    @classmethod
    def draft(
        cls,
        goal_id: ID,
        completion_contract: CompletionContract,
        nodes: Iterable[PlanNode],
        edges: Iterable[Edge],
        branches: Iterable[Branch] = (),
        phases: Iterable[PlanPhase] = (),
        *,
        plan_revision_id: ID | None = None,
        version: int = 1,
        supersedes_plan_revision_id: ID | None = None,
        created_at: datetime | None = None,
        design_document: str | None = None,
    ) -> Self:
        """Build and validate an unapproved PlanRevision."""
        if completion_contract.goal_id != goal_id:
            raise PlanInvariantError(
                f"PlanRevision for Goal {goal_id} cannot use another Goal's CompletionContract"
            )
        return cls(
            plan_revision_id=plan_revision_id or new_id(),
            goal_id=goal_id,
            version=version,
            completion_contract_id=completion_contract.completion_contract_id,
            completion_contract_version=completion_contract.version,
            nodes=tuple(nodes),
            edges=tuple(edges),
            branches=tuple(branches),
            phases=tuple(phases),
            created_at=created_at or utc_now(),
            supersedes_plan_revision_id=supersedes_plan_revision_id,
            design_document=design_document,
        )

    @classmethod
    def rehydrate(
        cls,
        *,
        plan_revision_id: ID,
        goal_id: ID,
        version: int,
        completion_contract_id: ID,
        completion_contract_version: int,
        nodes: Iterable[PlanNode],
        edges: Iterable[Edge],
        branches: Iterable[Branch],
        created_at: datetime,
        status: PlanRevisionStatus,
        approved_at: datetime | None,
        supersedes_plan_revision_id: ID | None,
        design_document: str | None = None,
        phases: Iterable[PlanPhase] = (),
    ) -> Self:
        """Restore a validated PlanRevision snapshot from trusted persistence data."""
        return cls(
            plan_revision_id=plan_revision_id,
            goal_id=goal_id,
            version=version,
            completion_contract_id=completion_contract_id,
            completion_contract_version=completion_contract_version,
            nodes=tuple(nodes),
            edges=tuple(edges),
            branches=tuple(branches),
            phases=tuple(phases),
            created_at=created_at,
            status=status,
            approved_at=approved_at,
            supersedes_plan_revision_id=supersedes_plan_revision_id,
            design_document=design_document,
            _state_token=_CONTROLLED_STATE,
        )

    def validate(self) -> None:
        """Validate references, dependency declarations, acyclicity, and branches."""
        owner = f"PlanRevision {self.plan_revision_id}"
        if not self.nodes:
            raise PlanInvariantError(f"{owner} must contain at least one PlanNode")
        node_by_id = {node.plan_node_id: node for node in self.nodes}
        branch_by_id = {branch.branch_id: branch for branch in self.branches}
        _require_unique_ids((node.plan_node_id for node in self.nodes), owner, "PlanNode")
        _require_unique_ids((edge.edge_id for edge in self.edges), owner, "Edge")
        _require_unique_ids((branch.branch_id for branch in self.branches), owner, "Branch")
        _require_unique_ids((phase.phase_id for phase in self.phases), owner, "PlanPhase")

        edge_keys: set[tuple[ID, ID, EdgeType, ID | None]] = set()
        for edge in self.edges:
            if edge.source_node_id not in node_by_id or edge.target_node_id not in node_by_id:
                raise PlanInvariantError(
                    f"{owner} Edge {edge.edge_id} references an unknown PlanNode"
                )
            if edge.branch_id is not None and edge.branch_id not in branch_by_id:
                raise PlanInvariantError(
                    f"{owner} Edge {edge.edge_id} references an unknown Branch"
                )
            key = (edge.source_node_id, edge.target_node_id, edge.edge_type, edge.branch_id)
            if key in edge_keys:
                raise PlanInvariantError(f"{owner} contains a duplicate Edge relationship")
            edge_keys.add(key)

        dependency_pairs = {
            (edge.source_node_id, edge.target_node_id)
            for edge in self.edges
            if edge.edge_type is EdgeType.DEPENDENCY
        }
        declared_pairs: set[tuple[ID, ID]] = set()
        for node in self.nodes:
            for dependency_id in node.required_dependency_ids:
                if dependency_id not in node_by_id:
                    raise PlanInvariantError(
                        f"{owner} PlanNode {node.plan_node_id} requires unknown dependency "
                        f"{dependency_id}"
                    )
                declared_pairs.add((dependency_id, node.plan_node_id))
        if dependency_pairs != declared_pairs:
            raise PlanInvariantError(f"{owner} dependency Edges must match required dependencies")

        for node in self.nodes:
            if node.kind is PlanNodeKind.REVIEWER and (
                not node.required_dependency_ids or not node.required_check_ids
            ):
                raise PlanInvariantError(
                    f"{owner} reviewer PlanNode {node.plan_node_id} requires dependencies "
                    "and a Gate"
                )

        _require_acyclic(self.nodes, self.edges, owner=owner)
        self._validate_branches(node_by_id, branch_by_id)
        self._validate_phases(node_by_id)

    def approve(
        self,
        completion_contract: CompletionContract,
        *,
        approved_at: datetime | None = None,
    ) -> Self:
        """Approve this immutable graph against the exact confirmed contract version."""
        owner = f"PlanRevision {self.plan_revision_id}"
        if self.status is not PlanRevisionStatus.DRAFT:
            raise PlanTransitionError(f"{owner} is already approved")
        if not completion_contract.is_confirmed:
            raise PlanTransitionError(f"{owner} requires a confirmed CompletionContract")
        if (
            completion_contract.goal_id != self.goal_id
            or completion_contract.completion_contract_id != self.completion_contract_id
            or completion_contract.version != self.completion_contract_version
        ):
            raise PlanTransitionError(f"{owner} contract identity or version does not match")
        if any(node.status is not PlanNodeStatus.PENDING for node in self.nodes):
            raise PlanTransitionError(f"{owner} cannot approve after node execution started")
        if any(branch.status is not BranchStatus.ACTIVE for branch in self.branches):
            raise PlanTransitionError(f"{owner} cannot approve after branch selection")
        return replace(
            self,
            status=PlanRevisionStatus.APPROVED,
            approved_at=approved_at or utc_now(),
            _state_token=_CONTROLLED_STATE,
        )

    def revise(
        self,
        completion_contract: CompletionContract,
        nodes: Iterable[PlanNode],
        edges: Iterable[Edge],
        branches: Iterable[Branch] = (),
        phases: Iterable[PlanPhase] = (),
        *,
        plan_revision_id: ID | None = None,
        created_at: datetime | None = None,
        design_document: str | None = None,
    ) -> Self:
        """Create a new draft version instead of editing an approved graph."""
        if self.status is not PlanRevisionStatus.APPROVED:
            raise PlanTransitionError(
                f"PlanRevision {self.plan_revision_id} must be approved before replanning"
            )
        return type(self).draft(
            goal_id=self.goal_id,
            completion_contract=completion_contract,
            nodes=nodes,
            edges=edges,
            branches=branches,
            phases=phases,
            plan_revision_id=plan_revision_id,
            version=self.version + 1,
            supersedes_plan_revision_id=self.plan_revision_id,
            created_at=created_at,
            design_document=design_document,
        )

    def _validate_phases(self, node_by_id: dict[ID, PlanNode]) -> None:
        if not self.phases:
            return
        owner = f"PlanRevision {self.plan_revision_id}"
        memberships: dict[ID, int] = {}
        phase_index: dict[ID, int] = {}
        for index, phase in enumerate(self.phases):
            for node_id in phase.node_ids:
                if node_id not in node_by_id:
                    raise PlanInvariantError(f"{owner} PlanPhase references unknown PlanNode")
                if node_id in memberships:
                    raise PlanInvariantError(f"{owner} PlanNode belongs to multiple Phases")
                memberships[node_id] = index
                phase_index[node_id] = index
            reviewer = node_by_id[phase.reviewer_node_id]
            if reviewer.kind is not PlanNodeKind.REVIEWER or not reviewer.required_check_ids:
                raise PlanInvariantError(
                    f"{owner} PlanPhase Reviewer must be a reviewer node with an approved Gate"
                )
            if any(
                node_by_id[node_id].kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}
                for node_id in phase.rework_node_ids
            ):
                raise PlanInvariantError(
                    f"{owner} PlanPhase rework targets must be work or merge nodes"
                )
            reachable = _reverse_reachable(
                phase.reviewer_node_id,
                phase.node_ids,
                self.edges,
            )
            if set(phase.node_ids) != reachable:
                raise PlanInvariantError(
                    f"{owner} every PlanPhase node must feed its Reviewer Gate"
                )
        if set(memberships) != set(node_by_id):
            raise PlanInvariantError(f"{owner} Phases must partition every PlanNode")
        for edge in self.edges:
            source_phase = phase_index[edge.source_node_id]
            target_phase = phase_index[edge.target_node_id]
            if source_phase > target_phase:
                raise PlanInvariantError(f"{owner} edges cannot point back to an earlier Phase")
            if (
                source_phase < target_phase
                and edge.source_node_id != self.phases[source_phase].gate_node_id
            ):
                raise PlanInvariantError(
                    f"{owner} cross-Phase edges must originate at the source Phase Gate"
                )
        for index, phase in enumerate(self.phases[1:], start=1):
            phase_nodes = set(phase.node_ids)
            internal_targets = {
                edge.target_node_id
                for edge in self.edges
                if edge.source_node_id in phase_nodes and edge.target_node_id in phase_nodes
            }
            roots = phase_nodes - internal_targets
            previous_gate = self.phases[index - 1].gate_node_id
            for root in roots:
                if not any(
                    edge.source_node_id == previous_gate
                    and edge.target_node_id == root
                    and edge.edge_type is EdgeType.DEPENDENCY
                    for edge in self.edges
                ):
                    raise PlanInvariantError(
                        f"{owner} every Phase root must depend on the previous Phase Gate"
                    )

    def _validate_branches(
        self,
        node_by_id: dict[ID, PlanNode],
        branch_by_id: dict[ID, Branch],
    ) -> None:
        owner = f"PlanRevision {self.plan_revision_id}"
        branch_nodes_seen: set[ID] = set()
        group_sizes: dict[tuple[ID, ID], int] = {}

        for branch in self.branches:
            fork = node_by_id.get(branch.fork_node_id)
            merge = node_by_id.get(branch.merge_node_id)
            if (
                fork is None
                or merge is None
                or any(node_id not in node_by_id for node_id in branch.node_ids)
            ):
                raise PlanInvariantError(
                    f"{owner} Branch {branch.branch_id} references an unknown node"
                )
            if fork.kind is not PlanNodeKind.FORK or merge.kind not in {
                PlanNodeKind.EVALUATOR,
                PlanNodeKind.MERGE,
            }:
                raise PlanInvariantError(
                    f"{owner} Branch {branch.branch_id} must connect a fork to an "
                    "evaluator or merge node"
                )
            overlap = branch_nodes_seen.intersection(branch.node_ids)
            if overlap:
                raise PlanInvariantError(
                    f"{owner} exploration Branch nodes cannot overlap: {overlap}"
                )
            branch_nodes_seen.update(branch.node_ids)
            group = (branch.fork_node_id, branch.merge_node_id)
            group_sizes[group] = group_sizes.get(group, 0) + 1

            expected_edges = [
                (branch.fork_node_id, branch.node_ids[0], EdgeType.EXPLORATION),
                *[
                    (source, target, EdgeType.DEPENDENCY)
                    for source, target in pairwise(branch.node_ids)
                ],
                (branch.node_ids[-1], branch.merge_node_id, EdgeType.MERGE),
            ]
            for source, target, edge_type in expected_edges:
                if not any(
                    edge.source_node_id == source
                    and edge.target_node_id == target
                    and edge.edge_type is edge_type
                    and edge.branch_id == branch.branch_id
                    for edge in self.edges
                ):
                    raise PlanInvariantError(
                        f"{owner} Branch {branch.branch_id} lacks its {edge_type} path Edge"
                    )

        if any(size < 2 for size in group_sizes.values()):
            raise PlanInvariantError(
                f"{owner} each exploration fork must have at least two Branches"
            )

        branch_by_node = {
            node_id: branch for branch in self.branches for node_id in branch.node_ids
        }
        for edge in self.edges:
            source_branch = branch_by_node.get(edge.source_node_id)
            target_branch = branch_by_node.get(edge.target_node_id)
            if (
                source_branch is not None
                and target_branch is not None
                and source_branch.branch_id != target_branch.branch_id
                and source_branch.fork_node_id == target_branch.fork_node_id
                and source_branch.merge_node_id == target_branch.merge_node_id
            ):
                raise PlanInvariantError(
                    f"{owner} Edge {edge.edge_id} crosses sibling Branch interiors"
                )

        for edge in self.edges:
            if edge.edge_type not in (EdgeType.EXPLORATION, EdgeType.MERGE):
                continue
            assert edge.branch_id is not None
            branch = branch_by_id[edge.branch_id]
            expected = (
                (branch.fork_node_id, branch.node_ids[0])
                if edge.edge_type is EdgeType.EXPLORATION
                else (branch.node_ids[-1], branch.merge_node_id)
            )
            if (edge.source_node_id, edge.target_node_id) != expected:
                raise PlanInvariantError(
                    f"{owner} Edge {edge.edge_id} conflicts with its Branch path"
                )


def _require_unique_ids(ids: Iterable[ID], owner: str, item_name: str) -> None:
    seen: set[ID] = set()
    for item_id in ids:
        if item_id in seen:
            raise PlanInvariantError(f"{owner} contains duplicate {item_name} ID {item_id}")
        seen.add(item_id)


def _reverse_reachable(target: ID, node_ids: tuple[ID, ...], edges: tuple[Edge, ...]) -> set[ID]:
    allowed = set(node_ids)
    incoming: dict[ID, list[ID]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        if edge.source_node_id in allowed and edge.target_node_id in allowed:
            incoming[edge.target_node_id].append(edge.source_node_id)
    reached: set[ID] = set()
    pending = [target]
    while pending:
        node_id = pending.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        pending.extend(incoming[node_id])
    return reached


def _require_acyclic(nodes: tuple[PlanNode, ...], edges: tuple[Edge, ...], *, owner: str) -> None:
    incoming = {node.plan_node_id: 0 for node in nodes}
    outgoing: dict[ID, list[ID]] = {node.plan_node_id: [] for node in nodes}
    for edge in edges:
        incoming[edge.target_node_id] += 1
        outgoing[edge.source_node_id].append(edge.target_node_id)

    available = [node_id for node_id, count in incoming.items() if count == 0]
    visited = 0
    while available:
        node_id = available.pop()
        visited += 1
        for target_id in outgoing[node_id]:
            incoming[target_id] -= 1
            if incoming[target_id] == 0:
                available.append(target_id)

    if visited != len(nodes):
        raise PlanInvariantError(f"{owner} contains an illegal cycle")


def _validated_id(value: ID, owner: str, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise PlanInvariantError(f"{owner} has invalid {field_name}: {error}") from error


def _utc(value: datetime, owner: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PlanInvariantError(f"{owner} timestamp must include a UTC offset")
    return value.astimezone(UTC)
