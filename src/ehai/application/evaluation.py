"""Pure, evidence-based branch selection for the P1 exploration loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ehai import ID, normalize_id
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)

FIRST_VIABLE_BRANCH_CRITERION = (
    "first active branch in PlanRevision order with a completed node and candidate evidence"
)


class NoViableBranchError(RuntimeError):
    """Raised when no active exploration Branch satisfies the fixed P1 criterion."""


@dataclass(frozen=True, slots=True)
class BranchEvaluationContext:
    """A fully scoped immutable execution snapshot for branch evaluation."""

    plan: PlanRevision
    run: Run
    attempts: tuple[Attempt, ...]
    artifacts: tuple[Artifact, ...]
    adoptions: tuple[ResultAdoption, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plan, PlanRevision):
            raise TypeError("BranchEvaluationContext plan must be a PlanRevision")
        if not isinstance(self.run, Run):
            raise TypeError("BranchEvaluationContext run must be a Run")
        attempts = tuple(self.attempts)
        artifacts = tuple(self.artifacts)
        adoptions = tuple(self.adoptions)
        if not all(isinstance(attempt, Attempt) for attempt in attempts):
            raise TypeError("BranchEvaluationContext attempts must contain Attempts")
        if not all(isinstance(artifact, Artifact) for artifact in artifacts):
            raise TypeError("BranchEvaluationContext artifacts must contain Artifacts")
        if not all(isinstance(adoption, ResultAdoption) for adoption in adoptions):
            raise TypeError("BranchEvaluationContext adoptions must contain ResultAdoption values")
        if len({adoption.adoption_id for adoption in adoptions}) != len(adoptions):
            raise ValueError("BranchEvaluationContext contains duplicate ResultAdoption IDs")
        if self.plan.status is not PlanRevisionStatus.APPROVED:
            raise ValueError("BranchEvaluationContext requires an approved PlanRevision")
        if not self.plan.branches:
            raise ValueError("BranchEvaluationContext PlanRevision has no exploration Branches")
        if self.run.status is not RunStatus.RUNNING:
            raise ValueError("BranchEvaluationContext requires a running Run")
        if (
            self.run.plan_revision_id != self.plan.plan_revision_id
            or self.run.goal_id != self.plan.goal_id
        ):
            raise ValueError("BranchEvaluationContext Run belongs to another PlanRevision")

        branch_node_ids = {node_id for branch in self.plan.branches for node_id in branch.node_ids}
        node_by_id = {node.plan_node_id: node for node in self.plan.nodes}
        adoption_source_attempt_ids = {adoption.source_attempt_id for adoption in adoptions}
        adoption_artifact_ids = {
            evidence.artifact_id for adoption in adoptions for evidence in adoption.evidence
        }
        if len({attempt.attempt_id for attempt in attempts}) != len(attempts):
            raise ValueError("BranchEvaluationContext contains duplicate Attempt IDs")
        attempt_by_id = {attempt.attempt_id: attempt for attempt in attempts}
        for attempt in attempts:
            if attempt.run_id != self.run.run_id and attempt.attempt_id not in (
                adoption_source_attempt_ids
            ):
                raise ValueError(f"Attempt {attempt.attempt_id} belongs to another Run")
            if attempt.run_id == self.run.run_id and attempt.plan_node_id not in branch_node_ids:
                raise ValueError(
                    f"Attempt {attempt.attempt_id} is outside the PlanRevision Branches"
                )

        if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise ValueError("BranchEvaluationContext contains duplicate Artifact IDs")
        artifact_ids = {artifact.artifact_id for artifact in artifacts}
        for artifact in artifacts:
            if artifact.run_id != self.run.run_id and artifact.artifact_id not in (
                adoption_artifact_ids
            ):
                raise ValueError(f"Artifact {artifact.artifact_id} belongs to another Run")
            if artifact.run_id == self.run.run_id and artifact.plan_node_id not in branch_node_ids:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} is outside the PlanRevision Branches"
                )
            if artifact.attempt_id is None or artifact.attempt_id not in attempt_by_id:
                raise ValueError(f"Artifact {artifact.artifact_id} references an unknown Attempt")
            attempt = attempt_by_id[artifact.attempt_id]
            if attempt.plan_node_id != artifact.plan_node_id:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} belongs to another Attempt scope"
                )
            if artifact.artifact_id not in attempt.artifact_ids:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} is absent from its Attempt snapshot"
                )
            if attempt.run_id != artifact.run_id:
                raise ValueError(
                    f"Artifact {artifact.artifact_id} belongs to another Attempt scope"
                )
        missing_artifacts = {
            artifact_id
            for attempt in attempts
            for artifact_id in attempt.artifact_ids
            if artifact_id not in artifact_ids
        }
        if missing_artifacts:
            raise ValueError(
                "BranchEvaluationContext omits Attempt Artifacts: "
                + ", ".join(sorted(missing_artifacts))
            )

        adoption_by_artifact: dict[ID, ResultAdoption] = {}
        adoption_target_nodes: set[ID] = set()
        adoption_source_attempts: set[ID] = set()
        for adoption in adoptions:
            if (
                adoption.target_run_id != self.run.run_id
                or adoption.target_plan_revision_id != self.plan.plan_revision_id
                or self.run.predecessor_run_id != adoption.source_run_id
            ):
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} does not belong to this target Run"
                )
            target_node = node_by_id.get(adoption.target_plan_node_id)
            if target_node is None or adoption.target_plan_node_id not in branch_node_ids:
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} targets a node outside the Branches"
                )
            if target_node.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} targets a non-implementation node"
                )
            if target_node.status is not PlanNodeStatus.COMPLETED:
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} targets an incomplete node"
                )
            if adoption.target_plan_node_id in adoption_target_nodes:
                raise ValueError(
                    f"BranchEvaluationContext has multiple adoptions for target node "
                    f"{adoption.target_plan_node_id}"
                )
            adoption_target_nodes.add(adoption.target_plan_node_id)

            source_attempt = attempt_by_id.get(adoption.source_attempt_id)
            if source_attempt is None:
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} references an unknown source Attempt"
                )
            if (
                source_attempt.run_id != adoption.source_run_id
                or source_attempt.plan_node_id != adoption.source_plan_node_id
                or source_attempt.attempt_id != adoption.source_attempt_id
                or source_attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} has no matching successful "
                    "source Attempt"
                )
            if adoption.source_attempt_id in adoption_source_attempts:
                raise ValueError(
                    f"BranchEvaluationContext has multiple adoptions for source Attempt "
                    f"{adoption.source_attempt_id}"
                )
            adoption_source_attempts.add(adoption.source_attempt_id)

            evidence_by_id = {item.artifact_id: item.sha256 for item in adoption.evidence}
            evidence_ids = tuple(evidence_by_id)
            if len(evidence_ids) != len(source_attempt.artifact_ids) or set(evidence_ids) != set(
                source_attempt.artifact_ids
            ):
                raise ValueError(
                    f"ResultAdoption {adoption.adoption_id} evidence does not match "
                    "its source Attempt"
                )
            for evidence in adoption.evidence:
                if evidence.artifact_id in adoption_by_artifact:
                    raise ValueError(
                        f"Artifact {evidence.artifact_id} is adopted by multiple target nodes"
                    )
                retained_artifact = next(
                    (item for item in artifacts if item.artifact_id == evidence.artifact_id),
                    None,
                )
                if retained_artifact is None:
                    raise ValueError(
                        f"ResultAdoption {adoption.adoption_id} references an unknown Artifact"
                    )
                if (
                    retained_artifact.run_id != adoption.source_run_id
                    or retained_artifact.plan_node_id != adoption.source_plan_node_id
                    or retained_artifact.attempt_id != adoption.source_attempt_id
                    or retained_artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                    or retained_artifact.sha256 != evidence.sha256
                ):
                    raise ValueError(
                        f"ResultAdoption {adoption.adoption_id} evidence is inconsistent"
                    )
                adoption_by_artifact[evidence.artifact_id] = adoption

        for attempt in attempts:
            if attempt.run_id == self.run.run_id and attempt.plan_node_id in adoption_target_nodes:
                raise ValueError(
                    f"ResultAdoption target node {attempt.plan_node_id} has an existing Attempt"
                )
            if (
                attempt.run_id != self.run.run_id
                and attempt.attempt_id not in adoption_source_attempts
            ):
                raise ValueError(
                    f"Attempt {attempt.attempt_id} is a foreign unadopted source Attempt"
                )
        for artifact in artifacts:
            if (
                artifact.run_id != self.run.run_id
                and artifact.artifact_id not in adoption_by_artifact
            ):
                raise ValueError(
                    f"Artifact {artifact.artifact_id} is a foreign unadopted source Artifact"
                )
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "adoptions", adoptions)

    def effective_plan_node_id(self, artifact: Artifact) -> ID:
        """Return the target node identity used to place an Artifact in a Branch."""
        if not isinstance(artifact, Artifact):
            raise TypeError("artifact must be an Artifact")
        if artifact.plan_node_id is None:
            raise ValueError(f"Artifact {artifact.artifact_id} has no PlanNode identity")
        for adoption in self.adoptions:
            if any(item.artifact_id == artifact.artifact_id for item in adoption.evidence):
                return adoption.target_plan_node_id
        return artifact.plan_node_id


@dataclass(frozen=True, slots=True)
class BranchSelection:
    """An immutable, evidenced selection proposal without state mutation authority."""

    selected_branch_id: ID
    pruned_branch_ids: tuple[ID, ...]
    criterion: str
    evidence_artifact_ids: tuple[ID, ...]
    explanation: str
    selected_artifact_ids: tuple[ID, ...] = ()

    def __post_init__(self) -> None:
        selected_id = normalize_id(self.selected_branch_id)
        pruned_ids = tuple(normalize_id(branch_id) for branch_id in self.pruned_branch_ids)
        evidence_ids = tuple(
            normalize_id(artifact_id) for artifact_id in self.evidence_artifact_ids
        )
        selected_artifact_ids = tuple(
            normalize_id(artifact_id)
            for artifact_id in (self.selected_artifact_ids or self.evidence_artifact_ids)
        )
        if len(set(pruned_ids)) != len(pruned_ids):
            raise ValueError("BranchSelection pruned_branch_ids must not contain duplicates")
        if selected_id in pruned_ids:
            raise ValueError("BranchSelection cannot prune its selected Branch")
        if not evidence_ids:
            raise ValueError("BranchSelection requires evidence Artifacts")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("BranchSelection evidence_artifact_ids must not contain duplicates")
        if not selected_artifact_ids:
            raise ValueError("BranchSelection requires selected Artifacts")
        if len(set(selected_artifact_ids)) != len(selected_artifact_ids):
            raise ValueError("BranchSelection selected_artifact_ids must not contain duplicates")
        if not set(selected_artifact_ids).issubset(evidence_ids):
            raise ValueError("BranchSelection selected Artifacts must be part of the evidence")
        if not isinstance(self.criterion, str) or not self.criterion.strip():
            raise ValueError("BranchSelection criterion must not be blank")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("BranchSelection explanation must not be blank")
        object.__setattr__(self, "selected_branch_id", selected_id)
        object.__setattr__(self, "pruned_branch_ids", pruned_ids)
        object.__setattr__(self, "evidence_artifact_ids", evidence_ids)
        object.__setattr__(self, "selected_artifact_ids", selected_artifact_ids)
        object.__setattr__(self, "criterion", self.criterion.strip())
        object.__setattr__(self, "explanation", self.explanation.strip())

    @property
    def compared_artifact_ids(self) -> tuple[ID, ...]:
        """Return the Artifact IDs actually compared by the Evaluator."""
        return self.evidence_artifact_ids


class BranchSelectionProtocolError(ValueError):
    """Raised when an Evaluator Artifact does not contain a valid selection proposal."""


def parse_branch_selection(document: str, context: BranchEvaluationContext) -> BranchSelection:
    """Parse and validate the strict Evaluator Artifact selection proposal."""
    if not isinstance(document, str):
        raise BranchSelectionProtocolError("BranchSelection proposal must be JSON text")
    if not isinstance(context, BranchEvaluationContext):
        raise TypeError("context must be a BranchEvaluationContext")
    decoded = _strict_json_decode(document)
    if not isinstance(decoded, dict):
        raise BranchSelectionProtocolError("BranchSelection proposal must be a JSON object")
    expected_keys = frozenset(
        {
            "selected_branch_id",
            "pruned_branch_ids",
            "criterion",
            "explanation",
            "compared_artifact_ids",
            "selected_artifact_ids",
        }
    )
    _require_exact_keys(decoded, expected_keys, "BranchSelection proposal")

    selection = BranchSelection(
        selected_branch_id=_required_id(decoded["selected_branch_id"], "selected_branch_id"),
        pruned_branch_ids=_required_id_list(decoded["pruned_branch_ids"], "pruned_branch_ids"),
        criterion=_required_text(decoded["criterion"], "criterion"),
        evidence_artifact_ids=_required_id_list(
            decoded["compared_artifact_ids"],
            "compared_artifact_ids",
        ),
        explanation=_required_text(decoded["explanation"], "explanation"),
        selected_artifact_ids=_required_id_list(
            decoded["selected_artifact_ids"],
            "selected_artifact_ids",
        ),
    )
    return validate_branch_selection(selection, context)


def validate_branch_selection(
    selection: BranchSelection,
    context: BranchEvaluationContext,
) -> BranchSelection:
    """Validate one parsed or injected selection against the same execution snapshot."""
    if not isinstance(selection, BranchSelection):
        raise BranchSelectionProtocolError("BranchEvaluator returned an invalid selection")
    if not isinstance(context, BranchEvaluationContext):
        raise TypeError("context must be a BranchEvaluationContext")

    branch_by_id = {branch.branch_id: branch for branch in context.plan.branches}
    node_by_id = {node.plan_node_id: node for node in context.plan.nodes}
    attempt_by_id = {attempt.attempt_id: attempt for attempt in context.attempts}
    selected = branch_by_id.get(selection.selected_branch_id)
    if selected is None or selected.status is not BranchStatus.ACTIVE:
        raise BranchSelectionProtocolError("selected_branch_id does not name an active Branch")

    active_siblings = tuple(
        branch
        for branch in context.plan.branches
        if branch.branch_id != selected.branch_id
        and branch.status is BranchStatus.ACTIVE
        and branch.fork_node_id == selected.fork_node_id
        and branch.merge_node_id == selected.merge_node_id
    )
    if set(selection.pruned_branch_ids) != {branch.branch_id for branch in active_siblings}:
        raise BranchSelectionProtocolError("pruned_branch_ids do not match active siblings")

    active_sibling_group = (selected, *active_siblings)
    candidate_ids_by_branch: dict[ID, set[ID]] = {}
    viable_branch_ids: set[ID] = set()
    for branch in active_sibling_group:
        ids: set[ID] = set()
        for artifact in context.artifacts:
            if context.effective_plan_node_id(artifact) not in branch.node_ids:
                continue
            if artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}:
                continue
            if artifact.attempt_id is None:
                continue
            attempt = attempt_by_id.get(artifact.attempt_id)
            if attempt is not None and attempt.status is AttemptStatus.SUCCEEDED:
                ids.add(artifact.artifact_id)
        candidate_ids_by_branch[branch.branch_id] = ids
        if all(
            node_by_id[node_id].status is PlanNodeStatus.COMPLETED for node_id in branch.node_ids
        ):
            viable_branch_ids.add(branch.branch_id)

    if not viable_branch_ids:
        raise BranchSelectionProtocolError("no viable active sibling Branch was available")
    if selected.branch_id not in viable_branch_ids:
        raise BranchSelectionProtocolError("selected_branch_id names a non-viable Branch")

    required_compared_ids = set().union(*candidate_ids_by_branch.values())
    if not required_compared_ids:
        raise BranchSelectionProtocolError(
            "no active sibling Branch candidate Artifacts were available"
        )
    if set(selection.compared_artifact_ids) != required_compared_ids:
        raise BranchSelectionProtocolError(
            "compared_artifact_ids evidence must include every active Branch candidate Artifact "
            "in the selected sibling group; "
            f"missing={sorted(required_compared_ids - set(selection.compared_artifact_ids))}; "
            f"unexpected={sorted(set(selection.compared_artifact_ids) - required_compared_ids)}"
        )
    for branch_id in viable_branch_ids:
        artifact_ids = candidate_ids_by_branch[branch_id]
        if not artifact_ids:
            raise BranchSelectionProtocolError(f"Branch {branch_id} has no candidate evidence")
        if not artifact_ids.intersection(selection.compared_artifact_ids):
            raise BranchSelectionProtocolError(f"Branch {branch_id} was not compared")

    selected_candidate_ids = candidate_ids_by_branch[selected.branch_id]
    if not set(selection.selected_artifact_ids).issubset(selected_candidate_ids):
        raise BranchSelectionProtocolError("selected_artifact_ids are outside the selected Branch")
    return selection


def _strict_json_decode(document: str) -> object:
    def reject_constant(value: str) -> None:
        raise BranchSelectionProtocolError(f"non-standard JSON constant is not allowed: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BranchSelectionProtocolError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            document,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except BranchSelectionProtocolError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise BranchSelectionProtocolError(f"invalid BranchSelection JSON: {error}") from error


def _require_exact_keys(value: dict[str, object], expected: frozenset[str], owner: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BranchSelectionProtocolError(
            f"{owner} keys are invalid; missing={missing}, extra={extra}"
        )


def _required_id(value: object, field_name: str) -> ID:
    if not isinstance(value, str):
        raise BranchSelectionProtocolError(f"{field_name} must be an ID string")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise BranchSelectionProtocolError(f"{field_name} must be a valid ID") from error


def _required_id_list(value: object, field_name: str) -> tuple[ID, ...]:
    if not isinstance(value, list) or not value:
        raise BranchSelectionProtocolError(f"{field_name} must be a non-empty array")
    ids = tuple(_required_id(item, field_name) for item in value)
    if len(set(ids)) != len(ids):
        raise BranchSelectionProtocolError(f"{field_name} must not contain duplicates")
    return ids


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BranchSelectionProtocolError(f"{field_name} must be non-empty text")
    return value.strip()


@runtime_checkable
class BranchEvaluator(Protocol):
    """Select an exploration Branch from a read-only execution snapshot."""

    def evaluate(self, context: BranchEvaluationContext) -> BranchSelection:
        """Return an evidenced selection without mutating Plan or execution state."""
        ...


class DeterministicBranchEvaluator:
    """Select the first viable active Branch in persisted PlanRevision order."""

    def evaluate(self, context: BranchEvaluationContext) -> BranchSelection:
        if not isinstance(context, BranchEvaluationContext):
            raise TypeError("context must be a BranchEvaluationContext")
        node_by_id = {node.plan_node_id: node for node in context.plan.nodes}
        attempt_by_id = {attempt.attempt_id: attempt for attempt in context.attempts}
        for branch in context.plan.branches:
            if branch.status is not BranchStatus.ACTIVE:
                continue
            nodes = tuple(node_by_id[node_id] for node_id in branch.node_ids)
            if any(node.status is not PlanNodeStatus.COMPLETED for node in nodes):
                continue
            selected_artifact_ids = tuple(
                artifact.artifact_id
                for artifact in context.artifacts
                if context.effective_plan_node_id(artifact) in branch.node_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                and artifact.attempt_id is not None
                and attempt_by_id[artifact.attempt_id].status is AttemptStatus.SUCCEEDED
            )
            if not selected_artifact_ids:
                continue
            sibling_node_ids = {
                node_id
                for sibling in context.plan.branches
                if sibling.status is BranchStatus.ACTIVE
                and sibling.fork_node_id == branch.fork_node_id
                and sibling.merge_node_id == branch.merge_node_id
                for node_id in sibling.node_ids
            }
            compared_artifact_ids = tuple(
                artifact.artifact_id
                for artifact in context.artifacts
                if context.effective_plan_node_id(artifact) in sibling_node_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                and artifact.attempt_id is not None
                and attempt_by_id[artifact.attempt_id].status is AttemptStatus.SUCCEEDED
            )
            selection = BranchSelection(
                selected_branch_id=branch.branch_id,
                pruned_branch_ids=_active_sibling_ids(context.plan.branches, branch),
                criterion=FIRST_VIABLE_BRANCH_CRITERION,
                evidence_artifact_ids=compared_artifact_ids,
                explanation=(
                    f"Selected Branch {branch.branch_id} ({branch.label}) because it is the "
                    "first active Branch in PlanRevision order with a completed node and "
                    "candidate or patch evidence from this Run."
                ),
                selected_artifact_ids=selected_artifact_ids,
            )
            return validate_branch_selection(selection, context)
        raise NoViableBranchError(
            f"Run {context.run.run_id} has no active Branch with completed work and "
            "candidate or patch evidence"
        )


def _active_sibling_ids(branches: tuple[Branch, ...], selected: Branch) -> tuple[ID, ...]:
    return tuple(
        branch.branch_id
        for branch in branches
        if branch.branch_id != selected.branch_id
        and branch.status is BranchStatus.ACTIVE
        and branch.fork_node_id == selected.fork_node_id
        and branch.merge_node_id == selected.merge_node_id
    )
