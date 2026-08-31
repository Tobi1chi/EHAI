from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from ehai import ID, new_id
from ehai.domain.checking import GateDecision
from ehai.domain.goal import CompletionContract, Goal, GoalInvariantError, GoalStatus, Project

NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _contract(goal_id: ID) -> CompletionContract:
    return CompletionContract.draft(
        goal_id=goal_id,
        criteria=("all required evidence is accepted",),
        required_check_ids=(new_id(),),
        created_at=NOW,
    )


def _decision(contract: CompletionContract, *, passed: bool = True) -> GateDecision:
    return GateDecision(
        gate_id=new_id(),
        run_id=new_id(),
        plan_node_id=new_id(),
        attempt_id=new_id(),
        passed=passed,
        evaluated_at=NOW + timedelta(minutes=1),
        required_check_ids=contract.required_check_ids,
        failed_check_ids=() if passed else contract.required_check_ids,
        evidence_artifact_ids=(new_id(),),
        reason=None if passed else "required check failed",
    )


def test_project_and_goal_creation_normalize_user_text() -> None:
    project = Project.create("  research  ", created_at=NOW)
    goal = Goal.create(project.project_id, "  compare approaches  ", created_at=NOW)

    assert project.name == "research"
    assert goal.objective == "compare approaches"
    assert goal.status is GoalStatus.OPEN
    assert goal.completion_contract is None


def test_completion_contract_is_confirmed_without_mutating_draft() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    draft = _contract(goal.goal_id)

    confirmed = draft.confirm(confirmed_at=NOW + timedelta(seconds=1))

    assert draft.confirmed_at is None
    assert confirmed.is_confirmed
    with pytest.raises(FrozenInstanceError):
        confirmed.criteria = ("weaker criterion",)  # type: ignore[misc]


def test_contract_revision_has_new_identity_and_explicit_lineage() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    first = _contract(goal.goal_id).confirm(confirmed_at=NOW)

    second = first.revise(
        ("new criterion",),
        (new_id(),),
        created_at=NOW + timedelta(minutes=1),
    )

    assert second.version == 2
    assert second.completion_contract_id != first.completion_contract_id
    assert second.supersedes_completion_contract_id == first.completion_contract_id
    assert not second.is_confirmed
    assert first.is_confirmed


def test_attached_draft_can_be_replaced_only_by_its_confirmed_snapshot() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    draft = _contract(goal.goal_id)
    attached = goal.use_completion_contract(draft)

    confirmed = attached.use_completion_contract(
        draft.confirm(confirmed_at=NOW + timedelta(seconds=1))
    )

    assert confirmed.completion_contract is not None
    assert confirmed.completion_contract.is_confirmed

    changed_same_version = CompletionContract(
        completion_contract_id=draft.completion_contract_id,
        goal_id=draft.goal_id,
        version=draft.version,
        criteria=("silently weakened",),
        required_check_ids=draft.required_check_ids,
        created_at=draft.created_at,
        confirmed_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(GoalInvariantError, match="without a new version"):
        attached.use_completion_contract(changed_same_version)


def test_goal_rejects_skipped_or_unrelated_contract_versions() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    first = _contract(goal.goal_id)
    attached = goal.use_completion_contract(first)
    skipped = CompletionContract(
        completion_contract_id=new_id(),
        goal_id=goal.goal_id,
        version=3,
        criteria=("criterion",),
        required_check_ids=(new_id(),),
        created_at=NOW,
        supersedes_completion_contract_id=first.completion_contract_id,
    )

    with pytest.raises(GoalInvariantError, match=str(goal.goal_id)):
        attached.use_completion_contract(skipped)

    unrelated = _contract(new_id())
    with pytest.raises(GoalInvariantError, match=str(goal.goal_id)):
        goal.use_completion_contract(unrelated)


def test_goal_can_only_be_satisfied_by_matching_evidenced_final_gate() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    contract = _contract(goal.goal_id).confirm(confirmed_at=NOW)
    aligned = goal.use_completion_contract(contract)

    satisfied = aligned.satisfy(_decision(contract))

    assert aligned.status is GoalStatus.OPEN
    assert satisfied.status is GoalStatus.SATISFIED
    assert satisfied.final_gate_id is not None
    assert satisfied.satisfied_at == NOW + timedelta(minutes=1)


def test_satisfied_goal_requires_transition_or_rehydration_boundary() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    contract = _contract(goal.goal_id).confirm(confirmed_at=NOW)
    gate_id = new_id()

    with pytest.raises(GoalInvariantError, match=r"satisfy\(\) or rehydrate\(\)"):
        Goal(
            goal_id=goal.goal_id,
            project_id=goal.project_id,
            objective=goal.objective,
            created_at=goal.created_at,
            completion_contract=contract,
            status=GoalStatus.SATISFIED,
            final_gate_id=gate_id,
            satisfied_at=NOW + timedelta(minutes=1),
        )

    restored = Goal.rehydrate(
        goal_id=goal.goal_id,
        project_id=goal.project_id,
        objective=goal.objective,
        created_at=goal.created_at,
        completion_contract=contract,
        status=GoalStatus.SATISFIED,
        final_gate_id=gate_id,
        satisfied_at=NOW + timedelta(minutes=1),
    )
    assert restored.status is GoalStatus.SATISFIED


def test_completion_contract_snapshots_mutable_container_inputs() -> None:
    goal_id = new_id()
    criteria = ["criterion"]
    checks = [new_id()]
    contract = CompletionContract(
        completion_contract_id=new_id(),
        goal_id=goal_id,
        version=1,
        criteria=criteria,  # type: ignore[arg-type]
        required_check_ids=checks,  # type: ignore[arg-type]
        created_at=NOW,
    )

    criteria.append("late mutation")
    checks.append(new_id())

    assert contract.criteria == ("criterion",)
    assert len(contract.required_check_ids) == 1


def test_goal_rejects_failed_gate_and_unconfirmed_contract() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    draft = _contract(goal.goal_id)

    with pytest.raises(GoalInvariantError, match=str(goal.goal_id)):
        goal.use_completion_contract(draft).satisfy(_decision(draft))

    confirmed = draft.confirm(confirmed_at=NOW)
    with pytest.raises(GoalInvariantError, match=str(goal.goal_id)):
        goal.use_completion_contract(confirmed).satisfy(_decision(confirmed, passed=False))


def test_goal_rejects_gate_without_matching_checks() -> None:
    goal = Goal.create(new_id(), "produce evidence", created_at=NOW)
    contract = _contract(goal.goal_id).confirm(confirmed_at=NOW)
    aligned = goal.use_completion_contract(contract)
    wrong_checks = GateDecision(
        gate_id=new_id(),
        run_id=new_id(),
        plan_node_id=new_id(),
        attempt_id=new_id(),
        passed=True,
        evaluated_at=NOW,
        required_check_ids=(new_id(),),
        failed_check_ids=(),
        evidence_artifact_ids=(new_id(),),
    )
    with pytest.raises(GoalInvariantError, match=str(goal.goal_id)):
        aligned.satisfy(wrong_checks)
