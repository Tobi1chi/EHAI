"""SQLite repositories bound to one explicit Unit of Work transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from ehai import ID, format_utc_datetime, json_dumps, json_loads, new_id, parse_utc_datetime
from ehai.application.ports import CommandReceipt, StateConflictError, StoredEvent
from ehai.application.process_changes import validate_process_gate_preservation
from ehai.application.process_obligations import ProcessObligationMapping
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, Checkpoint, CheckRun, CheckRunStatus, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import (
    BranchStatus,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
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
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import (
    AgentSessionRef,
    BuiltinExecutionRef,
    ExternalExecutionRef,
    WorkerEndpoint,
    WorkerProfile,
)
from ehai.infrastructure.sqlite.codec import (
    decode_agent_session_ref,
    decode_artifact,
    decode_attempt,
    decode_builtin_execution_ref,
    decode_check_run,
    decode_check_spec,
    decode_checkpoint,
    decode_completion_contract,
    decode_dispatch_work,
    decode_edge,
    decode_execution_plan,
    decode_external_execution_ref,
    decode_goal,
    decode_plan_node,
    decode_process_draft,
    decode_process_revision,
    decode_project,
    decode_run,
    decode_worker_endpoint,
    decode_worker_profile,
    encode_agent_session_ref,
    encode_artifact,
    encode_attempt,
    encode_branch,
    encode_builtin_execution_ref,
    encode_check_run,
    encode_check_spec,
    encode_checkpoint,
    encode_completion_contract,
    encode_dispatch_work,
    encode_edge,
    encode_execution_plan,
    encode_external_execution_ref,
    encode_goal,
    encode_plan_node,
    encode_plan_revision,
    encode_process_draft,
    encode_process_revision,
    encode_project,
    encode_run,
    encode_worker_endpoint,
    encode_worker_profile,
)


class PersistenceConflictError(StateConflictError):
    """Raised when immutable persisted identity is reused for different data."""


class UnknownEventCursorError(LookupError):
    """Raised when an Event resume cursor is not present in the Event Log."""


class DuplicateEventError(PersistenceConflictError):
    """Raised when an immutable Event ID is appended more than once."""


class IdempotencyConflictError(PersistenceConflictError):
    """Raised when an idempotency key is reused for another Command fingerprint."""

    def __init__(self, key: str, stored_fingerprint: str, submitted_fingerprint: str) -> None:
        self.key = key
        self.stored_fingerprint = stored_fingerprint
        self.submitted_fingerprint = submitted_fingerprint
        super().__init__(f"idempotency key {key!r} was already used with another fingerprint")


_PLAN_REVISION_STATUS_TRANSITIONS = {
    PlanRevisionStatus.DRAFT: frozenset({PlanRevisionStatus.DRAFT, PlanRevisionStatus.APPROVED}),
    PlanRevisionStatus.APPROVED: frozenset({PlanRevisionStatus.APPROVED}),
}
_PLAN_NODE_STATUS_TRANSITIONS = {
    PlanNodeStatus.PENDING: frozenset(
        {PlanNodeStatus.PENDING, PlanNodeStatus.READY, PlanNodeStatus.PRUNED}
    ),
    PlanNodeStatus.READY: frozenset(
        {
            PlanNodeStatus.PENDING,
            PlanNodeStatus.READY,
            PlanNodeStatus.RUNNING,
            PlanNodeStatus.PRUNED,
        }
    ),
    PlanNodeStatus.RUNNING: frozenset(
        {
            PlanNodeStatus.RUNNING,
            PlanNodeStatus.CANDIDATE,
            PlanNodeStatus.FAILED,
            PlanNodeStatus.BLOCKED,
        }
    ),
    PlanNodeStatus.BLOCKED: frozenset({PlanNodeStatus.BLOCKED, PlanNodeStatus.PENDING}),
    PlanNodeStatus.CANDIDATE: frozenset(
        {
            PlanNodeStatus.PENDING,
            PlanNodeStatus.CANDIDATE,
            PlanNodeStatus.VERIFYING,
            PlanNodeStatus.FAILED,
        }
    ),
    PlanNodeStatus.VERIFYING: frozenset(
        {
            PlanNodeStatus.PENDING,
            PlanNodeStatus.VERIFYING,
            PlanNodeStatus.COMPLETED,
            PlanNodeStatus.FAILED,
        }
    ),
    PlanNodeStatus.FAILED: frozenset(
        {PlanNodeStatus.PENDING, PlanNodeStatus.FAILED, PlanNodeStatus.READY}
    ),
    PlanNodeStatus.COMPLETED: frozenset({PlanNodeStatus.PENDING, PlanNodeStatus.COMPLETED}),
    PlanNodeStatus.PRUNED: frozenset({PlanNodeStatus.PENDING, PlanNodeStatus.PRUNED}),
}
_BRANCH_STATUS_TRANSITIONS = {
    BranchStatus.ACTIVE: frozenset(
        {BranchStatus.ACTIVE, BranchStatus.SELECTED, BranchStatus.PRUNED}
    ),
    BranchStatus.SELECTED: frozenset({BranchStatus.ACTIVE, BranchStatus.SELECTED}),
    BranchStatus.PRUNED: frozenset({BranchStatus.ACTIVE, BranchStatus.PRUNED}),
}
_RUN_STATUS_TRANSITIONS = {
    RunStatus.PENDING: frozenset({RunStatus.PENDING, RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.PAUSED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PAUSED: frozenset(
        {RunStatus.PAUSED, RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.COMPLETED: frozenset({RunStatus.COMPLETED}),
    RunStatus.FAILED: frozenset({RunStatus.FAILED}),
    RunStatus.CANCELLED: frozenset({RunStatus.CANCELLED}),
}
_ATTEMPT_STATUS_TRANSITIONS = {
    AttemptStatus.PENDING: frozenset(
        {AttemptStatus.PENDING, AttemptStatus.RUNNING, AttemptStatus.CANCELLED}
    ),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.RUNNING,
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.CANCELLED,
            AttemptStatus.INTERRUPTED,
        }
    ),
    AttemptStatus.SUCCEEDED: frozenset({AttemptStatus.SUCCEEDED}),
    AttemptStatus.FAILED: frozenset({AttemptStatus.FAILED}),
    AttemptStatus.TIMED_OUT: frozenset({AttemptStatus.TIMED_OUT}),
    AttemptStatus.CANCELLED: frozenset({AttemptStatus.CANCELLED}),
    AttemptStatus.INTERRUPTED: frozenset({AttemptStatus.INTERRUPTED}),
}
_CHECK_RUN_STATUS_TRANSITIONS = {
    CheckRunStatus.PENDING: frozenset(
        {CheckRunStatus.PENDING, CheckRunStatus.RUNNING, CheckRunStatus.CANCELLED}
    ),
    CheckRunStatus.RUNNING: frozenset(
        {
            CheckRunStatus.RUNNING,
            CheckRunStatus.COMPLETED,
            CheckRunStatus.FAILED,
            CheckRunStatus.TIMED_OUT,
            CheckRunStatus.CANCELLED,
            CheckRunStatus.INTERRUPTED,
        }
    ),
    CheckRunStatus.COMPLETED: frozenset({CheckRunStatus.COMPLETED}),
    CheckRunStatus.FAILED: frozenset({CheckRunStatus.FAILED}),
    CheckRunStatus.TIMED_OUT: frozenset({CheckRunStatus.TIMED_OUT}),
    CheckRunStatus.CANCELLED: frozenset({CheckRunStatus.CANCELLED}),
    CheckRunStatus.INTERRUPTED: frozenset({CheckRunStatus.INTERRUPTED}),
}


class SQLiteWorkerRegistry:
    """Configured Worker Profiles and Endpoints without discovery or installation."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def put_worker_profile(self, profile: WorkerProfile) -> None:
        snapshot = encode_worker_profile(profile)
        existing = self.get_worker_profile(profile.worker_profile_id)
        if existing is not None:
            if existing != profile:
                raise PersistenceConflictError(
                    f"WorkerProfile {profile.worker_profile_id} is immutable"
                )
            return
        self._connection.execute(
            """
            INSERT INTO worker_profiles(
                worker_profile_id, worker_kind, model, session_policy,
                credential_ref, priority, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.worker_profile_id,
                profile.kind.value,
                profile.model,
                profile.session_policy.value,
                profile.credential_ref,
                profile.priority,
                snapshot,
            ),
        )

    def get_worker_profile(self, worker_profile_id: ID) -> WorkerProfile | None:
        row = self._connection.execute(
            "SELECT snapshot_json FROM worker_profiles WHERE worker_profile_id = ?",
            (worker_profile_id,),
        ).fetchone()
        return None if row is None else decode_worker_profile(_row_index_string(row, 0))

    def list_worker_profiles(self) -> tuple[WorkerProfile, ...]:
        return tuple(
            decode_worker_profile(_row_index_string(row, 0))
            for row in self._connection.execute(
                "SELECT snapshot_json FROM worker_profiles ORDER BY worker_profile_id"
            ).fetchall()
        )

    def put_worker_endpoint(self, endpoint: WorkerEndpoint) -> None:
        snapshot = encode_worker_endpoint(endpoint)
        existing = self.get_worker_endpoint(endpoint.worker_endpoint_id)
        if existing is not None and _worker_endpoint_identity(
            existing
        ) != _worker_endpoint_identity(endpoint):
            raise PersistenceConflictError(
                f"WorkerEndpoint {endpoint.worker_endpoint_id} identity changed"
            )
        self._connection.execute(
            """
            INSERT INTO worker_endpoints(
                worker_endpoint_id, worker_kind, endpoint_type,
                capacity, status, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_endpoint_id) DO UPDATE SET
                status = excluded.status,
                snapshot_json = excluded.snapshot_json
            """,
            (
                endpoint.worker_endpoint_id,
                endpoint.worker_kind.value,
                endpoint.endpoint_type.value,
                endpoint.capacity,
                endpoint.status.value,
                snapshot,
            ),
        )

    def get_worker_endpoint(self, worker_endpoint_id: ID) -> WorkerEndpoint | None:
        row = self._connection.execute(
            "SELECT snapshot_json FROM worker_endpoints WHERE worker_endpoint_id = ?",
            (worker_endpoint_id,),
        ).fetchone()
        return None if row is None else decode_worker_endpoint(_row_index_string(row, 0))

    def list_worker_endpoints(self) -> tuple[WorkerEndpoint, ...]:
        return tuple(
            decode_worker_endpoint(_row_index_string(row, 0))
            for row in self._connection.execute(
                "SELECT snapshot_json FROM worker_endpoints ORDER BY worker_endpoint_id"
            ).fetchall()
        )


class SQLiteCurrentStateRepository:
    """Current-state snapshots and retained version history in one SQLite transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def put_project(self, project: Project) -> None:
        self._connection.execute(
            """
            INSERT INTO projects(project_id, created_at, snapshot_json)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                created_at = excluded.created_at,
                snapshot_json = excluded.snapshot_json
            """,
            (
                project.project_id,
                format_utc_datetime(project.created_at),
                encode_project(project),
            ),
        )

    def get_project(self, project_id: ID) -> Project | None:
        snapshot = self._snapshot("projects", "project_id", project_id)
        return None if snapshot is None else decode_project(snapshot)

    def list_projects(self) -> tuple[Project, ...]:
        return tuple(
            decode_project(snapshot)
            for snapshot in self._snapshots(
                "SELECT snapshot_json FROM projects ORDER BY created_at, project_id"
            )
        )

    def put_goal(self, goal: Goal) -> None:
        contract_id = (
            None
            if goal.completion_contract is None
            else goal.completion_contract.completion_contract_id
        )
        self._connection.execute(
            """
            INSERT INTO goals(
                goal_id, project_id, current_completion_contract_id, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(goal_id) DO UPDATE SET
                project_id = excluded.project_id,
                current_completion_contract_id = excluded.current_completion_contract_id,
                created_at = excluded.created_at,
                snapshot_json = excluded.snapshot_json
            """,
            (
                goal.goal_id,
                goal.project_id,
                contract_id,
                format_utc_datetime(goal.created_at),
                encode_goal(goal),
            ),
        )

    def get_goal(self, goal_id: ID) -> Goal | None:
        row = self._connection.execute(
            """
            SELECT snapshot_json, current_completion_contract_id
            FROM goals WHERE goal_id = ?
            """,
            (goal_id,),
        ).fetchone()
        if row is None:
            return None
        contract_id = _optional_row_string(row, "current_completion_contract_id")
        contract = None if contract_id is None else self.get_completion_contract(ID(contract_id))
        return decode_goal(_row_string(row, "snapshot_json"), contract)

    def list_goals(self, project_id: ID) -> tuple[Goal, ...]:
        return tuple(
            self._required_goal(ID(goal_id))
            for goal_id in self._strings(
                """
                SELECT goal_id FROM goals
                WHERE project_id = ? ORDER BY created_at, goal_id
                """,
                (project_id,),
            )
        )

    def put_completion_contract(self, contract: CompletionContract) -> None:
        existing = self.get_completion_contract(contract.completion_contract_id)
        if existing is not None:
            same_structure = _completion_contract_structure(existing) == (
                _completion_contract_structure(contract)
            )
            valid_confirmation = (
                same_structure
                and existing.confirmed_at is None
                and contract.confirmed_at is not None
            )
            if existing != contract and not valid_confirmation:
                raise PersistenceConflictError(
                    f"CompletionContract {contract.completion_contract_id} history changed"
                )
        self._connection.execute(
            """
            INSERT INTO completion_contracts(
                completion_contract_id, goal_id, version,
                supersedes_completion_contract_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(completion_contract_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json
            """,
            (
                contract.completion_contract_id,
                contract.goal_id,
                contract.version,
                contract.supersedes_completion_contract_id,
                encode_completion_contract(contract),
            ),
        )

    def get_completion_contract(self, completion_contract_id: ID) -> CompletionContract | None:
        snapshot = self._snapshot(
            "completion_contracts",
            "completion_contract_id",
            completion_contract_id,
        )
        return None if snapshot is None else decode_completion_contract(snapshot)

    def list_completion_contracts(self, goal_id: ID) -> tuple[CompletionContract, ...]:
        return tuple(
            decode_completion_contract(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM completion_contracts
                WHERE goal_id = ? ORDER BY version
                """,
                (goal_id,),
            )
        )

    def put_plan_revision(self, plan_revision: PlanRevision) -> None:
        existing = self.get_plan_revision(plan_revision.plan_revision_id)
        if existing is not None and existing.status is PlanRevisionStatus.APPROVED:
            if existing != plan_revision:
                raise PersistenceConflictError(
                    "Approved PlanRevision is immutable; update the Run execution plan instead"
                )
            return
        self._validate_plan_update(plan_revision)
        self._validate_plan_child_identity(plan_revision)
        self._connection.execute(
            """
            INSERT INTO plan_revisions(
                plan_revision_id, goal_id, completion_contract_id, version,
                supersedes_plan_revision_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_revision_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json
            """,
            (
                plan_revision.plan_revision_id,
                plan_revision.goal_id,
                plan_revision.completion_contract_id,
                plan_revision.version,
                plan_revision.supersedes_plan_revision_id,
                encode_plan_revision(plan_revision),
            ),
        )
        for index, node in enumerate(plan_revision.nodes):
            self._connection.execute(
                """
                INSERT INTO plan_nodes(
                    plan_node_id, plan_revision_id, sort_index, snapshot_json
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(plan_node_id) DO UPDATE SET
                    sort_index = excluded.sort_index,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    node.plan_node_id,
                    plan_revision.plan_revision_id,
                    index,
                    encode_plan_node(node),
                ),
            )
        for index, branch in enumerate(plan_revision.branches):
            self._connection.execute(
                """
                INSERT INTO branches(
                    branch_id, plan_revision_id, fork_node_id, merge_node_id,
                    sort_index, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id) DO UPDATE SET
                    sort_index = excluded.sort_index,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    branch.branch_id,
                    plan_revision.plan_revision_id,
                    branch.fork_node_id,
                    branch.merge_node_id,
                    index,
                    encode_branch(branch),
                ),
            )
        for index, edge in enumerate(plan_revision.edges):
            self._connection.execute(
                """
                INSERT INTO edges(
                    edge_id, plan_revision_id, source_node_id, target_node_id,
                    branch_id, sort_index, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    sort_index = excluded.sort_index,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    edge.edge_id,
                    plan_revision.plan_revision_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.branch_id,
                    index,
                    encode_edge(edge),
                ),
            )

    def get_plan_revision(self, plan_revision_id: ID) -> PlanRevision | None:
        snapshot = self._snapshot("plan_revisions", "plan_revision_id", plan_revision_id)
        return None if snapshot is None else decode_execution_plan(snapshot)

    def list_plan_revisions(self, goal_id: ID) -> tuple[PlanRevision, ...]:
        return tuple(
            self._required_plan_revision(ID(revision_id))
            for revision_id in self._strings(
                """
                SELECT plan_revision_id FROM plan_revisions
                WHERE goal_id = ? ORDER BY version
                """,
                (goal_id,),
            )
        )

    def get_execution_plan(self, run_id: ID) -> PlanRevision | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        snapshot = self._snapshot("run_execution_plans", "run_id", run_id)
        if snapshot is None:
            raise PersistenceConflictError(f"Run {run_id} has no execution plan")
        plan = decode_execution_plan(snapshot)
        if plan.plan_revision_id != run.plan_revision_id or plan.goal_id != run.goal_id:
            raise PersistenceConflictError(f"Run {run_id} execution plan ownership changed")
        return plan

    def put_execution_plan(self, run_id: ID, plan_revision: PlanRevision) -> None:
        run = self._required_run(run_id)
        existing = self._required_execution_plan(run_id)
        if plan_revision.plan_revision_id != run.plan_revision_id:
            raise PersistenceConflictError(f"Run {run_id} execution plan approval changed")
        process = self._required_active_process_revision(run_id)
        if _plan_structure(process.graph) != _plan_structure(plan_revision):
            raise PersistenceConflictError(
                "Execution graph differs from its active ProcessRevision"
            )
        self._validate_graph_update(existing, plan_revision, run_id=run_id)
        self._connection.execute(
            "UPDATE run_execution_plans SET snapshot_json = ? WHERE run_id = ?",
            (encode_execution_plan(plan_revision), run_id),
        )

    def get_process_revision(self, process_revision_id: ID) -> ProcessRevision | None:
        snapshot = self._snapshot("process_revisions", "process_revision_id", process_revision_id)
        return None if snapshot is None else decode_process_revision(snapshot)

    def get_process_draft(self, draft_id: ID) -> ProcessDraft | None:
        row = self._connection.execute(
            """
            SELECT draft_id, run_id, parent_process_revision_id, created_at, status, snapshot_json
            FROM process_drafts WHERE draft_id = ?
            """,
            (draft_id,),
        ).fetchone()
        return None if row is None else self._decode_process_draft_row(row)

    def list_process_drafts(self, run_id: ID) -> tuple[ProcessDraft, ...]:
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

    def get_result_adoption(self, adoption_id: ID) -> ResultAdoption | None:
        row = self._connection.execute(
            """
            SELECT adoption_id, target_run_id, target_plan_revision_id, target_plan_node_id,
                source_run_id, source_plan_revision_id, source_process_revision_id,
                source_plan_node_id, source_attempt_id, created_at, snapshot_json
            FROM result_adoptions WHERE adoption_id = ?
            """,
            (adoption_id,),
        ).fetchone()
        return None if row is None else self._decode_result_adoption_row(row)

    def list_result_adoptions(self, target_run_id: ID) -> tuple[ResultAdoption, ...]:
        return tuple(
            self._decode_result_adoption_row(row)
            for row in self._connection.execute(
                """
                SELECT adoption_id, target_run_id, target_plan_revision_id, target_plan_node_id,
                source_run_id, source_plan_revision_id, source_process_revision_id,
                source_plan_node_id, source_attempt_id, created_at, snapshot_json
            FROM result_adoptions
                WHERE target_run_id = ? ORDER BY created_at, adoption_id
                """,
                (target_run_id,),
            ).fetchall()
        )

    def put_result_adoption(self, record: ResultAdoption) -> None:
        if not isinstance(record, ResultAdoption):
            raise TypeError("record must be a ResultAdoption")

        existing = self.get_result_adoption(record.adoption_id)
        if existing is not None:
            if existing != record:
                raise PersistenceConflictError(f"ResultAdoption {record.adoption_id} is immutable")
            return

        collision = self._connection.execute(
            """
            SELECT adoption_id FROM result_adoptions
            WHERE target_run_id = ? AND target_plan_node_id = ?
            """,
            (record.target_run_id, record.target_plan_node_id),
        ).fetchone()
        if collision is not None:
            raise PersistenceConflictError(
                "Target Run and PlanNode already have a different ResultAdoption"
            )

        self._validate_result_adoption(record)
        snapshot = json_dumps(record.to_dict())
        try:
            self._connection.execute(
                """
                INSERT INTO result_adoptions(
                    adoption_id, target_run_id, target_plan_revision_id, target_plan_node_id,
                    source_run_id, source_plan_revision_id, source_process_revision_id,
                    source_plan_node_id, source_attempt_id, created_at, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.adoption_id,
                    record.target_run_id,
                    record.target_plan_revision_id,
                    record.target_plan_node_id,
                    record.source_run_id,
                    record.source_plan_revision_id,
                    record.source_process_revision_id,
                    record.source_plan_node_id,
                    record.source_attempt_id,
                    format_utc_datetime(record.created_at),
                    snapshot,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise PersistenceConflictError(
                f"ResultAdoption {record.adoption_id} cannot be persisted"
            ) from error

    def put_process_draft(self, draft: ProcessDraft) -> None:
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
        self,
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
                    PlanNodeStatus.BLOCKED,
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
        self, run_id: ID, current: PlanRevision, proposed: PlanRevision
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

    def _insert_process_members(self, current: PlanRevision, proposed: PlanRevision) -> None:
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

    def get_active_process_revision(self, run_id: ID) -> ProcessRevision | None:
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

    def put_run(self, run: Run) -> None:
        plan_revision = self._required_plan_revision(run.plan_revision_id)
        if plan_revision.goal_id != run.goal_id:
            raise PersistenceConflictError(
                f"Run {run.run_id} Goal does not match PlanRevision {run.plan_revision_id}"
            )
        existing = self.get_run(run.run_id)
        if existing is None and run.predecessor_run_id is not None:
            predecessor = self._required_run(run.predecessor_run_id)
            if (
                run.status is not RunStatus.PENDING
                or predecessor.goal_id != run.goal_id
                or predecessor.status is not RunStatus.PAUSED
                or predecessor.plan_revision_id == run.plan_revision_id
                or plan_revision.status is not PlanRevisionStatus.APPROVED
            ):
                raise PersistenceConflictError(
                    "Successor requires a paused same-Goal predecessor and a new approved plan"
                )
            if any(
                attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
                for attempt in self.list_attempts(predecessor.run_id)
            ):
                raise PersistenceConflictError("Successor predecessor has unresolved Attempts")
            for check in self.list_check_runs(predecessor.run_id):
                spec = self.get_check_spec(check.check_id)
                if check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING} and (
                    spec is None or spec.kind is not CheckKind.HUMAN
                ):
                    raise PersistenceConflictError("Successor predecessor has unresolved Checks")
        if existing is not None:
            if _run_identity(existing) != _run_identity(run):
                raise PersistenceConflictError(f"Run {run.run_id} identity changed")
            if run.status not in _RUN_STATUS_TRANSITIONS[existing.status]:
                raise PersistenceConflictError(f"Run {run.run_id} status transition is illegal")
        self._connection.execute(
            """
            INSERT INTO runs(run_id, goal_id, plan_revision_id, created_at, snapshot_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET snapshot_json = excluded.snapshot_json
            """,
            (
                run.run_id,
                run.goal_id,
                run.plan_revision_id,
                format_utc_datetime(run.created_at),
                encode_run(run),
            ),
        )
        if existing is None:
            process = ProcessRevision(
                process_revision_id=new_id(),
                run_id=run.run_id,
                version=1,
                graph=plan_revision,
                created_at=run.created_at,
                reason="Execution starts from the original approved plan",
                source=ProcessRevisionSource.RUN_STARTED,
            )
            self._connection.execute(
                """
                INSERT INTO process_revisions(process_revision_id, run_id, version, snapshot_json)
                VALUES (?, ?, ?, ?)
                """,
                (process.process_revision_id, run.run_id, 1, encode_process_revision(process)),
            )
            self._connection.execute(
                """
                INSERT INTO run_execution_plans(
                    run_id, plan_revision_id, snapshot_json, active_process_revision_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.plan_revision_id,
                    encode_execution_plan(plan_revision),
                    process.process_revision_id,
                ),
            )

    def get_run(self, run_id: ID) -> Run | None:
        snapshot = self._snapshot("runs", "run_id", run_id)
        return None if snapshot is None else decode_run(snapshot)

    def list_runs(self, goal_id: ID) -> tuple[Run, ...]:
        return tuple(
            decode_run(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM runs
                WHERE goal_id = ? ORDER BY created_at, run_id
                """,
                (goal_id,),
            )
        )

    def put_attempt(self, attempt: Attempt) -> None:
        self._required_run(attempt.run_id)
        plan_revision = self._attempt_plan(attempt)
        _required_plan_node(plan_revision, attempt.plan_node_id)
        existing = self.get_attempt(attempt.attempt_id)
        if existing is None:
            process = self._required_active_process_revision(attempt.run_id)
            if attempt.process_revision_id != process.process_revision_id:
                raise PersistenceConflictError("New Attempt must bind the active ProcessRevision")
        if existing is not None:
            if _attempt_identity(existing) != _attempt_identity(attempt):
                raise PersistenceConflictError(f"Attempt {attempt.attempt_id} identity changed")
            if existing.worker_profile_id is not None and _attempt_assignment(
                existing
            ) != _attempt_assignment(attempt):
                raise PersistenceConflictError(f"Attempt {attempt.attempt_id} assignment changed")
            if (
                existing.execution_handle is not None
                and existing.execution_handle != attempt.execution_handle
            ):
                raise PersistenceConflictError(
                    f"Attempt {attempt.attempt_id} execution binding changed"
                )
            if attempt.status not in _ATTEMPT_STATUS_TRANSITIONS[existing.status]:
                raise PersistenceConflictError(
                    f"Attempt {attempt.attempt_id} status transition is illegal"
                )
        self._validate_attempt_runtime_refs(attempt)
        execution_kind = (
            None if attempt.execution_handle is None else attempt.execution_handle.kind.value
        )
        provider_execution_id = (
            None
            if attempt.execution_handle is None
            else attempt.execution_handle.provider_execution_id
        )
        self._connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, run_id, plan_node_id, sequence, snapshot_json,
                worker_profile_id, worker_endpoint_id, agent_session_ref_id,
                execution_kind, provider_execution_id, activity, event_cursor,
                heartbeat_at, progress_at, deadline_at, lease_expires_at
                , queue_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                worker_profile_id = excluded.worker_profile_id,
                worker_endpoint_id = excluded.worker_endpoint_id,
                agent_session_ref_id = excluded.agent_session_ref_id,
                execution_kind = excluded.execution_kind,
                provider_execution_id = excluded.provider_execution_id,
                activity = excluded.activity,
                event_cursor = excluded.event_cursor,
                heartbeat_at = excluded.heartbeat_at,
                progress_at = excluded.progress_at,
                deadline_at = excluded.deadline_at,
                lease_expires_at = excluded.lease_expires_at
                , queue_reason = excluded.queue_reason
            """,
            (
                attempt.attempt_id,
                attempt.run_id,
                attempt.plan_node_id,
                attempt.sequence,
                encode_attempt(attempt),
                attempt.worker_profile_id,
                attempt.worker_endpoint_id,
                attempt.agent_session_ref_id,
                execution_kind,
                provider_execution_id,
                None if attempt.activity is None else attempt.activity.value,
                attempt.event_cursor,
                _format_optional_datetime(attempt.heartbeat_at),
                _format_optional_datetime(attempt.progress_at),
                _format_optional_datetime(attempt.deadline_at),
                _format_optional_datetime(attempt.lease_expires_at),
                attempt.queue_reason,
            ),
        )

    def get_attempt(self, attempt_id: ID) -> Attempt | None:
        snapshot = self._snapshot("attempts", "attempt_id", attempt_id)
        return None if snapshot is None else decode_attempt(snapshot)

    def list_attempts(self, run_id: ID) -> tuple[Attempt, ...]:
        return tuple(
            decode_attempt(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM attempts
                WHERE run_id = ? ORDER BY sequence, attempt_id
                """,
                (run_id,),
            )
        )

    def put_dispatch_work(self, work: DispatchWork) -> None:
        self._required_run(work.run_id)
        existing = self.get_dispatch_work(work.dispatch_work_id)
        if existing is not None:
            if existing.run_id != work.run_id or existing.created_at != work.created_at:
                raise PersistenceConflictError(
                    f"DispatchWork {work.dispatch_work_id} identity changed"
                )
            allowed = {
                DispatchWorkStatus.PENDING: {
                    DispatchWorkStatus.PENDING,
                    DispatchWorkStatus.CLAIMED,
                },
                DispatchWorkStatus.CLAIMED: {
                    DispatchWorkStatus.PENDING,
                    DispatchWorkStatus.CLAIMED,
                    DispatchWorkStatus.COMPLETED,
                },
                DispatchWorkStatus.COMPLETED: {
                    DispatchWorkStatus.PENDING,
                    DispatchWorkStatus.COMPLETED,
                },
            }
            if work.status not in allowed[existing.status]:
                raise PersistenceConflictError(
                    f"DispatchWork {work.dispatch_work_id} status transition is illegal"
                )
        self._connection.execute(
            """
            INSERT INTO dispatch_work(
                dispatch_work_id, run_id, status, created_at,
                claim_owner, lease_expires_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dispatch_work_id) DO UPDATE SET
                status = excluded.status,
                claim_owner = excluded.claim_owner,
                lease_expires_at = excluded.lease_expires_at,
                snapshot_json = excluded.snapshot_json
            """,
            (
                work.dispatch_work_id,
                work.run_id,
                work.status.value,
                format_utc_datetime(work.created_at),
                work.claim_owner,
                _format_optional_datetime(work.lease_expires_at),
                encode_dispatch_work(work),
            ),
        )

    def get_dispatch_work(self, dispatch_work_id: ID) -> DispatchWork | None:
        snapshot = self._snapshot("dispatch_work", "dispatch_work_id", dispatch_work_id)
        return None if snapshot is None else decode_dispatch_work(snapshot)

    def list_dispatch_work(
        self,
        status: DispatchWorkStatus | None = None,
    ) -> tuple[DispatchWork, ...]:
        if status is None:
            sql = """
                SELECT snapshot_json FROM dispatch_work
                ORDER BY created_at, dispatch_work_id
            """
            parameters: tuple[object, ...] = ()
        else:
            sql = """
                SELECT snapshot_json FROM dispatch_work
                WHERE status = ? ORDER BY created_at, dispatch_work_id
            """
            parameters = (DispatchWorkStatus(status).value,)
        return tuple(
            decode_dispatch_work(snapshot) for snapshot in self._snapshots(sql, parameters)
        )

    def claim_next_dispatch_work(
        self,
        *,
        owner: str,
        at: datetime,
        lease_expires_at: datetime,
    ) -> DispatchWork | None:
        row = self._connection.execute(
            """
            SELECT work.snapshot_json FROM dispatch_work AS work
            JOIN runs AS run ON run.run_id = work.run_id
            WHERE (work.status = 'pending'
               OR (work.status = 'claimed' AND work.lease_expires_at <= ?))
              AND (json_extract(run.snapshot_json, '$.status') != 'paused' OR EXISTS (
                  SELECT 1 FROM attempts AS attempt
                  WHERE attempt.run_id = work.run_id
                    AND json_extract(attempt.snapshot_json, '$.status') = 'running'
              ))
            ORDER BY CASE work.status WHEN 'claimed' THEN 0 ELSE 1 END,
                     work.created_at, work.dispatch_work_id
            LIMIT 1
            """,
            (format_utc_datetime(at),),
        ).fetchone()
        if row is None:
            return None
        work = decode_dispatch_work(_row_index_string(row, 0))
        claimed = (
            work.claim(owner, lease_expires_at, at=at)
            if work.status is DispatchWorkStatus.PENDING
            else work.reclaim(owner, lease_expires_at, at=at)
        )
        self.put_dispatch_work(claimed)
        return claimed

    def record_worker_event(
        self,
        attempt_id: ID,
        worker_event_id: str,
        *,
        at: datetime,
    ) -> bool:
        self._required_attempt(attempt_id)
        if not isinstance(worker_event_id, str) or not worker_event_id.strip():
            raise ValueError("worker_event_id must not be blank")
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO worker_event_receipts(
                attempt_id, worker_event_id, received_at
            ) VALUES (?, ?, ?)
            """,
            (attempt_id, worker_event_id, format_utc_datetime(at)),
        )
        return cursor.rowcount == 1

    def put_agent_session_ref(self, session: AgentSessionRef) -> None:
        self._required_run(session.run_id)
        registry = SQLiteWorkerRegistry(self._connection)
        profile = registry.get_worker_profile(session.worker_profile_id)
        endpoint = registry.get_worker_endpoint(session.worker_endpoint_id)
        if profile is None:
            raise PersistenceConflictError(
                f"WorkerProfile {session.worker_profile_id} is not persisted"
            )
        if endpoint is None:
            raise PersistenceConflictError(
                f"WorkerEndpoint {session.worker_endpoint_id} is not persisted"
            )
        if profile.kind != endpoint.worker_kind:
            raise PersistenceConflictError(
                f"AgentSessionRef {session.agent_session_ref_id} Worker kinds do not match"
            )
        snapshot = encode_agent_session_ref(session)
        existing = self.get_agent_session_ref(session.agent_session_ref_id)
        if existing is not None:
            if existing != session:
                raise PersistenceConflictError(
                    f"AgentSessionRef {session.agent_session_ref_id} is immutable"
                )
            return
        self._connection.execute(
            """
            INSERT INTO agent_session_refs(
                agent_session_ref_id, run_id, worker_profile_id, worker_endpoint_id,
                provider_session_id, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.agent_session_ref_id,
                session.run_id,
                session.worker_profile_id,
                session.worker_endpoint_id,
                session.provider_session_id,
                format_utc_datetime(session.created_at),
                snapshot,
            ),
        )

    def get_agent_session_ref(self, agent_session_ref_id: ID) -> AgentSessionRef | None:
        snapshot = self._snapshot(
            "agent_session_refs", "agent_session_ref_id", agent_session_ref_id
        )
        return None if snapshot is None else decode_agent_session_ref(snapshot)

    def list_agent_session_refs(self, run_id: ID) -> tuple[AgentSessionRef, ...]:
        return tuple(
            decode_agent_session_ref(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM agent_session_refs
                WHERE run_id = ? ORDER BY created_at, agent_session_ref_id
                """,
                (run_id,),
            )
        )

    def put_external_execution_ref(self, reference: ExternalExecutionRef) -> None:
        self._validate_execution_reference(
            reference.attempt_id,
            reference.agent_session_ref_id,
            expected_builtin=False,
        )
        snapshot = encode_external_execution_ref(reference)
        existing = self.get_external_execution_ref(reference.external_execution_ref_id)
        if existing is not None:
            if existing != reference:
                raise PersistenceConflictError(
                    f"ExternalExecutionRef {reference.external_execution_ref_id} is immutable"
                )
            return
        self._connection.execute(
            """
            INSERT INTO external_execution_refs(
                external_execution_ref_id, attempt_id, agent_session_ref_id,
                provider_execution_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                reference.external_execution_ref_id,
                reference.attempt_id,
                reference.agent_session_ref_id,
                reference.provider_execution_id,
                snapshot,
            ),
        )

    def get_external_execution_ref(
        self, external_execution_ref_id: ID
    ) -> ExternalExecutionRef | None:
        snapshot = self._snapshot(
            "external_execution_refs",
            "external_execution_ref_id",
            external_execution_ref_id,
        )
        return None if snapshot is None else decode_external_execution_ref(snapshot)

    def put_builtin_execution_ref(self, reference: BuiltinExecutionRef) -> None:
        self._validate_execution_reference(
            reference.attempt_id,
            reference.agent_session_ref_id,
            expected_builtin=True,
        )
        snapshot = encode_builtin_execution_ref(reference)
        existing = self.get_builtin_execution_ref(reference.builtin_execution_ref_id)
        if existing is not None:
            if existing != reference:
                raise PersistenceConflictError(
                    f"BuiltinExecutionRef {reference.builtin_execution_ref_id} is immutable"
                )
            return
        self._connection.execute(
            """
            INSERT INTO builtin_execution_refs(
                builtin_execution_ref_id, builtin_execution_id, attempt_id,
                agent_session_ref_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                reference.builtin_execution_ref_id,
                reference.builtin_execution_id,
                reference.attempt_id,
                reference.agent_session_ref_id,
                snapshot,
            ),
        )

    def get_builtin_execution_ref(self, builtin_execution_ref_id: ID) -> BuiltinExecutionRef | None:
        snapshot = self._snapshot(
            "builtin_execution_refs",
            "builtin_execution_ref_id",
            builtin_execution_ref_id,
        )
        return None if snapshot is None else decode_builtin_execution_ref(snapshot)

    def _validate_attempt_runtime_refs(self, attempt: Attempt) -> None:
        if attempt.worker_profile_id is None:
            return
        assert attempt.worker_endpoint_id is not None
        assert attempt.agent_session_ref_id is not None
        registry = SQLiteWorkerRegistry(self._connection)
        profile = registry.get_worker_profile(attempt.worker_profile_id)
        endpoint = registry.get_worker_endpoint(attempt.worker_endpoint_id)
        session = self.get_agent_session_ref(attempt.agent_session_ref_id)
        if profile is None or endpoint is None or session is None:
            raise PersistenceConflictError(
                f"Attempt {attempt.attempt_id} assignment references missing registry state"
            )
        if (
            session.run_id != attempt.run_id
            or session.worker_profile_id != profile.worker_profile_id
            or session.worker_endpoint_id != endpoint.worker_endpoint_id
        ):
            raise PersistenceConflictError(
                f"Attempt {attempt.attempt_id} assignment crosses execution scope"
            )
        plan = self._attempt_plan(attempt)
        node = _required_plan_node(plan, attempt.plan_node_id)
        if not node.required_capabilities.issubset(profile.capabilities):
            raise PersistenceConflictError(
                f"Attempt {attempt.attempt_id} WorkerProfile lacks required capabilities"
            )
        if node.session_policy != profile.session_policy:
            raise PersistenceConflictError(
                f"Attempt {attempt.attempt_id} SessionPolicy does not match PlanNode"
            )
        if attempt.execution_handle is None:
            return
        handle = attempt.execution_handle
        if handle.external is not None:
            stored: ExternalExecutionRef | BuiltinExecutionRef | None = (
                self.get_external_execution_ref(handle.external.external_execution_ref_id)
            )
        else:
            assert handle.builtin is not None
            stored = self.get_builtin_execution_ref(handle.builtin.builtin_execution_ref_id)
        expected = handle.external if handle.external is not None else handle.builtin
        if stored != expected:
            raise PersistenceConflictError(
                f"Attempt {attempt.attempt_id} execution reference is not persisted"
            )

    def _validate_execution_reference(
        self,
        attempt_id: ID,
        agent_session_ref_id: ID,
        *,
        expected_builtin: bool,
    ) -> None:
        attempt = self._required_attempt(attempt_id)
        session = self.get_agent_session_ref(agent_session_ref_id)
        if session is None:
            raise PersistenceConflictError(
                f"AgentSessionRef {agent_session_ref_id} is not persisted"
            )
        if session.run_id != attempt.run_id:
            raise PersistenceConflictError(
                f"Attempt {attempt_id} and Session {agent_session_ref_id} belong to different Runs"
            )
        registry = SQLiteWorkerRegistry(self._connection)
        profile = registry.get_worker_profile(session.worker_profile_id)
        if profile is None:
            raise PersistenceConflictError(
                f"WorkerProfile {session.worker_profile_id} is not persisted"
            )
        if expected_builtin != (profile.kind.value == "builtin"):
            raise PersistenceConflictError(
                f"Attempt {attempt_id} execution kind does not match WorkerProfile"
            )
        opposite_table = "external_execution_refs" if expected_builtin else "builtin_execution_refs"
        opposite = self._connection.execute(
            f"SELECT 1 FROM {opposite_table} WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if opposite is not None:
            raise PersistenceConflictError(
                f"Attempt {attempt_id} already has another execution kind"
            )

    def put_check_spec(self, plan_revision_id: ID, check_spec: CheckSpec) -> None:
        plan = self._required_plan_revision(plan_revision_id)
        existing_row = self._connection.execute(
            "SELECT plan_revision_id, snapshot_json FROM check_specs WHERE check_id = ?",
            (check_spec.check_id,),
        ).fetchone()
        snapshot = encode_check_spec(check_spec)
        if existing_row is not None:
            owner_id = _row_index_string(existing_row, 0)
            stored_snapshot = _row_index_string(existing_row, 1)
            if owner_id != plan.plan_revision_id or stored_snapshot != snapshot:
                raise PersistenceConflictError(f"CheckSpec {check_spec.check_id} is immutable")
            return

        referenced_ids = {check_id for node in plan.nodes for check_id in node.required_check_ids}
        contract = self.get_completion_contract(plan.completion_contract_id)
        if contract is None:
            raise PersistenceConflictError(
                f"PlanRevision {plan.plan_revision_id} CompletionContract is not persisted"
            )
        if check_spec.required and check_spec.check_id not in referenced_ids:
            raise PersistenceConflictError(
                f"required CheckSpec {check_spec.check_id} is not referenced by PlanRevision "
                f"{plan.plan_revision_id}"
            )
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sort_index), -1) + 1 FROM check_specs WHERE plan_revision_id = ?",
            (plan.plan_revision_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate always returns a row
            raise RuntimeError("SQLite did not assign a CheckSpec order")
        sort_index = _row_index_integer(row, 0)
        self._connection.execute(
            """
            INSERT INTO check_specs(check_id, plan_revision_id, sort_index, kind, snapshot_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                check_spec.check_id,
                plan.plan_revision_id,
                sort_index,
                check_spec.kind.value,
                snapshot,
            ),
        )

    def get_check_spec(self, check_id: ID) -> CheckSpec | None:
        snapshot = self._snapshot("check_specs", "check_id", check_id)
        return None if snapshot is None else decode_check_spec(snapshot)

    def list_check_specs(self, plan_revision_id: ID) -> tuple[CheckSpec, ...]:
        return tuple(
            decode_check_spec(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM check_specs
                WHERE plan_revision_id = ? ORDER BY sort_index
                """,
                (plan_revision_id,),
            )
        )

    def put_check_run(self, check_run: CheckRun) -> None:
        attempt = self._required_attempt(check_run.attempt_id)
        run = self._required_run(check_run.run_id)
        if check_run.adoption_id is None:
            if attempt.run_id != check_run.run_id or attempt.plan_node_id != check_run.plan_node_id:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} does not match Attempt {attempt.attempt_id}"
                )
            plan = self._attempt_plan(attempt)
        else:
            adoption = self._adoption_verification(
                run, check_run.plan_node_id, attempt, check_run.adoption_id
            )
            plan = self._required_execution_plan(run.run_id)
            if check_run.result is not None and not set(
                check_run.result.evidence_artifact_ids
            ).issubset(item.artifact_id for item in adoption.evidence):
                raise PersistenceConflictError("Adopted Check result has unrelated evidence")
        node = _required_plan_node(plan, check_run.plan_node_id)
        if check_run.check_id not in node.required_check_ids:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} Check is not required by PlanNode "
                f"{node.plan_node_id}"
            )
        if check_run.human_request is not None or check_run.human_decision is not None:
            check_spec = self.get_check_spec(check_run.check_id)
            if check_spec is None:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} CheckSpec {check_run.check_id} "
                    "is not persisted"
                )
            if check_spec.kind is not CheckKind.HUMAN:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} has human metadata for a non-human Check"
                )
            self._validate_human_check_run(check_run, run, node, attempt)
        existing = self.get_check_run(check_run.check_run_id)
        if existing is not None:
            if _check_run_identity(existing) != _check_run_identity(check_run):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} identity changed"
                )
            if existing.human_request is not None and (
                existing.human_request != check_run.human_request
            ):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human request is immutable"
                )
            if existing.human_decision is not None and (
                existing.human_decision != check_run.human_decision
                or existing.result != check_run.result
            ):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human decision is immutable"
                )
            if check_run.status not in _CHECK_RUN_STATUS_TRANSITIONS[existing.status]:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} status transition is illegal"
                )
        self._connection.execute(
            """
            INSERT INTO check_runs(
                check_run_id, run_id, plan_node_id, attempt_id,
                check_id, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_run_id) DO UPDATE SET snapshot_json = excluded.snapshot_json
            """,
            (
                check_run.check_run_id,
                check_run.run_id,
                check_run.plan_node_id,
                check_run.attempt_id,
                check_run.check_id,
                format_utc_datetime(check_run.created_at),
                encode_check_run(check_run),
            ),
        )

    def _validate_human_check_run(
        self,
        check_run: CheckRun,
        run: Run,
        node: PlanNode,
        attempt: Attempt,
    ) -> None:
        request = check_run.human_request
        if request is None:
            if check_run.human_decision is not None:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human decision has no request"
                )
            return
        if request.plan_revision_id != run.plan_revision_id:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request PlanRevision does not match Run"
            )
        plan = (
            self._attempt_plan(attempt)
            if check_run.adoption_id is None
            else self._required_execution_plan(run.run_id)
        )
        if plan.status is not PlanRevisionStatus.APPROVED:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request requires an approved PlanRevision"
            )
        if (
            request.completion_contract_id != plan.completion_contract_id
            or request.completion_contract_version != plan.completion_contract_version
        ):
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request CompletionContract does not "
                "match its approved PlanRevision"
            )
        contract = self.get_completion_contract(request.completion_contract_id)
        if contract is None:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request CompletionContract is missing"
            )
        if (
            contract.goal_id != run.goal_id
            or contract.completion_contract_id != request.completion_contract_id
            or contract.version != request.completion_contract_version
            or not contract.is_confirmed
        ):
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request CompletionContract is not "
                "the current Run contract"
            )
        if attempt.status is not AttemptStatus.SUCCEEDED:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human request requires a succeeded Attempt"
            )
        if (
            check_run.human_decision is not None
            and check_run.status is not CheckRunStatus.COMPLETED
        ):
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human decision requires a completed CheckRun"
            )

        attempt_artifact_ids = set(attempt.artifact_ids)
        request_artifact_ids = {evidence.artifact_id for evidence in request.evidence}
        if request_artifact_ids != attempt_artifact_ids:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} human evidence must cover the complete "
                f"Attempt {attempt.attempt_id} candidate/patch snapshot"
            )
        for evidence in request.evidence:
            if evidence.artifact_id not in attempt_artifact_ids:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human evidence Artifact "
                    f"{evidence.artifact_id} is not on Attempt {attempt.attempt_id}"
                )
            artifact = self.get_artifact(evidence.artifact_id)
            if artifact is None:
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human evidence Artifact "
                    f"{evidence.artifact_id} is missing"
                )
            if (
                artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or artifact.run_id != attempt.run_id
                or artifact.plan_node_id != attempt.plan_node_id
                or artifact.attempt_id != attempt.attempt_id
                or artifact.sha256 != evidence.sha256
            ):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} human evidence Artifact "
                    f"{evidence.artifact_id} is not the current candidate/patch version"
                )

    def get_check_run(self, check_run_id: ID) -> CheckRun | None:
        snapshot = self._snapshot("check_runs", "check_run_id", check_run_id)
        return None if snapshot is None else decode_check_run(snapshot)

    def list_check_runs(self, run_id: ID) -> tuple[CheckRun, ...]:
        return tuple(
            decode_check_run(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM check_runs
                WHERE run_id = ? ORDER BY created_at, check_run_id
                """,
                (run_id,),
            )
        )

    def put_checkpoint(self, checkpoint: Checkpoint) -> None:
        existing = self.get_checkpoint(checkpoint.checkpoint_id)
        if existing is not None:
            if existing != checkpoint:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} is immutable"
                )
            return
        self._validate_checkpoint_references(checkpoint)
        snapshot = encode_checkpoint(checkpoint)
        self._connection.execute(
            """
            INSERT INTO checkpoints(
                checkpoint_id, plan_revision_id, run_id, event_offset, gate_id, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.plan_revision_id,
                checkpoint.run_id,
                checkpoint.event_offset,
                checkpoint.gate_decision.gate_id,
                snapshot,
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO checkpoint_branch_selections(
                checkpoint_id, fork_node_id, branch_id
            ) VALUES (?, ?, ?)
            """,
            (
                (checkpoint.checkpoint_id, fork_id, branch_id)
                for fork_id, branch_id in checkpoint.branch_selections.items()
            ),
        )
        self._connection.executemany(
            """
            INSERT INTO checkpoint_artifacts(checkpoint_id, artifact_id)
            VALUES (?, ?)
            """,
            ((checkpoint.checkpoint_id, artifact_id) for artifact_id in checkpoint.artifact_refs),
        )

    def get_checkpoint(self, checkpoint_id: ID) -> Checkpoint | None:
        snapshot = self._snapshot("checkpoints", "checkpoint_id", checkpoint_id)
        return None if snapshot is None else decode_checkpoint(snapshot)

    def list_checkpoints(self, run_id: ID) -> tuple[Checkpoint, ...]:
        return tuple(
            decode_checkpoint(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM checkpoints
                WHERE run_id = ? ORDER BY event_offset, checkpoint_id
                """,
                (run_id,),
            )
        )

    def restore_checkpoint_state(self, checkpoint: Checkpoint, restored_run: Run) -> None:
        """Restore an audited Checkpoint without weakening normal transition checks."""
        stored_checkpoint = self.get_checkpoint(checkpoint.checkpoint_id)
        if stored_checkpoint != checkpoint:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} is not the persisted recovery snapshot"
            )
        latest_row = self._connection.execute(
            """
            SELECT checkpoint_id FROM checkpoints
            WHERE run_id = ? ORDER BY event_offset DESC, checkpoint_id DESC LIMIT 1
            """,
            (checkpoint.run_id,),
        ).fetchone()
        if latest_row is None or _row_index_string(latest_row, 0) != checkpoint.checkpoint_id:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} is superseded and cannot be restored"
            )
        current_run = self._required_run(checkpoint.run_id)
        current_plan = self._required_execution_plan(checkpoint.run_id)
        process = self._required_active_process_revision(checkpoint.run_id)
        legacy_baseline = (
            checkpoint.process_revision_id is None
            and process.version == 1
            and process.source is ProcessRevisionSource.LEGACY_SNAPSHOT
        )
        if checkpoint.process_revision_id != process.process_revision_id and not legacy_baseline:
            raise PersistenceConflictError("Checkpoint belongs to a different ProcessRevision")
        if current_run.status is not RunStatus.PAUSED:
            raise PersistenceConflictError(
                f"Run {current_run.run_id} must be paused before Checkpoint restore"
            )
        if (
            restored_run.run_id != checkpoint.run_id
            or restored_run.goal_id != checkpoint.run.goal_id
            or restored_run.plan_revision_id != checkpoint.plan_revision_id
            or restored_run.status is not RunStatus.PAUSED
        ):
            raise PersistenceConflictError(
                f"Run {restored_run.run_id} is not a paused snapshot of Checkpoint "
                f"{checkpoint.checkpoint_id}"
            )
        expected_run = (
            checkpoint.run.pause() if checkpoint.run.status is RunStatus.RUNNING else checkpoint.run
        )
        if restored_run != expected_run:
            raise PersistenceConflictError(
                f"Run {restored_run.run_id} differs from Checkpoint {checkpoint.checkpoint_id}"
            )
        if _plan_structure(current_plan) != _plan_structure(checkpoint.plan_revision):
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} PlanRevision structure changed"
            )

        self._connection.execute(
            "UPDATE run_execution_plans SET snapshot_json = ? WHERE run_id = ?",
            (encode_execution_plan(checkpoint.plan_revision), checkpoint.run_id),
        )
        self._connection.execute(
            "UPDATE runs SET snapshot_json = ? WHERE run_id = ?",
            (encode_run(restored_run), restored_run.run_id),
        )

    def put_artifact(self, artifact: Artifact) -> None:
        if artifact.run_id is not None:
            run = self._required_run(artifact.run_id)
            plan_revision = (
                self._required_execution_plan(run.run_id)
                if artifact.attempt_id is None
                else self._attempt_plan(self._required_attempt(artifact.attempt_id))
            )
            if artifact.plan_node_id is not None:
                _required_plan_node(plan_revision, artifact.plan_node_id)
            if artifact.attempt_id is not None:
                attempt = self._required_attempt(artifact.attempt_id)
                if (
                    attempt.run_id != artifact.run_id
                    or attempt.plan_node_id != artifact.plan_node_id
                ):
                    raise PersistenceConflictError(
                        f"Artifact {artifact.artifact_id} does not match Attempt "
                        f"{artifact.attempt_id}"
                    )
        snapshot = encode_artifact(artifact)
        existing = self._snapshot("artifacts", "artifact_id", artifact.artifact_id)
        if existing is not None:
            if existing != snapshot:
                raise PersistenceConflictError(f"Artifact {artifact.artifact_id} is immutable")
            return
        self._connection.execute(
            """
            INSERT INTO artifacts(
                artifact_id, run_id, plan_node_id, attempt_id, sha256,
                relative_path, created_at, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                artifact.run_id,
                artifact.plan_node_id,
                artifact.attempt_id,
                artifact.sha256,
                artifact.relative_path,
                format_utc_datetime(artifact.created_at),
                snapshot,
            ),
        )

    def _adoption_verification(
        self, run: Run, node_id: ID, attempt: Attempt, adoption_id: ID
    ) -> ResultAdoption:
        """Resolve a retained target binding without relabelling its producer."""
        record = self.get_result_adoption(adoption_id)
        if record is None or (
            record.target_run_id != run.run_id
            or record.target_plan_revision_id != run.plan_revision_id
            or record.target_plan_node_id != node_id
            or record.source_run_id != run.predecessor_run_id
            or record.source_run_id != attempt.run_id
            or record.source_plan_node_id != attempt.plan_node_id
            or record.source_attempt_id != attempt.attempt_id
            or attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise PersistenceConflictError("Verification has no matching adopted producer")
        approved = self._required_plan_revision(run.plan_revision_id)
        original_node = _required_plan_node(approved, node_id)
        if original_node.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            raise PersistenceConflictError("Only implementation nodes can accept prior results")
        current_node = _required_plan_node(self._required_execution_plan(run.run_id), node_id)
        if replace(current_node, status=original_node.status) != original_node:
            raise PersistenceConflictError("Adopted target node changed after its acceptance")
        if any(item.plan_node_id == node_id for item in self.list_attempts(run.run_id)):
            raise PersistenceConflictError("Target execution has superseded the adopted result")
        if {item.artifact_id for item in record.evidence} != set(attempt.artifact_ids):
            raise PersistenceConflictError("Adopted producer evidence no longer matches")
        for evidence in record.evidence:
            artifact = self.get_artifact(evidence.artifact_id)
            if artifact is None or (
                artifact.run_id != attempt.run_id
                or artifact.plan_node_id != attempt.plan_node_id
                or artifact.attempt_id != attempt.attempt_id
                or artifact.sha256 != evidence.sha256
                or artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
            ):
                raise PersistenceConflictError("Adopted verification evidence is inconsistent")
        return record

    def _validate_result_adoption(self, record: ResultAdoption) -> None:
        target_run = self._required_run(record.target_run_id)
        if target_run.predecessor_run_id != record.source_run_id:
            raise PersistenceConflictError("ResultAdoption source is not the bound predecessor Run")
        if target_run.status is not RunStatus.PENDING:
            raise PersistenceConflictError(
                f"ResultAdoption target Run {target_run.run_id} must be pending"
            )
        target_plan = self._required_plan_revision(record.target_plan_revision_id)
        if target_plan.status is not PlanRevisionStatus.APPROVED:
            raise PersistenceConflictError(
                f"ResultAdoption target PlanRevision {target_plan.plan_revision_id} "
                "must be approved"
            )
        if (
            target_run.plan_revision_id != record.target_plan_revision_id
            or target_plan.goal_id != target_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption target Run and PlanRevision do not match"
            )
        target_execution_plan = self._required_execution_plan(target_run.run_id)
        if (
            target_execution_plan.plan_revision_id != record.target_plan_revision_id
            or target_execution_plan.goal_id != target_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption target execution plan does not match its approved PlanRevision"
            )
        target_node = _required_plan_node(target_execution_plan, record.target_plan_node_id)
        if target_node.kind not in {PlanNodeKind.WORK, PlanNodeKind.MERGE}:
            raise PersistenceConflictError("ResultAdoption target must be an implementation node")
        if target_node.status is not PlanNodeStatus.PENDING:
            raise PersistenceConflictError(
                f"ResultAdoption target PlanNode {target_node.plan_node_id} must be pending"
            )

        source_run = self._required_run(record.source_run_id)
        if source_run.status is not RunStatus.PAUSED:
            raise PersistenceConflictError(
                f"ResultAdoption source Run {source_run.run_id} must be paused"
            )
        if source_run.goal_id != target_run.goal_id:
            raise PersistenceConflictError(
                "ResultAdoption source and target Runs must belong to the same Goal"
            )
        source_plan = self._required_plan_revision(record.source_plan_revision_id)
        if (
            source_plan.status is not PlanRevisionStatus.APPROVED
            or source_run.plan_revision_id != record.source_plan_revision_id
            or source_plan.goal_id != source_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption source Run and PlanRevision do not match"
            )
        source_execution_plan = self._required_execution_plan(source_run.run_id)
        if (
            source_execution_plan.plan_revision_id != record.source_plan_revision_id
            or source_execution_plan.goal_id != source_run.goal_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption source execution plan does not match its PlanRevision"
            )
        active_process = self._required_active_process_revision(source_run.run_id)
        if active_process.process_revision_id != record.source_process_revision_id:
            raise PersistenceConflictError(
                "ResultAdoption source ProcessRevision is not the active revision"
            )
        source_node = _required_plan_node(source_execution_plan, record.source_plan_node_id)
        if source_node.status is not PlanNodeStatus.COMPLETED:
            raise PersistenceConflictError(
                f"ResultAdoption source PlanNode {source_node.plan_node_id} must be completed"
            )

        attempts = self.list_attempts(source_run.run_id)
        if any(
            attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING} for attempt in attempts
        ):
            raise PersistenceConflictError(
                f"ResultAdoption source Run {source_run.run_id} has unresolved Attempts"
            )
        successful_attempts = tuple(
            attempt
            for attempt in attempts
            if attempt.plan_node_id == source_node.plan_node_id
            and attempt.status is AttemptStatus.SUCCEEDED
        )
        if (
            not successful_attempts
            or successful_attempts[-1].attempt_id != record.source_attempt_id
        ):
            raise PersistenceConflictError(
                "ResultAdoption source Attempt is not the latest succeeded Attempt for its node"
            )
        source_attempt = successful_attempts[-1]
        if (
            source_attempt.run_id != source_run.run_id
            or source_attempt.plan_node_id != source_node.plan_node_id
            or source_attempt.attempt_id != record.source_attempt_id
        ):
            raise PersistenceConflictError("ResultAdoption source Attempt ownership does not match")

        check_specs = {
            spec.check_id: spec for spec in self.list_check_specs(source_plan.plan_revision_id)
        }
        for check in self.list_check_runs(source_run.run_id):
            if check.status not in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}:
                continue
            check_spec = check_specs.get(check.check_id)
            if check_spec is None or check_spec.kind is not CheckKind.HUMAN:
                raise PersistenceConflictError(
                    f"ResultAdoption source Run {source_run.run_id} has an unresolved "
                    "automatic Check"
                )

        source_branch = next(
            (
                branch
                for branch in source_execution_plan.branches
                if source_node.plan_node_id in branch.node_ids
            ),
            None,
        )
        if source_branch is not None and source_branch.status is not BranchStatus.SELECTED:
            raise PersistenceConflictError(
                f"ResultAdoption source PlanNode {source_node.plan_node_id} belongs to "
                f"unselected Branch {source_branch.branch_id}"
            )

        evidence_ids = tuple(item.artifact_id for item in record.evidence)
        if len(evidence_ids) != len(source_attempt.artifact_ids) or set(evidence_ids) != set(
            source_attempt.artifact_ids
        ):
            raise PersistenceConflictError(
                "ResultAdoption evidence must exactly match the source Attempt Artifacts"
            )
        for evidence in record.evidence:
            artifact = self.get_artifact(evidence.artifact_id)
            if artifact is None:
                raise PersistenceConflictError(
                    f"ResultAdoption evidence Artifact {evidence.artifact_id} is not persisted"
                )
            if (
                artifact.kind not in {ArtifactKind.CANDIDATE, ArtifactKind.PATCH}
                or artifact.run_id != source_run.run_id
                or artifact.plan_node_id != source_node.plan_node_id
                or artifact.attempt_id != source_attempt.attempt_id
                or artifact.sha256 != evidence.sha256
            ):
                raise PersistenceConflictError(
                    f"ResultAdoption evidence Artifact {evidence.artifact_id} does not match "
                    "the source Attempt"
                )

    def get_artifact(self, artifact_id: ID) -> Artifact | None:
        snapshot = self._snapshot("artifacts", "artifact_id", artifact_id)
        return None if snapshot is None else decode_artifact(snapshot)

    def list_artifacts_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        return tuple(
            decode_artifact(snapshot)
            for snapshot in self._snapshots(
                """
                SELECT snapshot_json FROM artifacts
                WHERE run_id = ? ORDER BY created_at, artifact_id
                """,
                (run_id,),
            )
        )

    def _validate_plan_update(self, plan_revision: PlanRevision) -> None:
        existing = self.get_plan_revision(plan_revision.plan_revision_id)
        if existing is None:
            return
        self._validate_graph_update(existing, plan_revision)

    def _validate_new_process_draft(self, draft: ProcessDraft) -> None:
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

    def _validate_process_draft_update(self, existing: ProcessDraft, draft: ProcessDraft) -> None:
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

    def _validate_process_draft_candidate(self, draft: ProcessDraft) -> None:
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
        approved = self._required_plan_revision(draft.base_execution_plan.plan_revision_id)
        if _process_approval_identity(approved) != _process_approval_identity(
            draft.base_execution_plan
        ):
            raise PersistenceConflictError("ProcessDraft approval baseline changed")
        if _process_approval_identity(candidate.graph) != _process_approval_identity(
            draft.base_execution_plan
        ):
            raise PersistenceConflictError("ProcessDraft candidate changed its approval identity")

    def _validate_graph_update(
        self,
        existing: PlanRevision,
        plan_revision: PlanRevision,
        *,
        run_id: ID | None = None,
    ) -> None:
        if _plan_structure(existing) != _plan_structure(plan_revision):
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} structure changed"
            )
        if plan_revision.status not in _PLAN_REVISION_STATUS_TRANSITIONS[existing.status]:
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} status regressed"
            )
        if (
            existing.status is plan_revision.status
            and existing.approved_at != plan_revision.approved_at
        ):
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} approval timestamp changed"
            )
        for previous_node, current_node in zip(existing.nodes, plan_revision.nodes, strict=True):
            adopting_candidate = (
                previous_node.status is PlanNodeStatus.READY
                and current_node.status is PlanNodeStatus.CANDIDATE
            )
            adopting_intermediate = (
                previous_node.status is PlanNodeStatus.CANDIDATE
                and current_node.status is PlanNodeStatus.COMPLETED
                and not current_node.required_check_ids
                and any(
                    edge.source_node_id == current_node.plan_node_id for edge in plan_revision.edges
                )
            )
            if run_id is not None and (adopting_candidate or adopting_intermediate):
                adoption = next(
                    (
                        item
                        for item in self.list_result_adoptions(run_id)
                        if item.target_plan_node_id == current_node.plan_node_id
                    ),
                    None,
                )
                if adoption is not None and not any(
                    item.plan_node_id == current_node.plan_node_id
                    for item in self.list_attempts(run_id)
                ):
                    run = self._required_run(run_id)
                    if run.status is not RunStatus.RUNNING:
                        raise PersistenceConflictError("Adopted result requires a running target")
                    self._adoption_verification(
                        run,
                        current_node.plan_node_id,
                        self._required_attempt(adoption.source_attempt_id),
                        adoption.adoption_id,
                    )
                    continue
            if (
                previous_node.status is PlanNodeStatus.CANDIDATE
                and current_node.status is PlanNodeStatus.COMPLETED
                and not current_node.required_check_ids
                and any(
                    edge.source_node_id == current_node.plan_node_id for edge in plan_revision.edges
                )
            ):
                evidence = self._connection.execute(
                    """
                    SELECT a.snapshot_json FROM attempts a
                    WHERE a.plan_node_id = ? AND a.run_id = ?
                    ORDER BY a.sequence DESC LIMIT 1
                    """,
                    (current_node.plan_node_id, run_id),
                ).fetchone()
                if evidence is not None:
                    attempt = decode_attempt(_row_index_string(evidence, 0))
                    if attempt.status is AttemptStatus.SUCCEEDED and attempt.artifact_ids:
                        continue
            if current_node.status not in _PLAN_NODE_STATUS_TRANSITIONS[previous_node.status]:
                raise PersistenceConflictError(
                    f"PlanNode {current_node.plan_node_id} has an illegal persisted "
                    "status transition"
                )
        for previous_branch, current_branch in zip(
            existing.branches, plan_revision.branches, strict=True
        ):
            if current_branch.status not in _BRANCH_STATUS_TRANSITIONS[previous_branch.status]:
                raise PersistenceConflictError(
                    f"Branch {current_branch.branch_id} has an illegal persisted status transition"
                )

    def _validate_checkpoint_references(self, checkpoint: Checkpoint) -> None:
        process = self._required_active_process_revision(checkpoint.run_id)
        if checkpoint.process_revision_id != process.process_revision_id:
            raise PersistenceConflictError("New Checkpoint must bind the active ProcessRevision")
        persisted_plan = self._required_execution_plan(checkpoint.run_id)
        if persisted_plan != checkpoint.plan_revision:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} PlanRevision snapshot is stale"
            )
        persisted_run = self._required_run(checkpoint.run_id)
        if persisted_run != checkpoint.run:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Run snapshot is stale"
            )
        row = self._connection.execute(
            "SELECT event_json FROM event_log WHERE event_offset = ?",
            (checkpoint.event_offset,),
        ).fetchone()
        if row is None:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Event offset is not persisted"
            )
        event = Event.from_json(_row_string(row, "event_json"))
        gate_id = event.payload.get("gate_id")
        if (
            event.type is not EventType.GATE_PASSED
            or event.run_id != checkpoint.run_id
            or gate_id != checkpoint.gate_decision.gate_id
        ):
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} does not reference its GatePassed Event"
            )

        decision = checkpoint.gate_decision
        node = _required_plan_node(persisted_plan, decision.plan_node_id)
        if not set(node.required_check_ids).issubset(decision.required_check_ids):
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Gate omits a required PlanNode Check"
            )
        attempt = self._required_attempt(decision.attempt_id)
        if decision.adoption_id is None:
            if attempt.run_id != decision.run_id or attempt.plan_node_id != decision.plan_node_id:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} Gate Attempt ownership does not match"
                )
        else:
            adoption = self._adoption_verification(
                persisted_run, decision.plan_node_id, attempt, decision.adoption_id
            )
            if not set(decision.evidence_artifact_ids).issubset(
                item.artifact_id for item in adoption.evidence
            ):
                raise PersistenceConflictError(
                    "Adopted Gate has evidence outside its accepted result"
                )

        decision_evidence = set(decision.evidence_artifact_ids)
        for check_id in decision.required_check_ids:
            candidates = tuple(
                decode_check_run(snapshot)
                for snapshot in self._snapshots(
                    """
                    SELECT snapshot_json FROM check_runs
                    WHERE run_id = ? AND plan_node_id = ?
                        AND attempt_id = ? AND check_id = ?
                    ORDER BY created_at, check_run_id
                    """,
                    (decision.run_id, decision.plan_node_id, decision.attempt_id, check_id),
                )
            )
            valid_results = tuple(
                check_run.result
                for check_run in candidates
                if check_run.status is CheckRunStatus.COMPLETED
                and check_run.adoption_id == decision.adoption_id
                and check_run.result is not None
                and check_run.result.passed
                and check_run.result.evidence_artifact_ids
            )
            if not valid_results or not any(
                set(result.evidence_artifact_ids).issubset(decision_evidence)
                for result in valid_results
            ):
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} lacks a passing evidenced "
                    f"CheckRun for Check {check_id}"
                )

        for artifact_id in decision.evidence_artifact_ids:
            artifact = self.get_artifact(artifact_id)
            if artifact is None:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} evidence Artifact "
                    f"{artifact_id} is not persisted"
                )
            if (
                artifact.run_id != attempt.run_id
                or artifact.plan_node_id != attempt.plan_node_id
                or artifact.attempt_id != decision.attempt_id
            ):
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} evidence Artifact "
                    f"{artifact_id} belongs to another execution scope"
                )

    def _validate_plan_child_identity(self, plan_revision: PlanRevision) -> None:
        child_ids = {
            "plan_nodes": (
                "plan_node_id",
                tuple(node.plan_node_id for node in plan_revision.nodes),
            ),
            "edges": ("edge_id", tuple(edge.edge_id for edge in plan_revision.edges)),
            "branches": ("branch_id", tuple(branch.branch_id for branch in plan_revision.branches)),
        }
        for table, (id_column, entity_ids) in child_ids.items():
            for entity_id in entity_ids:
                owner = self._connection.execute(
                    f"SELECT plan_revision_id FROM {table} WHERE {id_column} = ?",
                    (entity_id,),
                ).fetchone()
                if (
                    owner is not None
                    and _row_index_string(owner, 0) != plan_revision.plan_revision_id
                ):
                    raise PersistenceConflictError(
                        f"{id_column} {entity_id} already belongs to another PlanRevision"
                    )

        existing = self._connection.execute(
            """
            SELECT goal_id, completion_contract_id, version, supersedes_plan_revision_id
            FROM plan_revisions WHERE plan_revision_id = ?
            """,
            (plan_revision.plan_revision_id,),
        ).fetchone()
        identity = (
            str(plan_revision.goal_id),
            str(plan_revision.completion_contract_id),
            plan_revision.version,
            (
                None
                if plan_revision.supersedes_plan_revision_id is None
                else str(plan_revision.supersedes_plan_revision_id)
            ),
        )
        if existing is None:
            return
        if _identity(existing, 4) != identity:
            raise PersistenceConflictError(
                f"PlanRevision {plan_revision.plan_revision_id} identity changed"
            )
        expected_children = {
            "plan_nodes": {str(node.plan_node_id) for node in plan_revision.nodes},
            "edges": {str(edge.edge_id) for edge in plan_revision.edges},
            "branches": {str(branch.branch_id) for branch in plan_revision.branches},
        }
        id_columns = {
            "plan_nodes": "plan_node_id",
            "edges": "edge_id",
            "branches": "branch_id",
        }
        for table, expected in expected_children.items():
            column = id_columns[table]
            actual = set(
                self._strings(
                    f"SELECT {column} FROM {table} WHERE plan_revision_id = ?",
                    (plan_revision.plan_revision_id,),
                )
            )
            if actual != expected:
                raise PersistenceConflictError(
                    f"PlanRevision {plan_revision.plan_revision_id} graph identity changed"
                )

    def _snapshot(self, table: str, id_column: str, entity_id: ID) -> str | None:
        row = self._connection.execute(
            f"SELECT snapshot_json FROM {table} WHERE {id_column} = ?",
            (entity_id,),
        ).fetchone()
        return None if row is None else _row_string(row, "snapshot_json")

    def _decode_process_draft_row(self, row: sqlite3.Row) -> ProcessDraft:
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

    def _decode_result_adoption_row(self, row: sqlite3.Row) -> ResultAdoption:
        try:
            decoded = json_loads(_row_string(row, "snapshot_json"))
            if not isinstance(decoded, dict):
                raise ValueError("snapshot is not a JSON object")
            record = ResultAdoption.from_mapping(decoded)
        except (TypeError, ValueError) as error:
            raise PersistenceConflictError("ResultAdoption snapshot is invalid") from error

        for field_name in (
            "adoption_id",
            "target_run_id",
            "target_plan_revision_id",
            "target_plan_node_id",
            "source_run_id",
            "source_plan_revision_id",
            "source_process_revision_id",
            "source_plan_node_id",
            "source_attempt_id",
        ):
            if _row_string(row, field_name) != str(getattr(record, field_name)):
                raise PersistenceConflictError(
                    f"ResultAdoption {record.adoption_id} projection does not match its snapshot"
                )
        if _row_string(row, "created_at") != format_utc_datetime(record.created_at):
            raise PersistenceConflictError(
                f"ResultAdoption {record.adoption_id} projection does not match its snapshot"
            )
        return record

    def _snapshots(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> tuple[str, ...]:
        return tuple(
            _row_string(row, "snapshot_json")
            for row in self._connection.execute(sql, parameters).fetchall()
        )

    def _strings(self, sql: str, parameters: tuple[object, ...]) -> tuple[str, ...]:
        rows = self._connection.execute(sql, parameters).fetchall()
        return tuple(_row_index_string(row, 0) for row in rows)

    def _required_goal(self, goal_id: ID) -> Goal:
        goal = self.get_goal(goal_id)
        if goal is None:  # pragma: no cover - selected from the same transaction
            raise RuntimeError(f"Goal {goal_id} disappeared during query")
        return goal

    def _required_run(self, run_id: ID) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise PersistenceConflictError(f"Run {run_id} is not persisted")
        return run

    def _required_attempt(self, attempt_id: ID) -> Attempt:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise PersistenceConflictError(f"Attempt {attempt_id} is not persisted")
        return attempt

    def _required_plan_revision(self, plan_revision_id: ID) -> PlanRevision:
        revision = self.get_plan_revision(plan_revision_id)
        if revision is None:  # pragma: no cover - selected from the same transaction
            raise RuntimeError(f"PlanRevision {plan_revision_id} disappeared during query")
        return revision

    def _required_execution_plan(self, run_id: ID) -> PlanRevision:
        plan = self.get_execution_plan(run_id)
        if plan is None:
            raise PersistenceConflictError(f"Run {run_id} has no execution plan")
        return plan

    def _required_active_process_revision(self, run_id: ID) -> ProcessRevision:
        process = self.get_active_process_revision(run_id)
        if process is None:
            raise PersistenceConflictError(f"Run {run_id} has no active ProcessRevision")
        return process

    def _attempt_plan(self, attempt: Attempt) -> PlanRevision:
        run = self._required_run(attempt.run_id)
        if attempt.process_revision_id is None:
            return self._required_plan_revision(run.plan_revision_id)
        process = self.get_process_revision(attempt.process_revision_id)
        if (
            process is None
            or process.run_id != run.run_id
            or process.graph.plan_revision_id != run.plan_revision_id
        ):
            raise PersistenceConflictError(
                f"Attempt {attempt.attempt_id} process ownership changed"
            )
        return process.graph


class SQLiteEventLog:
    """Append-only Event Log sharing its caller's SQLite transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def append(self, event: Event) -> StoredEvent:
        try:
            cursor = self._connection.execute(
                """
                INSERT INTO event_log(event_id, run_id, event_type, occurred_at, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.run_id,
                    event.type.value,
                    format_utc_datetime(event.occurred_at),
                    event.to_json(),
                ),
            )
        except sqlite3.IntegrityError as error:
            if self._connection.execute(
                "SELECT 1 FROM event_log WHERE event_id = ?", (event.id,)
            ).fetchone():
                raise DuplicateEventError(f"Event {event.id} was already appended") from error
            raise
        offset = cursor.lastrowid
        if offset is None:  # pragma: no cover - SQLite always assigns the integer primary key
            raise RuntimeError("SQLite did not assign an Event offset")
        return StoredEvent(offset=offset, event=event)

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("Event limit must be a non-negative integer")
        after_offset = 0
        if after_event_id is not None:
            row = self._connection.execute(
                "SELECT event_offset FROM event_log WHERE event_id = ?",
                (after_event_id,),
            ).fetchone()
            if row is None:
                raise UnknownEventCursorError(f"unknown Event cursor {after_event_id}")
            after_offset = _row_index_integer(row, 0)

        sql = """
            SELECT event_offset, event_json FROM event_log
            WHERE event_offset > ? ORDER BY event_offset
        """
        parameters: tuple[object, ...] = (after_offset,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (after_offset, limit)
        return tuple(
            StoredEvent(
                offset=_row_index_integer(row, 0),
                event=Event.from_json(_row_index_string(row, 1)),
            )
            for row in self._connection.execute(sql, parameters).fetchall()
        )

    def latest_offset(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(event_offset), 0) FROM event_log"
        ).fetchone()
        if row is None:  # pragma: no cover - aggregate always returns one row
            return 0
        return _row_index_integer(row, 0)


class SQLiteCommandReceiptStore:
    """Durable idempotency receipts sharing the Unit of Work transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def put(self, receipt: CommandReceipt) -> None:
        existing = self.get(receipt.idempotency_key)
        if existing is not None:
            if (
                existing.command_name != receipt.command_name
                or existing.command_fingerprint != receipt.command_fingerprint
                or existing.result != receipt.result
            ):
                raise IdempotencyConflictError(
                    receipt.idempotency_key,
                    existing.command_fingerprint,
                    receipt.command_fingerprint,
                )
            return
        self._connection.execute(
            """
            INSERT INTO command_receipts(
                idempotency_key, command_name, command_fingerprint,
                result_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                receipt.idempotency_key,
                receipt.command_name,
                receipt.command_fingerprint,
                json_dumps(receipt.result),
                format_utc_datetime(receipt.created_at),
            ),
        )

    def get(self, idempotency_key: str) -> CommandReceipt | None:
        row = self._connection.execute(
            """
            SELECT command_name, command_fingerprint, result_json, created_at
            FROM command_receipts WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        result = json_loads(_row_string(row, "result_json"))
        if not isinstance(result, dict):  # pragma: no cover - SQL CHECK and writer guarantee this
            raise RuntimeError("stored Command receipt result is not an object")
        return CommandReceipt(
            idempotency_key=idempotency_key,
            command_name=_row_string(row, "command_name"),
            command_fingerprint=_row_string(row, "command_fingerprint"),
            result=result,
            created_at=parse_utc_datetime(_row_string(row, "created_at")),
        )


def _row_string(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise RuntimeError(f"SQLite column {column} is not text")
    return value


def _optional_row_string(row: sqlite3.Row, column: str) -> str | None:
    value = row[column]
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"SQLite column {column} is not text or null")
    return value


def _row_index_string(row: sqlite3.Row, index: int) -> str:
    value = row[index]
    if not isinstance(value, str):
        raise RuntimeError(f"SQLite column {index} is not text")
    return value


def _row_index_integer(row: sqlite3.Row, index: int) -> int:
    value = row[index]
    if not isinstance(value, int):
        raise RuntimeError(f"SQLite column {index} is not an integer")
    return value


def _identity(row: sqlite3.Row, size: int) -> tuple[object, ...]:
    return tuple(row[index] for index in range(size))


def _completion_contract_structure(contract: CompletionContract) -> tuple[object, ...]:
    return (
        contract.completion_contract_id,
        contract.goal_id,
        contract.version,
        contract.criteria,
        contract.required_check_ids,
        contract.created_at,
        contract.supersedes_completion_contract_id,
    )


def _run_identity(run: Run) -> tuple[object, ...]:
    return (run.run_id, run.goal_id, run.plan_revision_id, run.created_at, run.predecessor_run_id)


def _attempt_identity(attempt: Attempt) -> tuple[object, ...]:
    return (
        attempt.attempt_id,
        attempt.run_id,
        attempt.plan_node_id,
        attempt.sequence,
        attempt.created_at,
        attempt.process_revision_id,
    )


def _attempt_assignment(attempt: Attempt) -> tuple[object, ...]:
    return (
        attempt.worker_profile_id,
        attempt.worker_endpoint_id,
        attempt.agent_session_ref_id,
    )


def _worker_endpoint_identity(endpoint: WorkerEndpoint) -> tuple[object, ...]:
    return (
        endpoint.worker_endpoint_id,
        endpoint.name,
        endpoint.worker_kind,
        endpoint.endpoint_type,
        endpoint.endpoint_ref,
        endpoint.capacity,
    )


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else format_utc_datetime(value)


def _check_run_identity(check_run: CheckRun) -> tuple[object, ...]:
    return (
        check_run.check_run_id,
        check_run.run_id,
        check_run.plan_node_id,
        check_run.attempt_id,
        check_run.adoption_id,
        check_run.check_id,
        check_run.created_at,
    )


def _required_plan_node(plan_revision: PlanRevision, plan_node_id: ID) -> PlanNode:
    for node in plan_revision.nodes:
        if node.plan_node_id == plan_node_id:
            return node
    raise PersistenceConflictError(
        f"PlanNode {plan_node_id} is not in PlanRevision {plan_revision.plan_revision_id}"
    )
