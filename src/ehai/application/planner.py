"""Planner Port and deterministic single-node I3 implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ehai import ID, new_id, utc_now
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.goal import CompletionContract, Goal, GoalStatus
from ehai.domain.planning import PlanNode, PlanNodeKind, PlanRevision, PlanRevisionStatus

NON_EMPTY_ARTIFACT_CRITERION = "artifact:non-empty"


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A Planner's unapproved graph, contract, and referenced Check definitions."""

    contract: CompletionContract
    plan_revision: PlanRevision
    check_specs: tuple[CheckSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "check_specs", tuple(self.check_specs))
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


@runtime_checkable
class Planner(Protocol):
    """Application Port for proposing, but never approving or executing, a plan."""

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        """Return an unapproved proposal for an open Goal."""
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
