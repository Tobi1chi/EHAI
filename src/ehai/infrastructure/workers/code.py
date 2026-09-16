"""Host-owned code preparation and result capture around Worker connectors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Protocol, runtime_checkable

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    HostWorkDrainingConnector,
    RuntimeConnector,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.handoffs import AttemptHandoffs, HandoffSubmission, handoff_id_for
from ehai.application.ports import ArtifactStore
from ehai.application.runtime_control import InteractiveRuntimeConnector, _PendingProviderRequest
from ehai.application.workers import (
    AdoptedResultInputs,
    ArtifactInputSnapshot,
    CandidateArtifact,
    WorkerRequest,
)
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.execution import Attempt, AttemptStatus, RunStatus
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
)
from ehai.domain.workers import AttemptActivity
from ehai.infrastructure.code_workspaces import (
    GitCodeResult,
    GitCodeWorkspace,
    MetadataError,
    RunBaseNotFoundError,
)
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workspaces import WorkspaceManager

CODE_SNAPSHOT_MEDIA_TYPE = "application/vnd.ehai.code-snapshot+json"


@runtime_checkable
class _ClosableConnector(Protocol):
    async def close(self) -> None: ...


@runtime_checkable
class _HandoffConnector(Protocol):
    def set_handoff_submitter(
        self, submitter: Callable[[ID, Mapping[str, JsonValue]], dict[str, JsonValue]]
    ) -> None: ...


class CodeRuntimeConnector:
    """Keep actual code separate from model-authored candidate reports."""

    def __init__(
        self,
        connector: RuntimeConnector,
        *,
        database: SQLiteDatabase,
        workspace_manager: WorkspaceManager,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.connector = connector
        self.database = database
        self.workspace_manager = workspace_manager
        self.artifact_store = artifact_store
        self.code_store = GitCodeWorkspace(
            base_workspace=workspace_manager.base_workspace,
            owned_root=workspace_manager.owned_root,
        )
        self._executions: dict[ID, ConnectorExecution] = {}
        self._prepared_commits: dict[ID, str] = {}
        self._handoffs = AttemptHandoffs(database.unit_of_work)
        self._resumption_context: dict[ID, dict[str, JsonValue]] = {}
        self._host_captures: dict[ID, asyncio.Task[GitCodeResult]] = {}
        if isinstance(connector, _HandoffConnector):
            connector.set_handoff_submitter(self._submit_handoff)

    def prepare_adoption_verification(self, adoption_id: ID) -> Path:
        """Check accepted bytes and prepare target-owned code without a Worker call."""
        adoption, _ = self._adopted_snapshot(adoption_id)
        try:
            self.code_store.load_run_base(run_id=str(adoption.target_run_id))
        except RunBaseNotFoundError:
            self.code_store.pin_run_base(run_id=str(adoption.target_run_id), base_commit="HEAD")
        return self.code_store.prepare_adoption_worktree(adoption)

    def integrate_run(self, run_id: ID, process_revision_id: ID) -> dict[str, JsonValue]:
        """Materialize only completed, currently selected blocks and their valid inputs."""
        if self.artifact_store is None:
            raise ValueError("Git integration requires the Artifact store")
        with self.database.read_session() as session:
            run = session.states.get_run(run_id)
            process = session.states.get_active_process_revision(run_id)
            plan = session.states.get_execution_plan(run_id)
            attempts = session.states.list_attempts(run_id)
            artifacts = {a.artifact_id: a for a in session.states.list_artifacts_for_run(run_id)}
            adoptions = session.states.list_result_adoptions(run_id)
        if (
            run is None
            or plan is None
            or process is None
            or process.process_revision_id != process_revision_id
            or run.status not in {RunStatus.PAUSED, RunStatus.COMPLETED}
            or any(a.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING} for a in attempts)
        ):
            raise ValueError(
                "Integration requires a drained paused/completed Run and current process"
            )
        excluded = {
            node_id
            for branch in plan.branches
            if branch.status is not BranchStatus.SELECTED
            for node_id in branch.node_ids
        }
        latest = {a.plan_node_id: a for a in sorted(attempts, key=lambda a: a.sequence)}
        accepted = {a.target_plan_node_id: a for a in adoptions}
        blocks = {b.node_id: b for b in process.block_changes or () if b.node_id is not None}
        sources: list[JsonValue] = []
        dependencies: list[tuple[str, ...]] = []
        result: GitCodeResult | None
        for node in plan.nodes:
            if (
                node.status is not PlanNodeStatus.COMPLETED
                or node.plan_node_id in excluded
                or node.kind is PlanNodeKind.EVALUATOR
            ):
                continue
            attempt = latest.get(node.plan_node_id)
            if attempt is None and node.plan_node_id in accepted:
                _, result = self._adopted_snapshot(accepted[node.plan_node_id].adoption_id)
            else:
                if attempt is None or attempt.status is not AttemptStatus.SUCCEEDED:
                    raise ValueError("Completed block has no successful current producer")
                result = self._snapshot(attempt)
                snapshots = [
                    artifacts[i]
                    for i in attempt.artifact_ids
                    if i in artifacts and artifacts[i].media_type == CODE_SNAPSHOT_MEDIA_TYPE
                ]
                if result is None or len(snapshots) != 1:
                    raise ValueError("Completed block has no unique host code snapshot")
                artifact = snapshots[0]
                content = self.artifact_store.read(artifact.artifact_id)
                if (
                    artifact.attempt_id != attempt.attempt_id
                    or artifact.run_id != run_id
                    or artifact.plan_node_id != node.plan_node_id
                    or len(content) != artifact.size_bytes
                    or sha256(content).hexdigest() != artifact.sha256
                    or json_loads(content.decode("utf-8")) != _delivery(result)
                ):
                    raise ValueError("Block snapshot provenance or bytes changed")
            recorded = self.code_store.load_attempt_dependency_commits(
                run_id=result.run_id, attempt_id=result.attempt_id
            )
            if recorded is None:
                raise ValueError("Block has no recorded input baseline; re-execution is required")
            dependencies.append(recorded)
            block = blocks.get(node.plan_node_id)
            sources.append(
                {
                    "block_id": node.plan_node_id if block is None else block.block_id,
                    "block_version": None if block is None else block.version,
                    "plan_node_id": node.plan_node_id,
                    "source_run_id": result.run_id,
                    "attempt_id": result.attempt_id,
                    "prepared_commit": self.code_store.prepared_commit(
                        run_id=result.run_id, attempt_id=result.attempt_id
                    ),
                    "commit": result.commit,
                }
            )
        commits = {s["commit"] for s in sources if isinstance(s, dict)}
        if any(not set(inputs).issubset(commits) for inputs in dependencies):
            raise ValueError("A selected snapshot contains excluded or superseded block inputs")
        result_document = self.code_store.integrate_results(
            run_id=str(run_id), process_revision_id=str(process_revision_id), sources=sources
        )
        with self.database.read_session() as session:
            if (
                session.states.get_run(run_id) != run
                or session.states.get_execution_plan(run_id) != plan
                or session.states.get_active_process_revision(run_id) != process
                or session.states.list_attempts(run_id) != attempts
            ):
                raise ValueError("Run changed during integration; result is not current")
        return result_document

    def _adopted_snapshot(self, adoption_id: ID) -> tuple[ResultAdoption, GitCodeResult]:
        """Read immutable accepted evidence without allocating a verification workspace."""
        if self.artifact_store is None:
            raise ValueError("Adoption verification requires the configured Artifact store")
        with self.database.read_session() as session:
            adoption = session.states.get_result_adoption(normalize_id(adoption_id))
            if adoption is None:
                raise ValueError("Result adoption is not persisted")
            target = session.states.get_run(adoption.target_run_id)
            source_run = session.states.get_run(adoption.source_run_id)
            source_process = session.states.get_process_revision(
                adoption.source_process_revision_id
            )
            attempt = session.states.get_attempt(adoption.source_attempt_id)
            if (
                target is None
                or source_run is None
                or source_process is None
                or attempt is None
                or (
                    target.predecessor_run_id != adoption.source_run_id
                    or target.plan_revision_id != adoption.target_plan_revision_id
                    or source_run.goal_id != target.goal_id
                    or source_run.plan_revision_id != adoption.source_plan_revision_id
                    or source_process.run_id != source_run.run_id
                    or source_process.graph.plan_revision_id != adoption.source_plan_revision_id
                    or not any(
                        node.plan_node_id == adoption.source_plan_node_id
                        for node in source_process.graph.nodes
                    )
                    or attempt.run_id != adoption.source_run_id
                    or attempt.plan_node_id != adoption.source_plan_node_id
                    or attempt.status is not AttemptStatus.SUCCEEDED
                    or set(attempt.artifact_ids) != {item.artifact_id for item in adoption.evidence}
                )
            ):
                raise ValueError("Adoption does not bind the target and a submitted producer")
            artifacts = tuple(
                session.states.get_artifact(item.artifact_id) for item in adoption.evidence
            )
        source = self.code_store.load_attempt_snapshot(
            run_id=str(adoption.source_run_id), attempt_id=str(adoption.source_attempt_id)
        ).result
        if source is None:
            raise ValueError("Adopted result has no host code snapshot")
        snapshots = 0
        for evidence, artifact in zip(adoption.evidence, artifacts, strict=True):
            if artifact is None or (
                artifact.run_id != adoption.source_run_id
                or artifact.plan_node_id != adoption.source_plan_node_id
                or artifact.attempt_id != adoption.source_attempt_id
                or artifact.sha256 != evidence.sha256
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            ):
                raise ValueError("Adopted Artifact provenance is inconsistent")
            content = self.artifact_store.read(evidence.artifact_id)
            if (
                len(content) != artifact.size_bytes
                or sha256(content).hexdigest() != evidence.sha256
            ):
                raise ValueError("Adopted Artifact bytes do not match accepted evidence")
            if artifact.media_type == CODE_SNAPSHOT_MEDIA_TYPE:
                if json_loads(content.decode("utf-8")) != _delivery(source):
                    raise ValueError("Accepted host snapshot and retained code metadata disagree")
                snapshots += 1
        if snapshots != 1:
            raise ValueError("Adopted code requires exactly one accepted host snapshot")
        return adoption, source

    def _adopted_input_commit(
        self,
        request: WorkerRequest | AdoptedResultInputs,
        artifact: ArtifactInputSnapshot,
        plan: PlanRevision,
    ) -> str:
        if artifact.adoption is None:
            raise ValueError("Cross-Run code input requires an explicit adoption")
        return self._adopted_node_commit(request, artifact.adoption, plan)

    def _adopted_node_commit(
        self,
        request: WorkerRequest | AdoptedResultInputs,
        accepted: ResultAdoption,
        plan: PlanRevision,
    ) -> str:
        adoption, result = self._adopted_snapshot(accepted.adoption_id)
        node = next(
            (node for node in plan.nodes if node.plan_node_id == adoption.target_plan_node_id),
            None,
        )
        with self.database.read_session() as session:
            target_attempts = session.states.list_attempts(request.run_id)
            approved = session.states.get_plan_revision(adoption.target_plan_revision_id)
        approved_node = (
            None
            if approved is None
            else next(
                (
                    item
                    for item in approved.nodes
                    if item.plan_node_id == adoption.target_plan_node_id
                ),
                None,
            )
        )
        if (
            adoption != accepted
            or adoption.target_run_id != request.run_id
            or adoption.target_plan_revision_id != request.run.plan_revision_id
            or adoption.source_run_id != request.run.predecessor_run_id
            or node is None
            or approved_node is None
            or node.status is not PlanNodeStatus.COMPLETED
            or replace(node, status=approved_node.status) != approved_node
            or any(item.plan_node_id == node.plan_node_id for item in target_attempts)
        ):
            raise ValueError("Adopted code input is not the current accepted target result")
        pin = self.code_store.load_run_base(run_id=str(request.run_id))
        if result.base_commit != pin.base_commit:
            raise ValueError("Adopted code input requires the accepted repository baseline")
        return result.commit

    def adoption_inputs_are_applicable(self, request: AdoptedResultInputs) -> bool:
        """Compare actual code inputs before skipping target Worker execution."""
        persisted, source = self._adopted_snapshot(request.adoption.adoption_id)
        if persisted != request.adoption:
            raise ValueError("Input applicability requires the persisted adoption version")
        with self.database.read_session() as session:
            plan = session.states.get_execution_plan(request.run_id)
            run = session.states.get_run(request.run_id)
            attempts = session.states.list_attempts(request.run_id)
        if plan != request.plan_revision or run != request.run:
            raise ValueError("Target execution changed during adoption input validation")
        source_dependencies = self.code_store.load_attempt_dependency_commits(
            run_id=str(persisted.source_run_id), attempt_id=str(persisted.source_attempt_id)
        )
        if source_dependencies is None:
            return False
        try:
            pin = self.code_store.load_run_base(run_id=str(request.run_id))
        except RunBaseNotFoundError:
            pin = self.code_store.pin_run_base(run_id=str(request.run_id), base_commit="HEAD")
        if pin.base_commit != source.base_commit:
            return False
        return source_dependencies == self._dependency_commits(request, plan, attempts)

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        if request.workspace is None:
            raise ValueError("Code execution requires an isolated workspace")
        self._resumption_context.pop(request.attempt_id, None)
        preparation = asyncio.create_task(
            asyncio.to_thread(self._prepare, request.request, Path(request.workspace))
        )
        try:
            conflicts = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            await _drain_capture(preparation)
            raise
        handoff = self._resumption_context.get(request.attempt_id)
        if conflicts or handoff is not None:
            original = request.request
            context = original.context
            if conflicts:
                context["integration_conflicts"] = list(conflicts)
            if handoff is not None:
                context["code_handoff"] = handoff
            request = ConnectorStartRequest(
                WorkerRequest(
                    run=original.run,
                    attempt=original.attempt,
                    plan_node=original.plan_node,
                    completion_contract=original.completion_contract,
                    required_check_specs=original.required_check_specs,
                    artifact_inputs=original.artifact_inputs,
                    context=context,
                ),
                request.workspace,
            )
        execution = await self.connector.start(request)
        self._executions[execution.attempt_id] = execution
        return execution

    def _submit_handoff(
        self, attempt_id: ID, arguments: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        submission = HandoffSubmission.from_mapping(arguments)
        handoff_id = handoff_id_for(attempt_id, submission.key)
        existing = self._handoffs.get(handoff_id)
        if existing is not None:
            if existing.get("submission_fingerprint") != submission.fingerprint():
                raise ValueError("Handoff key was already used for different context")
            self._load_confirmed_handoff(existing)
            return existing
        with self.database.read_session() as session:
            attempt = session.states.get_attempt(attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.RUNNING:
            raise ValueError("Only an active Attempt can submit a new code handoff")
        allocation = self.workspace_manager.allocation_for_attempt(attempt_id)
        if allocation is None or not allocation.reference.ehai_owned:
            raise ValueError("Handoff requires the Attempt's host-owned code workspace")
        dependencies = self.code_store.load_attempt_dependency_commits(
            run_id=str(attempt.run_id), attempt_id=str(attempt_id)
        )
        if dependencies is None:
            raise ValueError("Cannot confirm a handoff without its recorded dependency baseline")
        captured = self.code_store.capture_handoff(
            run_id=str(attempt.run_id),
            attempt_id=str(attempt_id),
            handoff_id=str(handoff_id),
            worktree=Path(allocation.reference.path),
            submission_fingerprint=submission.fingerprint(),
        )
        return self._handoffs.confirm(
            attempt_id,
            submission,
            code_snapshot=_delivery(captured),
            dependency_commits=dependencies,
        )

    def _prepare(self, request: WorkerRequest, workspace: Path) -> tuple[str, ...]:
        allocation = self.workspace_manager.allocation_for_attempt(request.attempt_id)
        if (
            allocation is None
            or not allocation.reference.ehai_owned
            or Path(allocation.reference.path).resolve() != workspace.resolve()
        ):
            raise ValueError("Code execution requires an EHAI-owned allocated worktree")
        try:
            pin = self.code_store.load_run_base(run_id=str(request.run_id))
        except RunBaseNotFoundError:
            pin = self.code_store.pin_run_base(run_id=str(request.run_id), base_commit="HEAD")
        upstreams, dependency_commits = self._upstream_selection(request)
        preparation = self.code_store.prepare_worktree(
            run_id=str(request.run_id),
            attempt_id=str(request.attempt_id),
            worktree=workspace,
            base_commit=pin.base_commit,
            upstream_commits=upstreams,
            dependency_commits=dependency_commits,
        )
        if request.plan_node.kind is PlanNodeKind.REVIEWER:
            if preparation.conflicts:
                raise MetadataError(
                    "Reviewer cannot start with unresolved integration conflicts: "
                    + ", ".join(preparation.conflicts)
                )
            self._prepared_commits[request.attempt_id] = self.code_store.prepared_commit(
                run_id=str(request.run_id), attempt_id=str(request.attempt_id)
            )
        return preparation.conflicts

    def _upstream_commits(self, request: WorkerRequest) -> tuple[str, ...]:
        """Return actual preparation heads, reusing handoffs only for equal inputs."""
        upstreams, _ = self._upstream_selection(request)
        return upstreams

    def _upstream_selection(
        self, request: WorkerRequest
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        with self.database.read_session() as session:
            plan = session.states.get_execution_plan(request.run.run_id)
            attempts = tuple(
                sorted(session.states.list_attempts(request.run_id), key=lambda item: item.sequence)
            )
        if plan is None:
            raise ValueError("Code execution plan is missing")
        dependency_commits = self._dependency_commits(request, plan, attempts)
        if request.plan_node.kind is PlanNodeKind.EVALUATOR:
            return (), dependency_commits
        if request.plan_node.kind is not PlanNodeKind.REVIEWER:
            previous = [
                attempt
                for attempt in attempts
                if attempt.plan_node_id == request.plan_node_id
                and attempt.attempt_id != request.attempt_id
                and attempt.status is AttemptStatus.SUCCEEDED
            ]
            handoff = self._compatible_handoff(
                request,
                attempts,
                dependency_commits,
                newer_than=0 if not previous else previous[-1].sequence,
            )
            if handoff is not None:
                self._resumption_context[request.attempt_id] = handoff[1]
                return (handoff[0].commit,), dependency_commits
            if previous:
                attempt = previous[-1]
                try:
                    previous_dependencies = self.code_store.load_attempt_dependency_commits(
                        run_id=str(attempt.run_id), attempt_id=str(attempt.attempt_id)
                    )
                except MetadataError as error:
                    raise ValueError(
                        f"Submitted code handoff for Attempt {attempt.attempt_id} "
                        "has no valid dependency baseline"
                    ) from error
                # Legacy metadata without an input baseline is retained, not reused.
                # Rebuild this new Attempt from valid dependencies instead of blocking
                # recovery or guessing that an old candidate has the same inputs.
                if (
                    previous_dependencies is not None
                    and previous_dependencies == dependency_commits
                ):
                    result = self._snapshot(attempt)
                    if result is None:
                        raise ValueError(
                            f"Submitted code handoff for Attempt {attempt.attempt_id} is missing"
                        )
                    return (result.commit,), dependency_commits
            else:
                adopted = self._adopted_rework_base(request, dependency_commits)
                if adopted is not None:
                    return (adopted.commit,), dependency_commits
        return dependency_commits, dependency_commits

    def _adopted_rework_base(
        self, request: WorkerRequest, dependency_commits: tuple[str, ...]
    ) -> GitCodeResult | None:
        """Reuse accepted code for repair only while its actual inputs remain applicable."""
        if request.plan_node.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            return None
        with self.database.read_session() as session:
            record = next(
                (
                    item
                    for item in session.states.list_result_adoptions(request.run_id)
                    if item.target_plan_node_id == request.plan_node_id
                ),
                None,
            )
            approved = session.states.get_plan_revision(request.run.plan_revision_id)
        if record is None:
            return None
        approved_node = (
            None
            if approved is None
            else next(
                (node for node in approved.nodes if node.plan_node_id == request.plan_node_id),
                None,
            )
        )
        if (
            approved_node is None
            or record.target_plan_revision_id != request.run.plan_revision_id
            or record.source_run_id != request.run.predecessor_run_id
            or replace(request.plan_node, status=approved_node.status) != approved_node
        ):
            return None
        dependencies = self.code_store.load_attempt_dependency_commits(
            run_id=str(record.source_run_id), attempt_id=str(record.source_attempt_id)
        )
        # Missing legacy input metadata is not evidence of equal inputs. A changed
        # dependency rebuilds from current inputs, just like ordinary same-Run rework.
        if dependencies is None or dependencies != dependency_commits:
            return None
        persisted, source = self._adopted_snapshot(record.adoption_id)
        if persisted != record:
            raise ValueError("Adopted repair binding changed during code preparation")
        pin = self.code_store.load_run_base(run_id=str(request.run_id))
        if source.base_commit != pin.base_commit:
            return None
        return source

    def _compatible_handoff(
        self,
        request: WorkerRequest,
        attempts: tuple[Attempt, ...],
        dependency_commits: tuple[str, ...],
        *,
        newer_than: int,
    ) -> tuple[GitCodeResult, dict[str, JsonValue]] | None:
        sources = {
            attempt.attempt_id: attempt
            for attempt in attempts
            if attempt.plan_node_id == request.plan_node_id
            and attempt.sequence > newer_than
            and attempt.status
            in {
                AttemptStatus.FAILED,
                AttemptStatus.INTERRUPTED,
                AttemptStatus.TIMED_OUT,
                AttemptStatus.CANCELLED,
            }
        }
        for record in reversed(self._handoffs.list_for_run(request.run_id)):
            source_id = record.get("attempt_id")
            if not isinstance(source_id, str) or normalize_id(source_id) not in sources:
                continue
            if record.get("plan_revision_id") != request.run.plan_revision_id:
                continue
            if record.get("dependency_commits") != list(dependency_commits):
                continue
            captured = self._load_confirmed_handoff(record)
            return captured, record
        return None

    def _load_confirmed_handoff(self, record: Mapping[str, JsonValue]) -> GitCodeResult:
        run_id, attempt_id = record.get("run_id"), record.get("attempt_id")
        handoff_id, fingerprint = record.get("handoff_id"), record.get("submission_fingerprint")
        if not all(
            isinstance(value, str) for value in (run_id, attempt_id, handoff_id, fingerprint)
        ):
            raise MetadataError("Confirmed handoff has incomplete identity")
        assert isinstance(run_id, str) and isinstance(attempt_id, str)
        assert isinstance(handoff_id, str) and isinstance(fingerprint, str)
        captured = self.code_store.load_handoff_snapshot(
            run_id=run_id,
            attempt_id=attempt_id,
            handoff_id=handoff_id,
            submission_fingerprint=fingerprint,
        )
        if captured is None or record.get("code_snapshot") != _delivery(captured):
            raise MetadataError("Confirmed handoff code is missing or changed")
        return captured

    def _dependency_commits(
        self,
        request: WorkerRequest | AdoptedResultInputs,
        plan: PlanRevision,
        attempts: tuple[Attempt, ...],
    ) -> tuple[str, ...]:
        if request.plan_node.kind is PlanNodeKind.EVALUATOR:
            return ()
        sources = set(request.plan_node.required_dependency_ids)
        sources.update(
            edge.source_node_id
            for edge in plan.edges
            if edge.target_node_id == request.plan_node_id
            and edge.edge_type is EdgeType.EXPLORATION
        )
        if request.plan_node.kind is PlanNodeKind.MERGE:
            sources.update(
                node_id
                for branch in plan.branches
                if branch.merge_node_id == request.plan_node_id
                and branch.status is BranchStatus.SELECTED
                for node_id in branch.node_ids
            )
        source_nodes = {node.plan_node_id: node for node in plan.nodes}
        direct_sources: set[ID] = set()
        expanded_sources: set[ID] = set()
        expanded_evaluators: set[ID] = set()
        evaluator_scopes: dict[ID, set[ID]] = {}
        root_evaluators: set[ID] = set()

        # Merge already receives the selected Branch's complete code history. Keep
        # that established preparation contract intact; only ordinary dependencies
        # use selected Artifact identity to expand an Evaluator.
        if request.plan_node.kind is PlanNodeKind.MERGE:
            for source in sorted(sources):
                node = source_nodes.get(source)
                if node is None:
                    raise ValueError(
                        f"Upstream PlanNode {source} is missing from the execution plan"
                    )
                if node.kind is not PlanNodeKind.EVALUATOR:
                    direct_sources.add(source)

        def resolve(source: ID, trail: frozenset[ID] = frozenset()) -> set[ID]:
            node = source_nodes.get(source)
            if node is None:
                raise ValueError(f"Upstream PlanNode {source} is missing from the execution plan")
            if node.kind is not PlanNodeKind.EVALUATOR:
                if trail:
                    expanded_sources.add(source)
                else:
                    direct_sources.add(source)
                return {source}
            if source in trail:
                raise ValueError(f"Evaluator dependency cycle reaches PlanNode {source}")
            expanded_evaluators.add(source)
            if not trail:
                root_evaluators.add(source)
            branches = _evaluator_branches(plan, node)
            selected = tuple(
                branch for branch in branches if branch.status is BranchStatus.SELECTED
            )
            if len(selected) != 1:
                raise ValueError(
                    f"Evaluator {source} requires exactly one selected Branch before code input"
                )
            selected_branch = selected[0]
            selected_nodes: list[PlanNode] = []
            for node_id in selected_branch.node_ids:
                selected_node = source_nodes.get(node_id)
                if selected_node is None:
                    raise ValueError(
                        f"Selected Branch {selected_branch.branch_id} references a missing "
                        f"PlanNode {node_id}"
                    )
                selected_nodes.append(selected_node)
            if any(node.status is not PlanNodeStatus.COMPLETED for node in selected_nodes):
                raise ValueError(
                    f"Selected Branch {selected_branch.branch_id} has incomplete code sources"
                )
            next_trail = trail | {source}
            scope: set[ID] = set()
            for node_id in selected_branch.node_ids:
                scope.update(resolve(node_id, next_trail))
            evaluator_scopes[source] = scope
            return scope

        if request.plan_node.kind is not PlanNodeKind.MERGE:
            for source in sorted(sources):
                resolve(source)

        # A selected Artifact is the only authority for code introduced by an
        # Evaluator. Its text/media type is intentionally irrelevant: candidate
        # and patch Artifacts may both carry the evidence for the same host snapshot.
        selected_commits: dict[ID, str] = {}
        if expanded_evaluators:
            attempt_by_id = {attempt.attempt_id: attempt for attempt in attempts}
            latest_successful = {
                attempt.plan_node_id: attempt
                for attempt in sorted(attempts, key=lambda item: item.sequence)
                if attempt.run_id == request.run_id
                if attempt.plan_node_id in expanded_sources
                and attempt.status is AttemptStatus.SUCCEEDED
            }
            selected_inputs = tuple(
                artifact
                for artifact in request.artifact_inputs
                if artifact.effective_plan_node_id in expanded_sources
            )
            for evaluator_id in sorted(root_evaluators):
                scope = evaluator_scopes.get(evaluator_id, set()) & expanded_sources
                if not any(
                    artifact.effective_plan_node_id in scope for artifact in selected_inputs
                ):
                    raise ValueError(
                        f"Evaluator {evaluator_id} has no selected code Artifact evidence"
                    )
            if not selected_inputs:
                raise ValueError(
                    "Evaluator-expanded code sources have no selected Artifact evidence"
                )
            for artifact in selected_inputs:
                if artifact.adoption is not None:
                    selected_commits[artifact.effective_plan_node_id] = self._adopted_input_commit(
                        request, artifact, plan
                    )
                    continue
                if artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}:
                    raise ValueError(
                        f"Selected Artifact {artifact.artifact_id} is not candidate evidence"
                    )
                if artifact.run_id != request.run_id or artifact.attempt_id is None:
                    raise ValueError(
                        f"Selected Artifact {artifact.artifact_id} has invalid Run/Attempt scope"
                    )
                predecessor = attempt_by_id.get(artifact.attempt_id)
                latest_predecessor = latest_successful.get(artifact.plan_node_id)
                if (
                    predecessor is None
                    or predecessor.run_id != request.run_id
                    or predecessor.plan_node_id != artifact.plan_node_id
                    or predecessor.status is not AttemptStatus.SUCCEEDED
                    or latest_predecessor is None
                    or latest_predecessor.attempt_id != predecessor.attempt_id
                ):
                    raise ValueError(
                        f"Selected Artifact {artifact.artifact_id} is not from the latest "
                        "successful Attempt for its source node"
                    )
                result = self._snapshot(predecessor)
                if (
                    result is None
                    or result.run_id != request.run_id
                    or result.attempt_id != predecessor.attempt_id
                ):
                    raise ValueError(
                        f"Selected Artifact {artifact.artifact_id} has no matching host code "
                        "snapshot"
                    )
                selected_commits[artifact.plan_node_id] = result.commit

        sources = direct_sources - expanded_sources
        latest = {
            attempt.plan_node_id: attempt
            for attempt in sorted(attempts, key=lambda item: item.sequence)
            if attempt.run_id == request.run_id
            if attempt.plan_node_id in sources and attempt.status is AttemptStatus.SUCCEEDED
        }
        merge_adoptions: dict[ID, ResultAdoption] = {}
        if request.plan_node.kind is PlanNodeKind.MERGE:
            # MERGE integrates the selected Branch's complete code history even
            # when its text inputs only include the Evaluator-selected subset.
            with self.database.read_session() as session:
                merge_adoptions = {
                    item.target_plan_node_id: item
                    for item in session.states.list_result_adoptions(request.run_id)
                    if item.target_plan_node_id in sources
                }
        commits: list[str] = []
        for source in sorted((*sources, *selected_commits)):
            predecessor = latest.get(source)
            commit = selected_commits.get(source)
            adopted_inputs = tuple(
                artifact
                for artifact in request.artifact_inputs
                if artifact.effective_plan_node_id == source and artifact.adoption is not None
            )
            if commit is None and adopted_inputs:
                adopted_commits = {
                    self._adopted_input_commit(request, artifact, plan)
                    for artifact in adopted_inputs
                }
                if len(adopted_commits) != 1:
                    raise ValueError("Adopted inputs disagree on the source code commit")
                commit = adopted_commits.pop()
            if commit is None and predecessor is None and source in merge_adoptions:
                commit = self._adopted_node_commit(request, merge_adoptions[source], plan)
            if commit is None:
                result = None if predecessor is None else self._snapshot(predecessor)
                if result is None:
                    raise ValueError(f"Upstream code snapshot for node {source} is missing")
                commit = result.commit
            if commit not in commits:
                commits.append(commit)
        return tuple(commits)

    def _snapshot(self, attempt: Attempt) -> GitCodeResult | None:
        try:
            snapshot = self.code_store.load_attempt_snapshot(
                run_id=str(attempt.run_id), attempt_id=str(attempt.attempt_id)
            )
        except MetadataError:
            return None
        return snapshot.result

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        async for event in self.connector.events(execution, after_cursor=after_cursor):
            if event.type is WorkerEventType.CANDIDATE and event.result is not None:
                with self.database.read_session() as session:
                    attempt = session.states.get_attempt(execution.attempt_id)
                    plan = None if attempt is None else session.states.get_run(attempt.run_id)
                    revision = (
                        None if plan is None else session.states.get_execution_plan(plan.run_id)
                    )
                if attempt is None or revision is None:
                    raise ValueError("Candidate execution context is missing")
                node = next(
                    node for node in revision.nodes if node.plan_node_id == attempt.plan_node_id
                )
                if node.kind is not PlanNodeKind.EVALUATOR:
                    captured = await self._capture_candidate(attempt)
                    if node.kind is PlanNodeKind.REVIEWER:
                        expected = self._prepared_commits.get(attempt.attempt_id)
                        if expected is None:
                            expected = self.code_store.prepared_commit(
                                run_id=str(attempt.run_id), attempt_id=str(attempt.attempt_id)
                            )
                        if captured.commit != expected:
                            raise ValueError("Reviewer modified its read-only code workspace")
                    if any(
                        artifact.media_type == CODE_SNAPSHOT_MEDIA_TYPE
                        or artifact.name in {"ehai-code-snapshot.json", "solution.patch"}
                        for artifact in event.result.artifacts
                    ):
                        raise ValueError("Worker cannot impersonate host code artifacts")
                    snapshot = CandidateArtifact(
                        ArtifactKind.PATCH,
                        "ehai-code-snapshot.json",
                        CODE_SNAPSHOT_MEDIA_TYPE,
                        json_dumps(_delivery(captured)).encode("utf-8"),
                    )
                    patch = CandidateArtifact(
                        ArtifactKind.LOG,
                        "solution.patch",
                        "text/x-diff",
                        self.code_store.read_diff(captured),
                    )
                    event = replace(
                        event,
                        result=replace(
                            event.result,
                            artifacts=(*event.result.artifacts, snapshot, patch),
                        ),
                    )
            yield event

    async def _capture_candidate(self, attempt: Attempt) -> GitCodeResult:
        task = asyncio.create_task(asyncio.to_thread(self._capture, attempt))
        self._host_captures[attempt.attempt_id] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _drain_capture(task)
            raise
        finally:
            if task.done():
                self._host_captures.pop(attempt.attempt_id, None)

    def _capture(self, attempt: Attempt) -> GitCodeResult:
        allocation = self.workspace_manager.allocation_for_attempt(attempt.attempt_id)
        if allocation is None:
            raise ValueError("Attempt code workspace is missing")
        self.code_store.resume_worktree(
            run_id=str(attempt.run_id), attempt_id=str(attempt.attempt_id)
        )
        return self.code_store.capture_result(
            run_id=str(attempt.run_id),
            attempt_id=str(attempt.attempt_id),
            worktree=Path(allocation.reference.path),
        )

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        return await self.connector.inspect(execution)

    async def cancel(self, execution: ConnectorExecution) -> None:
        await self.connector.cancel(execution)

    async def wait_for_host_work(self, execution: ConnectorExecution) -> None:
        """Wait for host-owned local work when the wrapped Connector exposes it."""
        try:
            if isinstance(self.connector, HostWorkDrainingConnector):
                await self.connector.wait_for_host_work(execution)
        finally:
            task = self._host_captures.get(execution.attempt_id)
            if task is not None:
                await _drain_capture(task)

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        with self.database.read_session() as session:
            attempt = session.states.get_attempt(request.attempt_id)
            run = None if attempt is None else session.states.get_run(attempt.run_id)
            plan = None if run is None else session.states.get_execution_plan(run.run_id)
        if (
            attempt is not None
            and plan is not None
            and any(
                node.plan_node_id == attempt.plan_node_id and node.kind is PlanNodeKind.REVIEWER
                for node in plan.nodes
            )
        ):
            self._prepared_commits[attempt.attempt_id] = self.code_store.prepared_commit(
                run_id=str(attempt.run_id), attempt_id=str(attempt.attempt_id)
            )
        execution = await self.connector.recover(request)
        if execution is not None:
            self._executions[execution.attempt_id] = execution
        return execution

    def pending_requests(
        self, execution: ConnectorExecution | None = None
    ) -> tuple[_PendingProviderRequest, ...]:
        if isinstance(self.connector, InteractiveRuntimeConnector):
            return self.connector.pending_requests(execution)
        return ()

    async def resolve_request(self, request_id: int | str, result: Mapping[str, JsonValue]) -> None:
        if not isinstance(self.connector, InteractiveRuntimeConnector):
            raise ValueError("This Worker has no interactive requests")
        await self.connector.resolve_request(request_id, result)

    async def close(self) -> None:
        if isinstance(self.connector, _ClosableConnector):
            await self.connector.close()
        for task in tuple(self._host_captures.values()):
            await _drain_capture(task)
        errors: list[str] = []
        for attempt_id in self._executions:
            with self.database.read_session() as session:
                attempt = session.states.get_attempt(attempt_id)
                run = None if attempt is None else session.states.get_run(attempt.run_id)
                plan = None if run is None else session.states.get_execution_plan(run.run_id)
            if (
                plan is not None
                and attempt is not None
                and any(
                    node.plan_node_id == attempt.plan_node_id
                    and node.kind is PlanNodeKind.EVALUATOR
                    for node in plan.nodes
                )
            ):
                continue
            if attempt is None or self._snapshot(attempt) is not None:
                continue
            try:
                await asyncio.to_thread(self._capture, attempt)
            except (ValueError, OSError, RuntimeError) as error:
                errors.append(f"Attempt {attempt_id}: {error}")
        if errors:
            raise RuntimeError(
                "Workers stopped; code worktrees retained but snapshot capture needs attention: "
                + "; ".join(errors)
            )

    def result(self, run_id: ID) -> dict[str, JsonValue]:
        with self.database.read_session() as session:
            attempts = sorted(
                session.states.list_attempts(run_id), key=lambda item: item.sequence, reverse=True
            )
        for attempt in attempts:
            result = self._snapshot(attempt)
            if result is not None:
                return {"code_delivery": _delivery(result)}
        return {}


async def _drain_capture[HostResult](task: asyncio.Task[HostResult]) -> None:
    drain = asyncio.gather(task, return_exceptions=True)
    cancelled = False
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            cancelled = True
    drain.result()
    if cancelled:
        raise asyncio.CancelledError


def _evaluator_branches(plan: PlanRevision, evaluator: PlanNode) -> tuple[Branch, ...]:
    """Return the one Branch group whose terminals feed an Evaluator."""
    dependencies = {
        edge.source_node_id
        for edge in plan.edges
        if edge.target_node_id == evaluator.plan_node_id and edge.edge_type is EdgeType.DEPENDENCY
    }
    groups: dict[tuple[ID, ID], list[Branch]] = {}
    for branch in plan.branches:
        groups.setdefault((branch.fork_node_id, branch.merge_node_id), []).append(branch)
    matches = tuple(
        tuple(branches)
        for branches in groups.values()
        if {branch.node_ids[-1] for branch in branches}.issubset(dependencies)
    )
    if len(matches) != 1:
        raise ValueError(
            f"Evaluator {evaluator.plan_node_id} must feed exactly one complete Branch group"
        )
    return matches[0]


def _delivery(result: GitCodeResult) -> dict[str, JsonValue]:
    return {
        "workspace": str(result.worktree),
        "base_commit": result.base_commit,
        "commit": result.commit,
        "diff_path": str(result.diff_path),
        "attempt_id": result.attempt_id,
        "run_id": result.run_id,
    }
