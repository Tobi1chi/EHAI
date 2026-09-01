from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ehai import ID, new_id
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    ExplorationUsage,
    GraphPatch,
    Planner,
    PlanProposal,
)
from ehai.domain import (
    BranchStatus,
    CheckKind,
    EdgeType,
    Goal,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevisionStatus,
)

NOW = datetime(2026, 8, 31, tzinfo=UTC)


class _Ids:
    def __init__(self, values: tuple[ID, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> ID:
        return next(self._values)


def _goal() -> Goal:
    return Goal.create(new_id(), "produce a verified artifact", created_at=NOW)


def test_deterministic_planner_proposes_one_unapproved_work_node() -> None:
    goal = _goal()
    planner = DeterministicPlanner(clock=lambda: NOW)

    proposal = planner.propose(goal, (NON_EMPTY_ARTIFACT_CRITERION,))

    assert isinstance(planner, Planner)
    assert proposal.contract.goal_id == goal.goal_id
    assert not proposal.contract.is_confirmed
    assert proposal.plan_revision.status is PlanRevisionStatus.DRAFT
    assert len(proposal.plan_revision.nodes) == 1
    node = proposal.plan_revision.nodes[0]
    assert node.kind is PlanNodeKind.WORK
    assert node.status is PlanNodeStatus.PENDING
    assert proposal.check_specs[0].kind is CheckKind.ARTIFACT
    assert proposal.check_specs[0].required
    assert node.required_check_ids == proposal.contract.required_check_ids
    assert goal.completion_contract is None


def test_injected_ids_and_clock_make_proposal_stable() -> None:
    ids = tuple(new_id() for _ in range(4))
    planner = DeterministicPlanner(id_factory=_Ids(ids), clock=lambda: NOW)

    proposal = planner.propose(_goal(), (NON_EMPTY_ARTIFACT_CRITERION,))

    check_id, contract_id, node_id, revision_id = ids
    assert proposal.check_specs[0].check_id == check_id
    assert proposal.contract.completion_contract_id == contract_id
    assert proposal.plan_revision.nodes[0].plan_node_id == node_id
    assert proposal.plan_revision.plan_revision_id == revision_id
    assert proposal.contract.created_at == NOW
    assert proposal.plan_revision.created_at == NOW


def test_plan_proposal_snapshots_specs_and_validates_required_checks() -> None:
    proposal = DeterministicPlanner(clock=lambda: NOW).propose(
        _goal(), (NON_EMPTY_ARTIFACT_CRITERION,)
    )
    specs = list(proposal.check_specs)
    snapshotted = PlanProposal(
        proposal.contract,
        proposal.plan_revision,
        specs,  # type: ignore[arg-type]
    )
    specs.clear()
    assert snapshotted.check_specs == proposal.check_specs

    with pytest.raises(ValueError, match="at least one"):
        PlanProposal(proposal.contract, proposal.plan_revision, ())


@pytest.mark.parametrize(
    "criteria",
    [
        (),
        ("artifact must be application/json",),
        (NON_EMPTY_ARTIFACT_CRITERION, "semantic:looks-good"),
    ],
)
def test_planner_rejects_unsupported_criteria(criteria: tuple[str, ...]) -> None:
    planner = DeterministicPlanner(clock=lambda: NOW)
    with pytest.raises(ValueError, match=r"completion criteria|exactly one P1"):
        planner.propose(_goal(), criteria)


def test_planner_rejects_goal_with_existing_contract() -> None:
    goal = _goal()
    planner = DeterministicPlanner(clock=lambda: NOW)
    proposal = planner.propose(goal, (NON_EMPTY_ARTIFACT_CRITERION,))
    aligned = goal.use_completion_contract(proposal.contract)
    with pytest.raises(ValueError, match="already has"):
        planner.propose(aligned, (NON_EMPTY_ARTIFACT_CRITERION,))


def _exploration_proposal(
    *,
    budget: ExplorationBudget | None = None,
) -> PlanProposal:
    request = ExplorationPlanRequest(
        goal=_goal(),
        criteria=(NON_EMPTY_ARTIFACT_CRITERION,),
        budget=budget or ExplorationBudget(max_attempts=5),
    )
    return DeterministicExplorationPlanner(clock=lambda: NOW).propose(request)


def test_exploration_budget_defaults_and_validates_limits() -> None:
    budget = ExplorationBudget(max_attempts=4)

    assert budget.max_width == 3
    assert budget.max_depth == 2
    assert budget.max_attempts == 4
    with pytest.raises(ValueError, match="max_width"):
        ExplorationBudget(max_attempts=4, max_width=0)
    with pytest.raises(ValueError, match="max_depth"):
        ExplorationBudget(max_attempts=4, max_depth=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_attempts"):
        ExplorationBudget(max_attempts=0)


@pytest.mark.parametrize(
    ("budget", "message"),
    [
        (ExplorationBudget(max_attempts=4, max_width=1), "width"),
        (ExplorationBudget(max_attempts=4, max_depth=1), "depth"),
        (ExplorationBudget(max_attempts=3), "attempts"),
    ],
)
def test_exploration_planner_rejects_insufficient_budget(
    budget: ExplorationBudget,
    message: str,
) -> None:
    if message == "depth":
        usage = ExplorationUsage(width=2, depth=2, attempts=4)
        with pytest.raises(ValueError, match="depth"):
            usage.require_within(budget)
        return
    with pytest.raises(ValueError, match=message):
        _exploration_proposal(budget=budget)


def test_exploration_planner_builds_two_branch_evaluator_merge_graph() -> None:
    proposal = _exploration_proposal()
    plan = proposal.plan_revision
    by_kind = {node.kind: node for node in plan.nodes if node.kind is not PlanNodeKind.WORK}
    work_nodes = tuple(node for node in plan.nodes if node.kind is PlanNodeKind.WORK)

    plan.validate()
    assert proposal.budget == ExplorationBudget(max_attempts=5)
    assert proposal.usage == ExplorationUsage(width=2, depth=1, attempts=5)
    assert len(plan.nodes) == 5
    assert len(work_nodes) == 2
    assert len(plan.branches) == 2
    assert all(branch.status is BranchStatus.ACTIVE for branch in plan.branches)
    assert {edge.edge_type for edge in plan.edges} == {
        EdgeType.DEPENDENCY,
        EdgeType.EXPLORATION,
        EdgeType.MERGE,
    }
    assert sum(edge.edge_type is EdgeType.EXPLORATION for edge in plan.edges) == 2
    assert sum(edge.edge_type is EdgeType.MERGE for edge in plan.edges) == 2
    assert sum(edge.edge_type is EdgeType.DEPENDENCY for edge in plan.edges) == 3

    fork = by_kind[PlanNodeKind.FORK]
    evaluator = by_kind[PlanNodeKind.EVALUATOR]
    merge = by_kind[PlanNodeKind.MERGE]
    assert set(evaluator.required_dependency_ids) == {node.plan_node_id for node in work_nodes}
    assert merge.required_dependency_ids == (evaluator.plan_node_id,)
    assert all(branch.fork_node_id == fork.plan_node_id for branch in plan.branches)
    assert all(branch.merge_node_id == merge.plan_node_id for branch in plan.branches)
    assert proposal.check_specs[0].description == NON_EMPTY_ARTIFACT_CRITERION
    executable = (*work_nodes, evaluator, merge)
    assert all(
        node.required_check_ids == (proposal.check_specs[0].check_id,) for node in executable
    )
    assert fork.required_check_ids == (proposal.check_specs[0].check_id,)


def test_plan_proposal_budget_extension_preserves_legacy_constructor() -> None:
    legacy = DeterministicPlanner(clock=lambda: NOW).propose(
        _goal(), (NON_EMPTY_ARTIFACT_CRITERION,)
    )

    reconstructed = PlanProposal(
        legacy.contract,
        legacy.plan_revision,
        legacy.check_specs,
    )

    assert reconstructed.budget is None
    assert reconstructed.usage is None


def test_graph_patch_creates_new_revision_and_preserves_approved_base() -> None:
    goal = _goal()
    base_proposal = DeterministicPlanner(clock=lambda: NOW).propose(
        goal,
        (NON_EMPTY_ARTIFACT_CRITERION,),
    )
    confirmed = base_proposal.contract.confirm(confirmed_at=NOW)
    base = base_proposal.plan_revision.approve(confirmed, approved_at=NOW)
    exploration = DeterministicExplorationPlanner(clock=lambda: NOW).propose(
        ExplorationPlanRequest(
            goal=goal,
            criteria=(NON_EMPTY_ARTIFACT_CRITERION,),
            budget=ExplorationBudget(max_attempts=5),
        )
    )
    original_nodes = base.nodes
    patch = GraphPatch(
        base_plan_revision_id=base.plan_revision_id,
        target_completion_contract_id=confirmed.completion_contract_id,
        nodes=exploration.plan_revision.nodes,
        edges=exploration.plan_revision.edges,
        branches=exploration.plan_revision.branches,
        budget=ExplorationBudget(max_attempts=5),
        usage=ExplorationUsage(width=2, depth=1, attempts=5),
    )

    revised = patch.apply(base, confirmed, plan_revision_id=new_id(), created_at=NOW)

    assert revised.status is PlanRevisionStatus.DRAFT
    assert revised.version == base.version + 1
    assert revised.supersedes_plan_revision_id == base.plan_revision_id
    assert revised.completion_contract_id == confirmed.completion_contract_id
    assert base.status is PlanRevisionStatus.APPROVED
    assert base.nodes == original_nodes
    assert revised.nodes is not base.nodes


def test_graph_patch_rejects_unapproved_base_and_budget_mismatch() -> None:
    goal = _goal()
    base_proposal = DeterministicPlanner(clock=lambda: NOW).propose(
        goal,
        (NON_EMPTY_ARTIFACT_CRITERION,),
    )
    exploration = _exploration_proposal()
    with pytest.raises(ValueError, match="usage"):
        GraphPatch(
            base_plan_revision_id=base_proposal.plan_revision.plan_revision_id,
            target_completion_contract_id=base_proposal.contract.completion_contract_id,
            nodes=exploration.plan_revision.nodes,
            edges=exploration.plan_revision.edges,
            branches=exploration.plan_revision.branches,
            budget=ExplorationBudget(max_attempts=5),
            usage=ExplorationUsage(width=1, depth=1, attempts=5),
        )

    patch = GraphPatch(
        base_plan_revision_id=base_proposal.plan_revision.plan_revision_id,
        target_completion_contract_id=base_proposal.contract.completion_contract_id,
        nodes=exploration.plan_revision.nodes,
        edges=exploration.plan_revision.edges,
        branches=exploration.plan_revision.branches,
        budget=ExplorationBudget(max_attempts=5),
        usage=ExplorationUsage(width=2, depth=1, attempts=5),
    )
    with pytest.raises(ValueError, match="must be approved"):
        patch.apply(base_proposal.plan_revision, base_proposal.contract)


def test_deterministic_planners_replan_with_fresh_contract_and_check_lineage() -> None:
    goal = _goal()
    base_proposal = DeterministicPlanner(clock=lambda: NOW).propose(
        goal,
        (NON_EMPTY_ARTIFACT_CRITERION,),
    )
    confirmed = base_proposal.contract.confirm(confirmed_at=NOW)
    aligned = goal.use_completion_contract(confirmed)
    base = base_proposal.plan_revision.approve(confirmed, approved_at=NOW)

    single = DeterministicPlanner(clock=lambda: NOW).replan(
        aligned,
        base,
        (NON_EMPTY_ARTIFACT_CRITERION,),
    )
    exploration = DeterministicExplorationPlanner(clock=lambda: NOW).replan(
        ExplorationPlanRequest(
            goal=aligned,
            criteria=(NON_EMPTY_ARTIFACT_CRITERION,),
            budget=ExplorationBudget(max_attempts=5),
        ),
        base,
    )

    for proposal in (single, exploration):
        assert proposal.contract.version == 2
        assert (
            proposal.contract.supersedes_completion_contract_id == confirmed.completion_contract_id
        )
        assert proposal.contract.completion_contract_id != confirmed.completion_contract_id
        assert proposal.check_specs[0].check_id not in confirmed.required_check_ids
        assert proposal.plan_revision.version == 2
        assert proposal.plan_revision.supersedes_plan_revision_id == base.plan_revision_id
        assert (
            proposal.plan_revision.completion_contract_id
            == proposal.contract.completion_contract_id
        )
        assert proposal.plan_revision.completion_contract_version == proposal.contract.version
