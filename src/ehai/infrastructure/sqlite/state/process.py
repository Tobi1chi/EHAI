"""Process revisions and drafts."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace

from ehai import ID, format_utc_datetime
from ehai.application.process_blocks import validate_block_changes
from ehai.application.process_changes import validate_process_gate_preservation
from ehai.application.process_obligations import ProcessObligationMapping
from ehai.domain.artifacts import ArtifactKind
from ehai.domain.checking import CheckRunStatus
from ehai.domain.events import Event, EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.planning import (
    BranchStatus,
    PlanNodeStatus,
    PlanRevision,
)
from ehai.domain.process import (
    ProcessRevision,
    ProcessRevisionSource,
    branch_selection_signature,
)
from ehai.domain.process import (
    node_definition as _node_definition,
)
from ehai.domain.process import (
    node_input_scope as _node_input_scope,
)
from ehai.domain.process import (
    process_approval_identity as _process_approval_identity,
)
from ehai.domain.process import (
    process_graph_definition as _plan_structure,
)
from ehai.domain.process_drafts import (
    ProcessDraft,
    ProcessDraftStatus,
    process_draft_identity,
)
from ehai.infrastructure.sqlite.codec import (
    decode_edge,
    decode_plan_node,
    decode_process_draft,
    decode_process_revision,
    encode_branch,
    encode_edge,
    encode_execution_plan,
    encode_plan_node,
    encode_process_draft,
    encode_process_revision,
)
from ehai.infrastructure.sqlite.state.common import (
    PersistenceConflictError,
    _required_plan_node,
    _row_index_string,
    _row_string,
)
from ehai.infrastructure.sqlite.state.host import StateRepositoryHost


class ProcessStateMixin:
    """Process revisions and drafts."""

    def get_process_revision(
        self: StateRepositoryHost, process_revision_id: ID
    ) -> ProcessRevision | None:
        snapshot = self._snapshot("process_revisions", "process_revision_id", process_revision_id)
        return None if snapshot is None else decode_process_revision(snapshot)

    def get_process_draft(self: StateRepositoryHost, draft_id: ID) -> ProcessDraft | None:
        row = self._connection.execute(
            """
            SELECT draft_id, run_id, parent_process_revision_id, created_at, status, snapshot_json
            FROM process_drafts WHERE draft_id = ?
            """,
            (draft_id,),
        ).fetchone()
        return None if row is None else self._decode_process_draft_row(row)

    def list_process_drafts(self: StateRepositoryHost, run_id: ID) -> tuple[ProcessDraft, ...]:
        return tuple(
            self._decode_process_draft_row(row)
            for row in self._connection.execute(
                """
                SELECT draft_id, run_id, parent_process_revision_id, created_at, status,
                    snapshot_json
                FROM process_drafts
                WHERE run_id = ? ORDER BY created_at, draft_id
                """,
                (run_id,),
            ).fetchall()
        )

    def put_process_draft(self: StateRepositoryHost, draft: ProcessDraft) -> None:
        snapshot = encode_process_draft(draft)
        existing = self.get_process_draft(draft.draft_id)
        if existing is None:
            self._validate_new_process_draft(draft)
        else:
            self._validate_process_draft_update(existing, draft)
            if existing == draft:
                return
        self._connection.execute(
            """
            INSERT INTO process_drafts(
                draft_id, run_id, parent_process_revision_id, created_at, status, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(draft_id) DO UPDATE SET
                status = excluded.status,
                snapshot_json = excluded.snapshot_json
            """,
            (
                draft.draft_id,
                draft.run_id,
                draft.parent_process_revision_id,
                format_utc_datetime(draft.created_at),
                draft.status.value,
                snapshot,
            ),
        )

    def publish_process_revision(
        self: StateRepositoryHost,
        revision: ProcessRevision,
        *,
        expected_current: PlanRevision,
        obligation_mapping: ProcessObligationMapping,
        source_documents: Mapping[str, str],
    ) -> None:
        """Persist a successor after application-level boundary review, never approve it."""
        existing = self.get_process_revision(revision.process_revision_id)
        if existing is not None:
            if existing != revision:
                raise PersistenceConflictError("ProcessRevision identity is immutable")
            return
        run = self._required_run(revision.run_id)
        previous = self._required_active_process_revision(run.run_id)
        current = self._required_execution_plan(run.run_id)
        if current != expected_current:
            raise PersistenceConflictError("Execution graph changed while the process was reviewed")
        approved = self._required_plan_revision(run.plan_revision_id)
        if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
            raise PersistenceConflictError("Process adjustment requires a nonterminal started Run")
        if (
            revision.source is not ProcessRevisionSource.PLANNER_ADJUSTMENT
            or revision.parent_process_revision_id != previous.process_revision_id
            or revision.version != previous.version + 1
            or revision.created_at < previous.created_at
        ):
            raise PersistenceConflictError("Process adjustment does not follow the active version")
        if _process_approval_identity(revision.graph) != _process_approval_identity(approved):
            raise PersistenceConflictError("Process adjustment changed its original approval")
        if any(
            attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
            for attempt in self.list_attempts(run.run_id)
        ):
            raise PersistenceConflictError(
                "Stop in-flight and queued Attempts before changing process"
            )
        proposed_nodes = {node.plan_node_id: node for node in revision.graph.nodes}
        for node in current.nodes:
            if node.status is PlanNodeStatus.RUNNING:
                raise PersistenceConflictError("Recover running node state before changing process")
            if (
                node.status
                in {
                    PlanNodeStatus.SUSPENDED,
                    PlanNodeStatus.CANDIDATE,
                    PlanNodeStatus.VERIFYING,
                }
                and proposed_nodes.get(node.plan_node_id) != node
            ):
                raise PersistenceConflictError("Process adjustment would strand unresolved work")
        for check in self.list_check_runs(run.run_id):
            if check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING} and (
                check.human_request is None or check.plan_node_id not in proposed_nodes
            ):
                raise PersistenceConflictError("Process adjustment would strand an active Check")
        validate_process_gate_preservation(
            approved,
            previous,
            revision,
            obligation_mapping=obligation_mapping,
            source_documents=source_documents,
        )
        self._validate_retained_process_selections(run.run_id, current, revision.graph)
        validate_block_changes(previous, revision)
        self._connection.execute("SAVEPOINT publish_process_revision")
        try:
            self._insert_process_members(current, revision.graph)
            self._connection.execute(
                """
                INSERT INTO process_revisions(
                    process_revision_id, run_id, version, parent_process_revision_id, snapshot_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    revision.process_revision_id,
                    revision.run_id,
                    revision.version,
                    revision.parent_process_revision_id,
                    encode_process_revision(revision),
                ),
            )
            self._connection.execute(
                """
                UPDATE run_execution_plans SET snapshot_json = ?, active_process_revision_id = ?
                WHERE run_id = ?
                """,
                (encode_execution_plan(revision.graph), revision.process_revision_id, run.run_id),
            )
        except BaseException:
            self._connection.execute("ROLLBACK TO SAVEPOINT publish_process_revision")
            self._connection.execute("RELEASE SAVEPOINT publish_process_revision")
            raise
        self._connection.execute("RELEASE SAVEPOINT publish_process_revision")

    def _validate_retained_process_selections(
        self: StateRepositoryHost, run_id: ID, current: PlanRevision, proposed: PlanRevision
    ) -> None:
        """Retained choices must still name the latest successful candidate evidence."""
        current_branches = {branch.branch_id: branch for branch in current.branches}
        retained = tuple(
            branch
            for branch in proposed.branches
            if branch.status is BranchStatus.SELECTED and branch.branch_id in current_branches
        )
        if not retained:
            return
        latest = {
            attempt.plan_node_id: attempt.attempt_id
            for attempt in sorted(self.list_attempts(run_id), key=lambda item: item.sequence)
            if attempt.status is AttemptStatus.SUCCEEDED
        }
        artifact_nodes: dict[str, ID] = {
            artifact.artifact_id: artifact.plan_node_id
            for artifact in self.list_artifacts_for_run(run_id)
            if artifact.kind in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            and artifact.plan_node_id is not None
            and artifact.attempt_id is not None
            and artifact.attempt_id == latest.get(artifact.plan_node_id)
        }
        nodes = {node.plan_node_id: node for node in proposed.nodes}
        retained_groups = {(branch.fork_node_id, branch.merge_node_id) for branch in retained}
        retained_nodes = {
            node_id
            for branch in proposed.branches
            if (branch.fork_node_id, branch.merge_node_id) in retained_groups
            for node_id in branch.node_ids
        }
        run = self._required_run(run_id)
        for adoption in self.list_result_adoptions(run_id):
            target_id = adoption.target_plan_node_id
            if target_id not in retained_nodes or target_id in latest:
                continue
            target_node = nodes[target_id]
            if target_node.status is not PlanNodeStatus.COMPLETED:
                continue
            producer = self._required_attempt(adoption.source_attempt_id)
            self._adoption_verification(run, target_id, producer, adoption.adoption_id)
            current_node = _required_plan_node(current, target_id)
            if replace(target_node, status=current_node.status) != current_node:
                raise PersistenceConflictError("Retained adopted Branch node changed definition")
            for adopted_evidence in adoption.evidence:
                if adopted_evidence.artifact_id in artifact_nodes:
                    raise PersistenceConflictError("Retained Branch evidence has ambiguous targets")
                artifact_nodes[adopted_evidence.artifact_id] = target_id
        for selected in retained:
            row = self._connection.execute(
                """
                SELECT event_json FROM event_log
                WHERE run_id = ? AND event_type = ?
                    AND json_extract(event_json, '$.correlation_id') = ?
                ORDER BY event_offset DESC LIMIT 1
                """,
                (run_id, EventType.BRANCH_SELECTED.value, selected.branch_id),
            ).fetchone()
            if row is None:
                raise PersistenceConflictError("Retained Branch selection has no evidence Event")
            payload = Event.from_json(_row_string(row, "event_json")).payload
            evidence = payload.get("evidence_artifact_ids")
            chosen = payload.get("selected_artifact_ids")
            group = tuple(
                branch
                for branch in proposed.branches
                if (branch.fork_node_id, branch.merge_node_id)
                == (selected.fork_node_id, selected.merge_node_id)
            )
            candidate_nodes = {node_id for branch in group for node_id in branch.node_ids}
            if (
                payload.get("branch_id") != selected.branch_id
                or payload.get("fork_node_id") != selected.fork_node_id
                or not isinstance(evidence, list)
                or not evidence
                or payload.get("compared_artifact_ids", evidence) != evidence
                or not isinstance(chosen, list)
                or not chosen
                or any(
                    not isinstance(value, str)
                    or value not in artifact_nodes
                    or artifact_nodes[value] not in candidate_nodes
                    for value in evidence
                )
                or any(
                    not isinstance(value, str)
                    or value not in artifact_nodes
                    or artifact_nodes[value] not in selected.node_ids
                    for value in chosen
                )
            ):
                raise PersistenceConflictError(
                    "Retained Branch selection refers to stale or invalid candidate Artifacts"
                )
            compared_nodes = {artifact_nodes[value] for value in evidence if isinstance(value, str)}
            if any(
                all(
                    nodes[node_id].status is PlanNodeStatus.COMPLETED for node_id in branch.node_ids
                )
                and not compared_nodes.intersection(branch.node_ids)
                for branch in group
            ) or any(
                nodes[node_id].status is not PlanNodeStatus.COMPLETED
                for node_id in selected.node_ids
            ):
                raise PersistenceConflictError(
                    "Retained Branch selection no longer covers its viable candidate group"
                )

    def _insert_process_members(
        self: StateRepositoryHost, current: PlanRevision, proposed: PlanRevision
    ) -> None:
        old_nodes = {node.plan_node_id: node for node in current.nodes}
        old_branches = {branch.branch_id: branch for branch in current.branches}
        for node in proposed.nodes:
            stored = self._connection.execute(
                "SELECT plan_revision_id, snapshot_json FROM plan_nodes WHERE plan_node_id = ?",
                (node.plan_node_id,),
            ).fetchone()
            if stored is not None:
                old = old_nodes.get(node.plan_node_id)
                if (
                    _row_string(stored, "plan_revision_id") != proposed.plan_revision_id
                    or old is None
                    or _node_definition(decode_plan_node(_row_string(stored, "snapshot_json")))
                    != _node_definition(node)
                    or old != node
                    or _node_input_scope(current, node.plan_node_id)
                    != _node_input_scope(proposed, node.plan_node_id)
                ):
                    raise PersistenceConflictError(
                        "Changed task/input must use a new node identity"
                    )
                continue
            if node.status is not PlanNodeStatus.PENDING:
                raise PersistenceConflictError("A new process node must start pending")
            self._connection.execute(
                """
                INSERT INTO plan_nodes(plan_node_id, plan_revision_id, sort_index, snapshot_json)
                SELECT ?, ?, COALESCE(MAX(sort_index), -1) + 1, ? FROM plan_nodes
                WHERE plan_revision_id = ?
                """,
                (
                    node.plan_node_id,
                    proposed.plan_revision_id,
                    encode_plan_node(node),
                    proposed.plan_revision_id,
                ),
            )
        for branch in proposed.branches:
            stored = self._connection.execute(
                "SELECT plan_revision_id, snapshot_json FROM branches WHERE branch_id = ?",
                (branch.branch_id,),
            ).fetchone()
            if stored is not None:
                old_branch = old_branches.get(branch.branch_id)
                if (
                    _row_string(stored, "plan_revision_id") != proposed.plan_revision_id
                    or old_branch is None
                ):
                    raise PersistenceConflictError(
                        "Process branch identity crosses its current scope"
                    )
                selection_changed = branch_selection_signature(
                    current, branch.branch_id
                ) != branch_selection_signature(proposed, branch.branch_id)
                expected_status = BranchStatus.ACTIVE if selection_changed else old_branch.status
                if branch.status is not expected_status:
                    raise PersistenceConflictError(
                        "Branch selection must be preserved or invalidated "
                        "with its candidate inputs"
                    )
                continue
            if branch.status is not BranchStatus.ACTIVE:
                raise PersistenceConflictError("New process branches must start active")
            self._connection.execute(
                """
                INSERT INTO branches(
                    branch_id, plan_revision_id, fork_node_id, merge_node_id,
                    sort_index, snapshot_json
                ) SELECT ?, ?, ?, ?, COALESCE(MAX(sort_index), -1) + 1, ? FROM branches
                WHERE plan_revision_id = ?
                """,
                (
                    branch.branch_id,
                    proposed.plan_revision_id,
                    branch.fork_node_id,
                    branch.merge_node_id,
                    encode_branch(branch),
                    proposed.plan_revision_id,
                ),
            )
        for edge in proposed.edges:
            stored = self._connection.execute(
                "SELECT plan_revision_id, snapshot_json FROM edges WHERE edge_id = ?",
                (edge.edge_id,),
            ).fetchone()
            if stored is not None:
                if (
                    _row_string(stored, "plan_revision_id") != proposed.plan_revision_id
                    or decode_edge(_row_string(stored, "snapshot_json")) != edge
                ):
                    raise PersistenceConflictError("Changed edge must use a new identity")
                continue
            self._connection.execute(
                """
                INSERT INTO edges(
                    edge_id, plan_revision_id, source_node_id, target_node_id,
                    branch_id, sort_index, snapshot_json
                ) SELECT ?, ?, ?, ?, ?, COALESCE(MAX(sort_index), -1) + 1, ? FROM edges
                WHERE plan_revision_id = ?
                """,
                (
                    edge.edge_id,
                    proposed.plan_revision_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.branch_id,
                    encode_edge(edge),
                    proposed.plan_revision_id,
                ),
            )

    def get_active_process_revision(
        self: StateRepositoryHost, run_id: ID
    ) -> ProcessRevision | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        row = self._connection.execute(
            "SELECT active_process_revision_id FROM run_execution_plans WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            raise PersistenceConflictError(f"Run {run_id} has no active ProcessRevision")
        process = self.get_process_revision(ID(_row_index_string(row, 0)))
        if (
            process is None
            or process.run_id != run_id
            or process.graph.plan_revision_id != run.plan_revision_id
            or process.graph.goal_id != run.goal_id
        ):
            raise PersistenceConflictError(f"Run {run_id} active ProcessRevision ownership changed")
        return process

    def _validate_new_process_draft(self: StateRepositoryHost, draft: ProcessDraft) -> None:
        if draft.status is not ProcessDraftStatus.PLANNING:
            raise PersistenceConflictError("A new ProcessDraft must start in planning status")
        run = self._required_run(draft.run_id)
        if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
            raise PersistenceConflictError("ProcessDraft requires a nonterminal started Run")
        current = self._required_execution_plan(run.run_id)
        if current != draft.base_execution_plan:
            raise PersistenceConflictError("ProcessDraft base execution plan is stale")
        active = self._required_active_process_revision(run.run_id)
        if active.process_revision_id != draft.parent_process_revision_id:
            raise PersistenceConflictError("ProcessDraft parent is not the active ProcessRevision")
        if _plan_structure(active.graph) != _plan_structure(draft.base_execution_plan):
            raise PersistenceConflictError("ProcessDraft parent graph does not match its baseline")

    def _validate_process_draft_update(
        self: StateRepositoryHost, existing: ProcessDraft, draft: ProcessDraft
    ) -> None:
        if process_draft_identity(existing) != process_draft_identity(draft):
            raise PersistenceConflictError(f"ProcessDraft {draft.draft_id} identity changed")
        if existing == draft:
            return
        if existing.status is not ProcessDraftStatus.PLANNING:
            raise PersistenceConflictError(f"ProcessDraft {draft.draft_id} outcome is immutable")
        if draft.status is ProcessDraftStatus.PLANNING:
            raise PersistenceConflictError(f"ProcessDraft {draft.draft_id} planning state changed")
        if draft.status is ProcessDraftStatus.READY:
            self._validate_process_draft_candidate(draft)
            return
        if draft.status is ProcessDraftStatus.FAILED:
            return
        raise PersistenceConflictError(f"ProcessDraft {draft.draft_id} has an invalid status")

    def _validate_process_draft_candidate(self: StateRepositoryHost, draft: ProcessDraft) -> None:
        candidate = draft.candidate
        if candidate is None:  # pragma: no cover - ProcessDraft validates this
            raise PersistenceConflictError("A ready ProcessDraft requires a candidate")
        parent = self.get_process_revision(draft.parent_process_revision_id)
        if parent is None or parent.run_id != draft.run_id:
            raise PersistenceConflictError("ProcessDraft candidate parent is not retained")
        if candidate.version != parent.version + 1:
            raise PersistenceConflictError(
                "ProcessDraft candidate must be the immediate successor of its parent"
            )
        validate_block_changes(parent, candidate)
        approved = self._required_plan_revision(draft.base_execution_plan.plan_revision_id)
        if _process_approval_identity(approved) != _process_approval_identity(
            draft.base_execution_plan
        ):
            raise PersistenceConflictError("ProcessDraft approval baseline changed")
        if _process_approval_identity(candidate.graph) != _process_approval_identity(
            draft.base_execution_plan
        ):
            raise PersistenceConflictError("ProcessDraft candidate changed its approval identity")

    def _decode_process_draft_row(self: StateRepositoryHost, row: sqlite3.Row) -> ProcessDraft:
        draft = decode_process_draft(_row_string(row, "snapshot_json"))
        if (
            _row_string(row, "draft_id") != str(draft.draft_id)
            or _row_string(row, "run_id") != str(draft.run_id)
            or _row_string(row, "parent_process_revision_id")
            != str(draft.parent_process_revision_id)
            or _row_string(row, "created_at") != format_utc_datetime(draft.created_at)
            or _row_string(row, "status") != draft.status.value
        ):
            raise PersistenceConflictError(
                f"ProcessDraft {draft.draft_id} projection does not match its snapshot"
            )
        return draft

    def _required_active_process_revision(self: StateRepositoryHost, run_id: ID) -> ProcessRevision:
        process = self.get_active_process_revision(run_id)
        if process is None:
            raise PersistenceConflictError(f"Run {run_id} has no active ProcessRevision")
        return process
