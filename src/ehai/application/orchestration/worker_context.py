"""Build Worker inputs and context from approved plan facts."""

from __future__ import annotations

from collections.abc import Mapping

from ehai import ID, JsonValue, normalize_id
from ehai.application.interventions import (
    attempt_process_interventions,
    list_interventions,
)
from ehai.application.orchestration.common import (
    OrchestrationError,
    _evaluator_branches,
    _ExecutionContext,
    _required_attempt,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.workers import (
    MAX_ARTIFACT_INPUT_BYTES,
    MAX_ARTIFACT_INPUT_TOTAL_BYTES,
    AdoptedResultInputs,
    ArtifactInputBudgetExceeded,
    ArtifactInputSnapshot,
)
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    EdgeType,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
)


class WorkerContextMixin:
    """Build Worker inputs and context from approved plan facts."""

    def _worker_inputs(
        self: OrchestratorHost, context: _ExecutionContext | AdoptedResultInputs
    ) -> tuple[ArtifactInputSnapshot, ...]:
        plan = context.plan_revision
        node_by_id = {node.plan_node_id: node for node in plan.nodes}
        selected_branch_sources: set[ID] = set()
        selected_branches: dict[ID, Branch] = {}
        if context.plan_node.kind is PlanNodeKind.EVALUATOR:
            evaluator_branches = _evaluator_branches(plan, context.plan_node)
            source_node_ids = {
                node_id
                for branch in evaluator_branches
                if branch.status is BranchStatus.ACTIVE
                for node_id in branch.node_ids
            }
        elif context.plan_node.kind is PlanNodeKind.MERGE:
            selected = _required_selected_branch(plan, context.plan_node.plan_node_id)
            source_node_ids = set(selected.node_ids)
        else:
            source_node_ids = set()
            dependencies = set(context.plan_node.required_dependency_ids)
            dependencies.update(
                edge.source_node_id
                for edge in plan.edges
                if edge.target_node_id == context.plan_node.plan_node_id
                and edge.edge_type is EdgeType.EXPLORATION
            )

            def resolve(source_id: ID, trail: frozenset[ID] = frozenset()) -> None:
                source = node_by_id.get(source_id)
                if source is None:
                    raise OrchestrationError(
                        f"Upstream PlanNode {source_id} is missing from the execution plan"
                    )
                if source.kind is not PlanNodeKind.EVALUATOR:
                    source_node_ids.add(source_id)
                    return
                if source_id in trail:
                    raise OrchestrationError(
                        f"Evaluator dependency cycle reaches PlanNode {source_id}"
                    )
                branches = _evaluator_branches(plan, source)
                selected = tuple(
                    branch for branch in branches if branch.status is BranchStatus.SELECTED
                )
                if len(selected) != 1:
                    raise OrchestrationError(
                        f"Evaluator {source_id} requires exactly one selected Branch before input"
                    )
                selected_branch = selected[0]
                if any(
                    node_by_id[node_id].status is not PlanNodeStatus.COMPLETED
                    for node_id in selected_branch.node_ids
                ):
                    raise OrchestrationError(
                        f"Selected Branch {selected_branch.branch_id} has incomplete inputs"
                    )
                selected_branches[selected_branch.branch_id] = selected_branch
                selected_branch_sources.update(selected_branch.node_ids)
                next_trail = trail | {source_id}
                for node_id in selected_branch.node_ids:
                    resolve(node_id, next_trail)

            for source_id in sorted(dependencies):
                resolve(source_id)
        if not source_node_ids:
            return ()
        adopted_inputs: dict[ID, ResultAdoption] = {}
        with self._uow_factory() as uow:
            latest = {
                attempt.plan_node_id: attempt.attempt_id
                for attempt in sorted(
                    uow.states.list_attempts(context.run.run_id), key=lambda item: item.sequence
                )
                if attempt.status is AttemptStatus.SUCCEEDED
                and attempt.plan_node_id in source_node_ids
            }
            artifacts = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(context.run.run_id)
                if artifact.plan_node_id in source_node_ids
                and artifact.attempt_id == latest.get(artifact.plan_node_id)
                and artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            )
            input_nodes = {artifact.artifact_id: artifact.plan_node_id for artifact in artifacts}
            for adoption in uow.states.list_result_adoptions(context.run.run_id):
                target_id = adoption.target_plan_node_id
                if target_id not in source_node_ids or target_id in latest:
                    continue
                target_node = node_by_id[target_id]
                if target_node.status is not PlanNodeStatus.COMPLETED:
                    continue
                producer = _required_attempt(uow, adoption.source_attempt_id)
                self._validate_adopted_scope(
                    uow, context.run, plan, target_node, producer, adoption
                )
                retained = self._adopted_artifacts(uow, producer, adoption)
                for artifact in retained:
                    if artifact.artifact_id in input_nodes:
                        raise OrchestrationError(
                            "One Artifact has ambiguous target input bindings; consolidate its use"
                        )
                    adopted_inputs[artifact.artifact_id] = adoption
                    input_nodes[artifact.artifact_id] = target_id
                artifacts = (*artifacts, *retained)
            if selected_branches:
                selected_artifact_ids: set[ID] = set()
                for branch in selected_branches.values():
                    selected_event = next(
                        (
                            stored.event
                            for stored in reversed(uow.events.list_events())
                            if stored.event.run_id == context.run.run_id
                            and stored.event.type is EventType.BRANCH_SELECTED
                            and stored.event.correlation_id == branch.branch_id
                        ),
                        None,
                    )
                    if selected_event is None:
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has no persisted selection Event"
                        )
                    payload = selected_event.payload
                    if (
                        payload.get("branch_id") != branch.branch_id
                        or payload.get("fork_node_id") != branch.fork_node_id
                    ):
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has an invalid selection Event"
                        )
                    values = payload.get("selected_artifact_ids")
                    if not isinstance(values, list) or not values:
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has no selected Artifact IDs"
                        )
                    try:
                        normalized = tuple(
                            normalize_id(value) for value in values if isinstance(value, str)
                        )
                    except ValueError as error:
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has invalid selected Artifact IDs"
                        ) from error
                    if len(normalized) != len(values) or len(set(normalized)) != len(normalized):
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} has invalid selected Artifact IDs"
                        )
                    branch_artifact_ids = {
                        artifact.artifact_id
                        for artifact in artifacts
                        if input_nodes[artifact.artifact_id] in branch.node_ids
                    }
                    if not set(normalized).issubset(branch_artifact_ids):
                        raise OrchestrationError(
                            f"selected Branch {branch.branch_id} Artifact inputs are stale, "
                            "incomplete, or outside that Branch"
                        )
                    selected_artifact_ids.update(normalized)
                artifacts = tuple(
                    artifact
                    for artifact in artifacts
                    if input_nodes[artifact.artifact_id] not in selected_branch_sources
                    or artifact.artifact_id in selected_artifact_ids
                )
        return self._artifact_input_snapshots(artifacts, adoptions=adopted_inputs)

    def _worker_context(
        self: OrchestratorHost,
        context: _ExecutionContext,
        artifact_inputs: tuple[ArtifactInputSnapshot, ...],
    ) -> dict[str, JsonValue]:
        from ehai.application.project_configuration import run_configuration

        with self._uow_factory() as uow:
            configuration = run_configuration(uow.events, context.run.run_id)
        result = self._node_worker_context(context, artifact_inputs)
        if configuration is not None:
            result["project_configuration"] = configuration
            result["project_configuration_instruction"] = (
                "Apply static_rules within the approved task and authorized tools. "
                "Rules do not grant permissions or change approved Gates."
            )
        return result

    def _node_worker_context(
        self: OrchestratorHost,
        context: _ExecutionContext,
        artifact_inputs: tuple[ArtifactInputSnapshot, ...],
    ) -> dict[str, JsonValue]:
        if context.plan_node.kind is PlanNodeKind.EVALUATOR:
            evaluator_branches = _evaluator_branches(context.plan_revision, context.plan_node)
            return {
                "process_revision_id": context.attempt.process_revision_id,
                **self._code_task_context(context),
                **self._rework_context(context),
                "approved_design_document": context.plan_revision.design_document,
                "candidate_branches": _candidate_branch_context(
                    context.plan_revision,
                    artifact_inputs,
                    branch_group=evaluator_branches,
                ),
            }
        if context.plan_node.kind is not PlanNodeKind.MERGE:
            return {
                "process_revision_id": context.attempt.process_revision_id,
                **self._code_task_context(context),
                **self._rework_context(context),
                "approved_design_document": context.plan_revision.design_document,
            }
        selected = _required_selected_branch(
            context.plan_revision,
            context.plan_node.plan_node_id,
        )
        with self._uow_factory() as uow:
            selected_event = next(
                (
                    stored.event
                    for stored in reversed(uow.events.list_events())
                    if stored.event.run_id == context.run.run_id
                    and stored.event.type is EventType.BRANCH_SELECTED
                    and stored.event.correlation_id == selected.branch_id
                ),
                None,
            )
        selected_artifacts = tuple(
            artifact
            for artifact in artifact_inputs
            if artifact.effective_plan_node_id in selected.node_ids
        )
        if selected_event is None:
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has no persisted selection Event"
            )
        selection = selected_event.payload
        evidence_ids = selection.get("evidence_artifact_ids")
        selected_ids = selection.get("selected_artifact_ids")
        criterion = selection.get("criterion")
        explanation = selection.get("explanation")
        if (
            selection.get("branch_id") != selected.branch_id
            or selection.get("fork_node_id") != selected.fork_node_id
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or not isinstance(selected_ids, list)
            or not selected_ids
            or not isinstance(criterion, str)
            or not criterion.strip()
            or not isinstance(explanation, str)
            or not explanation.strip()
        ):
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has invalid persisted selection evidence"
            )
        try:
            evidence_artifact_ids = [
                normalize_id(artifact_id)
                for artifact_id in evidence_ids
                if isinstance(artifact_id, str)
            ]
            selected_artifact_ids = [
                normalize_id(artifact_id)
                for artifact_id in selected_ids
                if isinstance(artifact_id, str)
            ]
        except ValueError as error:
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has invalid selection Artifact IDs"
            ) from error
        if len(evidence_artifact_ids) != len(evidence_ids) or len(selected_artifact_ids) != len(
            selected_ids
        ):
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has invalid selection Artifact IDs"
            )
        selected_payload_ids = set(selected_artifact_ids)
        selected_artifact_id_set = {artifact.artifact_id for artifact in selected_artifacts}
        if not selected_payload_ids.issubset(selected_artifact_id_set):
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} payload is outside its candidate Artifacts"
            )
        evidence_json: list[JsonValue] = [str(artifact_id) for artifact_id in evidence_artifact_ids]
        selected_json: list[JsonValue] = [str(artifact_id) for artifact_id in selected_artifact_ids]
        contents: list[JsonValue] = [
            artifact.to_prompt_dict()
            for artifact in selected_artifacts
            if artifact.artifact_id in selected_payload_ids
        ]
        if not contents:
            raise OrchestrationError(
                f"selected Branch {selected.branch_id} has no selected Artifact content"
            )
        return {
            "process_revision_id": context.attempt.process_revision_id,
            **self._code_task_context(context),
            **self._rework_context(context),
            "approved_design_document": context.plan_revision.design_document,
            "branch_selection": {
                "selected_branch_id": selected.branch_id,
                "fork_node_id": selected.fork_node_id,
                "criterion": criterion,
                "explanation": explanation,
                "evidence_artifact_ids": evidence_json,
                "compared_artifact_ids": evidence_json,
                "selected_artifact_ids": selected_json,
            },
            "selected_artifacts": contents,
        }

    def _rework_context(self: OrchestratorHost, context: _ExecutionContext) -> dict[str, JsonValue]:
        with self._uow_factory() as uow:
            replies = [
                item
                for item in list_interventions(uow.events, context.run.run_id)
                if item.get("plan_node_id") == context.plan_node.plan_node_id
                and item.get("status") == "replied"
            ]
            reply_context: dict[str, JsonValue] = (
                {"intervention_reply": replies[-1]} if replies else {}
            )
            succession = next(
                (
                    stored.event.payload
                    for stored in uow.events.list_events()
                    if stored.event.run_id == context.run.run_id
                    and stored.event.type is EventType.RUN_SUCCESSOR_CREATED
                ),
                None,
            )
            if succession is not None:
                reply_context["predecessor_context"] = dict(succession)
                reply_context["predecessor_context_instruction"] = (
                    "Producer IDs and user dispositions below belong to the predecessor. "
                    "Use relevant facts under this Run's new approved plan and permissions. "
                    "Superseded requests are withdrawn, not successful Checks. "
                    "Execute current Gates."
                )
            process_notices = attempt_process_interventions(uow, context.attempt)
            if process_notices:
                reply_context["process_interventions"] = list(process_notices)
                reply_context["process_interventions_instruction"] = (
                    "These are questions and replies pinned when this process was proposed. "
                    "Original node, Attempt and process IDs identify their source, not this task. "
                    "Use relevant replies as continuation facts within the approved boundary; "
                    "they do not grant permissions, change Gates, or resolve open questions. "
                    "A later intervention_reply for this task remains applicable. "
                    "Never assume a replacement route resolves unknown external effects."
                )
            reopening = next(
                (
                    stored.event
                    for stored in reversed(uow.events.list_events())
                    if stored.event.run_id == context.run.run_id
                    and stored.event.type is EventType.PLAN_NODE_REOPENED
                    and stored.event.correlation_id == context.plan_node.plan_node_id
                ),
                None,
            )
            if reopening is None:
                return reply_context
            review_attempt_id = reopening.payload.get("review_attempt_id")
            checks = tuple(
                check
                for check in uow.states.list_check_runs(context.run.run_id)
                if check.attempt_id == review_attempt_id
                and (check.result is None or not check.result.passed)
            )
            reviews = tuple(
                artifact
                for artifact in uow.states.list_artifacts_for_run(context.run.run_id)
                if artifact.attempt_id == review_attempt_id
                and artifact.kind is ArtifactKind.CANDIDATE
                and artifact.name == "review.json"
            )
        reports: list[JsonValue] = []
        for artifact in reviews:
            content = self._artifact_store.read(artifact.artifact_id).decode("utf-8")
            reports.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "content": content[:32000],
                    "truncated": len(content) > 32000,
                }
            )
        failures: list[JsonValue] = [
            {
                "check_id": check.check_id,
                "check_run_id": check.check_run_id,
                "reason": check.failure_reason
                if check.result is None
                else check.result.failure_reason,
                "output": None if check.result is None else (check.result.output or "")[-16000:],
            }
            for check in checks
        ]
        return {
            **reply_context,
            "phase_rework": {
                **reopening.payload,
                "failed_checks": failures,
                "review_reports": reports,
                "instruction": (
                    "Repair the assigned implementation using this review and Gate evidence. "
                    "Keep the approved requirements, interfaces, Gate conditions and permissions."
                ),
            },
        }

    def _code_task_context(
        self: OrchestratorHost, context: _ExecutionContext
    ) -> dict[str, JsonValue]:
        if self._execution_workspace is None:
            return {}
        with self._uow_factory() as uow:
            checks = tuple(
                check
                for check in uow.states.list_check_runs(context.run.run_id)
                if check.plan_node_id == context.plan_node.plan_node_id
                and (check.result is None or not check.result.passed)
            )[-3:]
        failures: list[JsonValue] = [
            {
                "check_id": check.check_id,
                "attempt_id": check.attempt_id,
                "adoption_id": check.adoption_id,
                "reason": (
                    check.failure_reason if check.result is None else check.result.failure_reason
                ),
                "output": None if check.result is None else (check.result.output or "")[-16000:],
            }
            for check in checks
        ]
        return {
            "code_execution": True,
            "gate_failures": failures,
            "workspace_semantics": (
                "The host prepares an isolated worktree containing applicable predecessor code. "
                "Continue from its files, resolve merge conflicts if present, and submit a concise "
                "candidate report. The host captures the actual code, not just the report. "
                "Nodes with approved Checks run their own Gate; the final Gate alone "
                "can complete the Run. A Reviewer submits evidence and recommendations, "
                "not code changes or a Gate decision."
            ),
        }

    def _artifact_input_snapshots(
        self: OrchestratorHost,
        artifacts: tuple[Artifact, ...],
        *,
        adoptions: Mapping[ID, ResultAdoption] | None = None,
    ) -> tuple[ArtifactInputSnapshot, ...]:
        snapshots: list[ArtifactInputSnapshot] = []
        total_bytes = 0
        for artifact in artifacts:
            content = self._artifact_store.read(artifact.artifact_id)
            if len(content) > MAX_ARTIFACT_INPUT_BYTES:
                raise ArtifactInputBudgetExceeded(
                    f"Artifact {artifact.artifact_id} exceeds input limit "
                    f"{MAX_ARTIFACT_INPUT_BYTES} bytes"
                )
            total_bytes += len(content)
            if total_bytes > MAX_ARTIFACT_INPUT_TOTAL_BYTES:
                raise ArtifactInputBudgetExceeded(
                    f"Artifact inputs exceed total limit {MAX_ARTIFACT_INPUT_TOTAL_BYTES} bytes"
                )
            snapshots.append(
                ArtifactInputSnapshot.from_artifact(
                    artifact,
                    content,
                    adoption=None if adoptions is None else adoptions.get(artifact.artifact_id),
                )
            )
        return tuple(snapshots)


def _candidate_branch_context(
    plan: PlanRevision,
    artifact_inputs: tuple[ArtifactInputSnapshot, ...],
    *,
    branch_group: tuple[Branch, ...],
) -> list[JsonValue]:
    """Build the Evaluator's explicit branch-to-candidate content map."""
    branches: list[JsonValue] = []
    node_by_id = {node.plan_node_id: node for node in plan.nodes}
    for branch in branch_group:
        if branch.status is not BranchStatus.ACTIVE:
            continue
        artifacts: list[JsonValue] = [
            artifact.to_prompt_dict()
            for artifact in artifact_inputs
            if artifact.effective_plan_node_id in branch.node_ids
        ]
        branches.append(
            {
                "branch_id": branch.branch_id,
                "label": branch.label,
                "fork_node_id": branch.fork_node_id,
                "merge_node_id": branch.merge_node_id,
                "node_ids": list(branch.node_ids),
                "viable": all(
                    node_by_id[node_id].status is PlanNodeStatus.COMPLETED
                    for node_id in branch.node_ids
                ),
                "artifacts": artifacts,
            }
        )
    return branches


def _required_selected_branch(plan: PlanRevision, merge_node_id: ID) -> Branch:
    selected = tuple(
        branch
        for branch in plan.branches
        if branch.merge_node_id == merge_node_id and branch.status is BranchStatus.SELECTED
    )
    if len(selected) != 1:
        raise OrchestrationError(
            f"Merge PlanNode {merge_node_id} requires exactly one selected Branch"
        )
    return selected[0]
