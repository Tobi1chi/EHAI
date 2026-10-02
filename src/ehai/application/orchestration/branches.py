"""Evaluate exploration branches and record the selection."""

from __future__ import annotations

from hashlib import sha256

from ehai import ID, JsonValue
from ehai.application.evaluation import (
    BranchEvaluationContext,
    BranchSelection,
    BranchSelectionProtocolError,
    parse_branch_selection,
    validate_branch_selection,
)
from ehai.application.orchestration.adoption import AdoptionMixin
from ehai.application.orchestration.common import (
    BranchEvaluationError,
    OrchestrationError,
    _evaluator_branches,
    _rehydrate_plan,
    _required_attempt,
    _required_node,
    _required_plan,
    _required_run,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.orchestration.readiness import (
    _branch_containing,
)
from ehai.application.ports import ArtifactStore, UnitOfWork
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
)


class BranchEvaluationMixin:
    """Evaluate exploration branches and record the selection."""

    def _evaluate_completed_evaluator(self: OrchestratorHost, run_id: ID) -> bool:
        with self._uow_factory() as uow:
            run, plan, _, _ = self._load_run_context(uow, run_id)
            if run.status is not RunStatus.RUNNING:
                return False
            completed_evaluator = None
            evaluator_branches: tuple[Branch, ...] = ()
            for node in plan.nodes:
                if node.kind is not PlanNodeKind.EVALUATOR:
                    continue
                if node.status is not PlanNodeStatus.COMPLETED:
                    continue
                branches = _evaluator_branches(plan, node)
                if all(branch.status is BranchStatus.ACTIVE for branch in branches):
                    completed_evaluator = node
                    evaluator_branches = branches
                    break
            if completed_evaluator is None:
                return False
            context = _branch_evaluation_context(
                uow,
                plan,
                run,
                branches=evaluator_branches,
                artifact_store=self._artifact_store,
            )
            evaluator_attempts = tuple(
                attempt
                for attempt in sorted(
                    uow.states.list_attempts(run.run_id), key=lambda item: item.sequence
                )
                if attempt.plan_node_id == completed_evaluator.plan_node_id
                and attempt.status is AttemptStatus.SUCCEEDED
            )
            evaluator_attempt = evaluator_attempts[-1] if evaluator_attempts else None
            evaluator_artifact_ids = (
                set() if evaluator_attempt is None else set(evaluator_attempt.artifact_ids)
            )
            evaluator_artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(run.run_id)
                if artifact.artifact_id in evaluator_artifact_ids
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
        try:
            if self._branch_evaluator is None:
                if len(evaluator_artifacts) != 1:
                    raise BranchSelectionProtocolError(
                        "Evaluator must persist exactly one candidate BranchSelection Artifact"
                    )
                try:
                    document = self._artifact_store.read(evaluator_artifacts[0].artifact_id).decode(
                        "utf-8"
                    )
                except UnicodeDecodeError as error:
                    raise BranchSelectionProtocolError(
                        "Evaluator BranchSelection Artifact must be UTF-8 JSON"
                    ) from error
                selection = parse_branch_selection(document, context)
            else:
                selection = self._branch_evaluator.evaluate(context)
            self._record_branch_selection(context, selection)
        except Exception as error:
            self._record_evaluation_failure(run.run_id, error)
            raise BranchEvaluationError(
                f"run {run.run_id} branch evaluation failed: {type(error).__name__}: {error}"
            ) from error
        return True

    def validate_evaluator_candidate(self: OrchestratorHost, attempt_id: ID, document: str) -> None:
        """Validate selection before a Worker finishes, so it can repair its proposal."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            plan = _required_plan(uow, run.run_id)
            evaluator = _required_node(plan, attempt.plan_node_id)
            if evaluator.kind is not PlanNodeKind.EVALUATOR:
                raise OrchestrationError("Only evaluator nodes submit branch selections")
            context = _branch_evaluation_context(
                uow,
                plan,
                run,
                branches=_evaluator_branches(plan, evaluator),
                artifact_store=self._artifact_store,
            )
        parse_branch_selection(document, context)

    def _record_branch_selection(
        self: OrchestratorHost,
        context: BranchEvaluationContext,
        selection: BranchSelection,
    ) -> None:
        selection = validate_branch_selection(selection, context)
        branch_by_id = {branch.branch_id: branch for branch in context.plan.branches}
        selected = branch_by_id.get(selection.selected_branch_id)
        if selected is None or selected.status is not BranchStatus.ACTIVE:
            raise OrchestrationError(
                f"BranchEvaluator selected unavailable Branch {selection.selected_branch_id}"
            )
        node_by_id = {node.plan_node_id: node for node in context.plan.nodes}
        if any(
            node_by_id[node_id].status is not PlanNodeStatus.COMPLETED
            for node_id in selected.node_ids
        ):
            raise OrchestrationError(
                f"BranchEvaluator selected non-viable Branch {selected.branch_id}"
            )
        active_sibling_ids = {
            branch.branch_id
            for branch in context.plan.branches
            if branch.branch_id != selected.branch_id
            and branch.status is BranchStatus.ACTIVE
            and branch.fork_node_id == selected.fork_node_id
            and branch.merge_node_id == selected.merge_node_id
        }
        if set(selection.pruned_branch_ids) != active_sibling_ids:
            raise OrchestrationError(
                "BranchEvaluator pruned Branches do not match the active sibling set"
            )
        artifact_by_id = {artifact.artifact_id: artifact for artifact in context.artifacts}
        attempt_by_id = {attempt.attempt_id: attempt for attempt in context.attempts}
        active_sibling_group = {selected.branch_id, *active_sibling_ids}
        viable_sibling_group = {
            branch_id
            for branch_id in active_sibling_group
            if all(
                node_by_id[node_id].status is PlanNodeStatus.COMPLETED
                for node_id in branch_by_id[branch_id].node_ids
            )
        }
        compared_branch_ids: set[ID] = set()
        for artifact_id in selection.evidence_artifact_ids:
            artifact = artifact_by_id.get(artifact_id)
            artifact_plan_node_id = (
                None if artifact is None else context.effective_plan_node_id(artifact)
            )
            attempt = (
                None
                if artifact is None or artifact.attempt_id is None
                else attempt_by_id.get(artifact.attempt_id)
            )
            artifact_branch = (
                None
                if artifact_plan_node_id is None
                else _branch_containing(context.plan, artifact_plan_node_id)
            )
            if (
                artifact is None
                or artifact_branch is None
                or artifact_branch.branch_id not in active_sibling_group
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or attempt is None
                or attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise OrchestrationError(
                    "BranchEvaluator selection evidence is outside active Branch candidates"
                )
            compared_branch_ids.add(artifact_branch.branch_id)
        if not viable_sibling_group.issubset(compared_branch_ids):
            raise OrchestrationError("BranchEvaluator did not compare every viable Branch")
        for artifact_id in selection.selected_artifact_ids:
            artifact = artifact_by_id.get(artifact_id)
            attempt = (
                None
                if artifact is None or artifact.attempt_id is None
                else attempt_by_id.get(artifact.attempt_id)
            )
            if (
                artifact is None
                or context.effective_plan_node_id(artifact) not in selected.node_ids
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or attempt is None
                or attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise OrchestrationError(
                    "BranchEvaluator selected Artifacts do not belong to the selected Branch"
                )

        with self._uow_factory() as uow:
            run = _required_run(uow, context.run.run_id)
            plan = _required_plan(uow, run.run_id)
            if run != context.run or plan != context.plan:
                raise OrchestrationError(
                    f"run {run.run_id} changed while BranchEvaluator was deciding"
                )
            current_context = _branch_evaluation_context(
                uow,
                plan,
                run,
                branches=tuple(
                    branch
                    for branch in plan.branches
                    if (branch.fork_node_id, branch.merge_node_id)
                    == (selected.fork_node_id, selected.merge_node_id)
                ),
                artifact_store=self._artifact_store,
            )
            if current_context != context:
                raise OrchestrationError("Branch candidate evidence changed during evaluation")
            replacements = {
                selected.branch_id: selected.select(),
                **{
                    branch_id: branch_by_id[branch_id].prune()
                    for branch_id in selection.pruned_branch_ids
                },
            }
            pruned_nodes: list[PlanNode] = []
            nodes = list(plan.nodes)
            for branch_id in selection.pruned_branch_ids:
                branch = branch_by_id[branch_id]
                for index, node in enumerate(nodes):
                    if node.plan_node_id in branch.node_ids and node.status in {
                        PlanNodeStatus.PENDING,
                        PlanNodeStatus.READY,
                    }:
                        pruned = node.prune()
                        nodes[index] = pruned
                        pruned_nodes.append(pruned)
            plan = _rehydrate_plan(
                plan,
                nodes=tuple(nodes),
                branches=tuple(
                    replacements.get(branch.branch_id, branch) for branch in plan.branches
                ),
            )
            uow.states.put_execution_plan(run.run_id, plan)
            evidence: list[JsonValue] = list(selection.evidence_artifact_ids)
            selected_artifact_ids: list[JsonValue] = list(selection.selected_artifact_ids)
            uow.events.append(
                self._event(
                    EventType.BRANCH_SELECTED,
                    run,
                    selected.branch_id,
                    {
                        "branch_id": selected.branch_id,
                        "fork_node_id": selected.fork_node_id,
                        "criterion": selection.criterion,
                        "evidence_artifact_ids": evidence,
                        "compared_artifact_ids": evidence,
                        "selected_artifact_ids": selected_artifact_ids,
                        "explanation": selection.explanation,
                    },
                )
            )
            for branch_id in selection.pruned_branch_ids:
                uow.events.append(
                    self._event(
                        EventType.BRANCH_PRUNED,
                        run,
                        branch_id,
                        {
                            "branch_id": branch_id,
                            "selected_branch_id": selected.branch_id,
                            "criterion": selection.criterion,
                            "evidence_artifact_ids": evidence,
                            "compared_artifact_ids": evidence,
                            "selected_artifact_ids": selected_artifact_ids,
                            "explanation": selection.explanation,
                        },
                    )
                )
            for node in pruned_nodes:
                uow.events.append(
                    self._event(
                        EventType.PLAN_NODE_PRUNED,
                        run,
                        node.plan_node_id,
                        {"plan_node_id": node.plan_node_id},
                    )
                )
            uow.commit()

    def _record_evaluation_failure(self: OrchestratorHost, run_id: ID, error: Exception) -> None:
        reason = f"Branch evaluation failed: {type(error).__name__}: {error}"
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            failed_run = run.fail(reason, at=self._clock())
            uow.states.put_run(failed_run)
            uow.events.append(
                self._event(
                    EventType.RUN_FAILED,
                    failed_run,
                    failed_run.run_id,
                    {"run_id": failed_run.run_id, "reason": reason},
                )
            )
            uow.commit()


def _branch_evaluation_context(
    uow: UnitOfWork,
    plan: PlanRevision,
    run: Run,
    *,
    branches: tuple[Branch, ...],
    artifact_store: ArtifactStore,
) -> BranchEvaluationContext:
    branch_nodes = {node_id for branch in branches for node_id in branch.node_ids}
    scoped_attempts = tuple(
        attempt
        for attempt in uow.states.list_attempts(run.run_id)
        if attempt.plan_node_id in branch_nodes
    )
    latest = {
        attempt.plan_node_id: attempt
        for attempt in sorted(scoped_attempts, key=lambda item: item.sequence)
        if attempt.status is AttemptStatus.SUCCEEDED
    }
    attempts = tuple(sorted(latest.values(), key=lambda item: item.sequence))
    artifact_ids = {artifact_id for attempt in attempts for artifact_id in attempt.artifact_ids}
    artifacts = tuple(
        artifact
        for artifact in uow.states.list_artifacts_for_run(run.run_id)
        if artifact.artifact_id in artifact_ids
    )
    adoptions: list[ResultAdoption] = []
    for adoption in uow.states.list_result_adoptions(run.run_id):
        if adoption.target_plan_node_id not in branch_nodes:
            continue
        node = _required_node(plan, adoption.target_plan_node_id)
        if node.status is not PlanNodeStatus.COMPLETED or node.plan_node_id in latest:
            continue
        producer = _required_attempt(uow, adoption.source_attempt_id)
        AdoptionMixin._validate_adopted_scope(uow, run, plan, node, producer, adoption)
        retained = AdoptionMixin._adopted_artifacts(uow, producer, adoption)
        for artifact in retained:
            content = artifact_store.read(artifact.artifact_id)
            if (
                len(content) != artifact.size_bytes
                or sha256(content).hexdigest() != artifact.sha256
            ):
                raise OrchestrationError("Adopted branch evidence bytes changed")
            if artifact.artifact_id in artifact_ids:
                raise OrchestrationError("Branch evidence has ambiguous adoption target nodes")
            artifact_ids.add(artifact.artifact_id)
        adoptions.append(adoption)
        attempts = (*attempts, producer)
        artifacts = (*artifacts, *retained)
    return BranchEvaluationContext(plan, run, attempts, artifacts, tuple(adoptions))
