"""Verify and accept results adopted from earlier Runs."""

from __future__ import annotations

from ehai import ID, normalize_id
from ehai.application.orchestration.common import (
    AdoptionInputsChanged,
    OrchestrationError,
    _replace_node,
    _required_check_specs,
    _required_node,
    _required_plan,
    _required_run,
    _review_rework_uses_planner,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.orchestration.readiness import (
    _branch_containing,
    _exploration_dependencies_completed,
    ready_nodes,
)
from ehai.application.ports import UnitOfWork
from ehai.application.process_control import run_contract_is_current
from ehai.application.workers import (
    AdoptedResultInputs,
)
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)


class AdoptionMixin:
    """Verify and accept results adopted from earlier Runs."""

    def advance_ready_adoptions(self: OrchestratorHost, run_id: ID) -> Run | None:
        """Admit ready retained results before Worker dispatch, outside queue transactions."""
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status is not RunStatus.RUNNING or not uow.states.list_result_adoptions(run_id):
                return None
        # Resolve a just-completed Evaluator before queueing its adopted Merge.
        # Otherwise ordinary dispatch would create an unnecessary target Attempt.
        while self._evaluate_completed_evaluator(run_id):
            pass
        examined: set[ID] = set()
        advanced: Run | None = None
        while True:
            with self._uow_factory() as uow:
                run = _required_run(uow, run_id)
                records = uow.states.list_result_adoptions(run_id)
                if not records or run.status is not RunStatus.RUNNING:
                    return advanced
                plan = _required_plan(uow, run_id)
                if not run_contract_is_current(uow, run, plan):
                    return advanced
                if _review_rework_uses_planner(uow, run_id) and any(
                    node.kind is PlanNodeKind.REVIEWER and node.status is PlanNodeStatus.FAILED
                    for node in plan.nodes
                ):
                    return advanced
                executed = {item.plan_node_id for item in uow.states.list_attempts(run_id)}
                submitted = {
                    stored.event.payload.get("adoption_id")
                    for stored in uow.events.list_events()
                    if stored.event.run_id == run_id
                    and stored.event.type is EventType.PLAN_NODE_CANDIDATE_SUBMITTED
                    and isinstance(stored.event.payload.get("adoption_id"), str)
                }
                ready_ids = {node.plan_node_id for node in ready_nodes(plan)} | {
                    node.plan_node_id
                    for node in plan.nodes
                    if node.status is PlanNodeStatus.READY
                    and _exploration_dependencies_completed(plan, node)
                }
                adoption = next(
                    (
                        item
                        for item in records
                        if item.adoption_id not in examined
                        and item.adoption_id not in submitted
                        and item.target_plan_node_id not in executed
                        and item.target_plan_node_id in ready_ids
                    ),
                    None,
                )
            if adoption is None:
                return advanced
            examined.add(adoption.adoption_id)
            try:
                advanced = self.accept_adopted_result(adoption.adoption_id)
            except AdoptionInputsChanged:
                # Leave the node ready for normal Worker execution. No synthetic
                # Attempt or reused Gate result represents the declined adoption.
                continue

    def accept_adopted_result(self: OrchestratorHost, adoption_id: ID) -> Run:
        """Advance an admitted, ready result without creating a Worker Attempt.

        Host validation establishes actual target input applicability before
        accepting a candidate. This is not a public approval entry point.
        """
        normalized_id = normalize_id(adoption_id)
        with self._uow_factory() as uow:
            adoption = uow.states.get_result_adoption(normalized_id)
            if adoption is None:
                raise OrchestrationError("Result adoption is not persisted")
            observed = self._load_verification_scope(uow, adoption.source_attempt_id, normalized_id)
            if observed.run.status is not RunStatus.RUNNING:
                raise OrchestrationError("Adopted result requires a running target Run")
            if observed.plan_node.status is PlanNodeStatus.COMPLETED:
                return observed.run
        input_context = AdoptedResultInputs(
            observed.run, observed.plan_revision, observed.plan_node, adoption
        )
        inputs = AdoptedResultInputs(
            observed.run,
            observed.plan_revision,
            observed.plan_node,
            adoption,
            self._worker_inputs(input_context),
        )
        if self._adoption_inputs is None:
            raise OrchestrationError("Adoption input applicability validator is not configured")
        if not self._adoption_inputs(inputs):
            raise AdoptionInputsChanged(
                "Adopted result inputs changed; target needs fresh execution"
            )
        with self._uow_factory() as uow:
            adoption = uow.states.get_result_adoption(normalized_id)
            if adoption is None:
                raise OrchestrationError("Result adoption is not persisted")
            scope = self._load_verification_scope(uow, adoption.source_attempt_id, normalized_id)
            if scope != observed:
                raise OrchestrationError("Adoption target changed during input validation")
            run, plan, node = scope.run, scope.plan_revision, scope.plan_node
            if run.status is not RunStatus.RUNNING:
                raise OrchestrationError("Adopted result requires a running target Run")
            if node.status is PlanNodeStatus.COMPLETED:
                return run
            if node.status in {PlanNodeStatus.PENDING, PlanNodeStatus.READY}:
                if any(
                    check.adoption_id == normalized_id
                    for check in uow.states.list_check_runs(run.run_id)
                ):
                    raise OrchestrationError(
                        "Adopted result was already evaluated; rework needs a new result"
                    )
                readiness_plan = (
                    plan
                    if node.status is PlanNodeStatus.PENDING
                    else _replace_node(plan, node.reopen_for_rework())
                )
                if node.plan_node_id not in {
                    item.plan_node_id for item in ready_nodes(readiness_plan)
                }:
                    raise OrchestrationError("Adopted result dependencies are not ready")
                self._verification_workspace(scope)
                if node.status is PlanNodeStatus.PENDING:
                    node = node.mark_ready()
                    plan = _replace_node(plan, node)
                    uow.states.put_execution_plan(run.run_id, plan)
                node = node.submit_adopted_candidate(adoption)
                plan = _replace_node(plan, node)
                uow.states.put_execution_plan(run.run_id, plan)
                uow.events.append(
                    self._event(
                        EventType.PLAN_NODE_CANDIDATE_SUBMITTED,
                        run,
                        node.plan_node_id,
                        {
                            "plan_node_id": node.plan_node_id,
                            "acceptance": "adopted_result",
                            "adoption_id": normalized_id,
                            "source_attempt_id": scope.attempt.attempt_id,
                        },
                    )
                )
            elif node.status not in {PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING}:
                raise OrchestrationError("Adopted target is not ready for acceptance")
            specs = _required_check_specs(uow, plan, node)
            uow.commit()
        return self._check_and_complete(
            run.run_id,
            scope.attempt.attempt_id,
            scope.artifacts,
            specs,
            branch_local=_branch_containing(plan, node.plan_node_id) is not None,
            adoption_id=normalized_id,
        )

    @staticmethod
    def _validate_adopted_scope(
        uow: UnitOfWork,
        run: Run,
        plan: PlanRevision,
        node: PlanNode,
        attempt: Attempt,
        adoption: ResultAdoption,
    ) -> None:
        if (
            adoption.target_run_id != run.run_id
            or adoption.target_plan_revision_id != run.plan_revision_id
            or adoption.target_plan_node_id != node.plan_node_id
            or run.predecessor_run_id != adoption.source_run_id
            or attempt.run_id != adoption.source_run_id
            or attempt.plan_node_id != adoption.source_plan_node_id
            or attempt.attempt_id != adoption.source_attempt_id
            or attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise OrchestrationError("Verification has no matching adopted producer")
        if plan.status is not PlanRevisionStatus.APPROVED:
            raise OrchestrationError("Adopted target PlanRevision is not approved")
        if any(
            item.plan_node_id == node.plan_node_id for item in uow.states.list_attempts(run.run_id)
        ):
            raise OrchestrationError("Target execution has superseded the adopted result")

        approved = uow.states.get_plan_revision(run.plan_revision_id)
        if approved is None:
            raise OrchestrationError(
                f"Adopted target PlanRevision {run.plan_revision_id} is not persisted"
            )
        approved_node = _required_node(approved, node.plan_node_id)
        if _node_definition(node) != _node_definition(approved_node):
            raise OrchestrationError("Adopted target node changed after its acceptance")

        source_run = _required_run(uow, adoption.source_run_id)
        if (
            source_run.goal_id != run.goal_id
            or source_run.plan_revision_id != adoption.source_plan_revision_id
        ):
            raise OrchestrationError("Adopted source Run identity is no longer valid")
        source_plan = uow.states.get_plan_revision(adoption.source_plan_revision_id)
        if source_plan is None or (
            source_plan.status is not PlanRevisionStatus.APPROVED
            or source_plan.goal_id != source_run.goal_id
        ):
            raise OrchestrationError("Adopted source PlanRevision is no longer valid")
        process = uow.states.get_process_revision(adoption.source_process_revision_id)
        if process is None or (
            process.run_id != source_run.run_id
            or process.graph.plan_revision_id != adoption.source_plan_revision_id
            or process.graph.goal_id != source_run.goal_id
        ):
            raise OrchestrationError("Adopted source ProcessRevision is no longer valid")
        _required_node(process.graph, adoption.source_plan_node_id)

    @staticmethod
    def _adopted_artifacts(
        uow: UnitOfWork,
        attempt: Attempt,
        adoption: ResultAdoption,
    ) -> tuple[Artifact, ...]:
        expected = {item.artifact_id: item.sha256 for item in adoption.evidence}
        if set(attempt.artifact_ids) != set(expected):
            raise OrchestrationError("Adopted evidence no longer matches the source Attempt")
        artifacts_by_id = {
            artifact.artifact_id: artifact
            for artifact in uow.states.list_artifacts_for_run(adoption.source_run_id)
        }
        artifacts: list[Artifact] = []
        for artifact_id in attempt.artifact_ids:
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None or (
                artifact.run_id != adoption.source_run_id
                or artifact.plan_node_id != adoption.source_plan_node_id
                or artifact.attempt_id != adoption.source_attempt_id
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or artifact.sha256 != expected.get(artifact_id)
            ):
                raise OrchestrationError("Adopted verification evidence is inconsistent")
            artifacts.append(artifact)
        return tuple(artifacts)


def _node_definition(node: PlanNode) -> tuple[object, ...]:
    """Return a PlanNode's immutable task definition without lifecycle status."""
    return (
        node.plan_node_id,
        node.title,
        node.instruction,
        node.kind,
        node.required_dependency_ids,
        node.required_check_ids,
        node.required_capabilities,
        node.session_policy,
    )
