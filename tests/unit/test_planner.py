from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ehai import ID, new_id
from ehai.application.planner import DeterministicPlanner, Planner, PlanProposal
from ehai.domain import (
    CheckKind,
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

    proposal = planner.propose(goal, ("artifact exists",))

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

    proposal = planner.propose(_goal(), ("first", "second"))

    check_id, contract_id, node_id, revision_id = ids
    assert proposal.check_specs[0].check_id == check_id
    assert proposal.contract.completion_contract_id == contract_id
    assert proposal.plan_revision.nodes[0].plan_node_id == node_id
    assert proposal.plan_revision.plan_revision_id == revision_id
    assert proposal.contract.created_at == NOW
    assert proposal.plan_revision.created_at == NOW


def test_plan_proposal_snapshots_specs_and_validates_required_checks() -> None:
    proposal = DeterministicPlanner(clock=lambda: NOW).propose(_goal(), ("criterion",))
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


def test_planner_rejects_empty_criteria_and_goal_with_existing_contract() -> None:
    goal = _goal()
    planner = DeterministicPlanner(clock=lambda: NOW)
    with pytest.raises(ValueError, match="completion criteria"):
        planner.propose(goal, ())

    proposal = planner.propose(goal, ("criterion",))
    aligned = goal.use_completion_contract(proposal.contract)
    with pytest.raises(ValueError, match="already has"):
        planner.propose(aligned, ("criterion",))
