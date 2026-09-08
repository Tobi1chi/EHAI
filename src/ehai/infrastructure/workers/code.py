"""Host-owned code preparation and result capture around Worker connectors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from ehai import ID, JsonValue, json_dumps
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    RuntimeConnector,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.runtime_control import InteractiveRuntimeConnector, _PendingProviderRequest
from ehai.application.workers import CandidateArtifact, WorkerRequest
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.execution import Attempt, AttemptStatus
from ehai.domain.planning import BranchStatus, EdgeType, PlanNodeKind
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


class CodeRuntimeConnector:
    """Keep actual code separate from model-authored candidate reports."""

    def __init__(
        self,
        connector: RuntimeConnector,
        *,
        database: SQLiteDatabase,
        workspace_manager: WorkspaceManager,
    ) -> None:
        self.connector = connector
        self.database = database
        self.workspace_manager = workspace_manager
        self.code_store = GitCodeWorkspace(
            base_workspace=workspace_manager.base_workspace,
            owned_root=workspace_manager.owned_root,
        )
        self._executions: dict[ID, ConnectorExecution] = {}

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        if request.workspace is None:
            raise ValueError("Code execution requires an isolated workspace")
        conflicts = await asyncio.to_thread(self._prepare, request.request, Path(request.workspace))
        if conflicts:
            original = request.request
            context = original.context
            context["integration_conflicts"] = list(conflicts)
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
        upstreams = self._upstream_commits(request)
        preparation = self.code_store.prepare_worktree(
            run_id=str(request.run_id),
            attempt_id=str(request.attempt_id),
            worktree=workspace,
            base_commit=pin.base_commit,
            upstream_commits=upstreams,
        )
        return preparation.conflicts

    def _upstream_commits(self, request: WorkerRequest) -> tuple[str, ...]:
        with self.database.read_session() as session:
            plan = session.states.get_plan_revision(request.run.plan_revision_id)
            attempts = sorted(
                session.states.list_attempts(request.run_id), key=lambda item: item.sequence
            )
        if plan is None:
            raise ValueError("Code execution plan is missing")
        if request.plan_node.kind is PlanNodeKind.EVALUATOR:
            return ()
        previous = [
            attempt
            for attempt in attempts
            if attempt.plan_node_id == request.plan_node_id
            and attempt.attempt_id != request.attempt_id
            and attempt.status is AttemptStatus.SUCCEEDED
        ]
        for attempt in reversed(previous):
            result = self._snapshot(attempt)
            if result is None:
                raise ValueError(
                    f"Submitted code handoff for Attempt {attempt.attempt_id} is missing"
                )
            return (result.commit,)
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
        sources = {
            source for source in sources if source_nodes[source].kind is not PlanNodeKind.EVALUATOR
        }
        latest = {
            attempt.plan_node_id: attempt
            for attempt in attempts
            if attempt.plan_node_id in sources and attempt.status is AttemptStatus.SUCCEEDED
        }
        commits: list[str] = []
        for source in sorted(sources):
            predecessor = latest.get(source)
            result = None if predecessor is None else self._snapshot(predecessor)
            if result is None:
                raise ValueError(f"Upstream code snapshot for node {source} is missing")
            if result.commit not in commits:
                commits.append(result.commit)
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
                        None
                        if plan is None
                        else session.states.get_plan_revision(plan.plan_revision_id)
                    )
                if attempt is None or revision is None:
                    raise ValueError("Candidate execution context is missing")
                node = next(
                    node for node in revision.nodes if node.plan_node_id == attempt.plan_node_id
                )
                if node.kind is not PlanNodeKind.EVALUATOR:
                    captured = await asyncio.to_thread(self._capture, attempt)
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

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
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
        errors: list[str] = []
        for attempt_id in self._executions:
            with self.database.read_session() as session:
                attempt = session.states.get_attempt(attempt_id)
                run = None if attempt is None else session.states.get_run(attempt.run_id)
                plan = (
                    None if run is None else session.states.get_plan_revision(run.plan_revision_id)
                )
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


def _delivery(result: GitCodeResult) -> dict[str, JsonValue]:
    return {
        "workspace": str(result.worktree),
        "base_commit": result.base_commit,
        "commit": result.commit,
        "diff_path": str(result.diff_path),
        "attempt_id": result.attempt_id,
        "run_id": result.run_id,
    }
