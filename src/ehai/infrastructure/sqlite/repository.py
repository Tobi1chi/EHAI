"""SQLite repositories bound to one explicit Unit of Work transaction."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ehai import ID, format_utc_datetime, json_dumps, json_loads, parse_utc_datetime
from ehai.application.ports import CommandReceipt, StoredEvent
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import Checkpoint, CheckRun, CheckRunStatus, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import (
    BranchStatus,
    PlanNode,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
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
    decode_branch,
    decode_builtin_execution_ref,
    decode_check_run,
    decode_check_spec,
    decode_checkpoint,
    decode_completion_contract,
    decode_dispatch_work,
    decode_edge,
    decode_external_execution_ref,
    decode_goal,
    decode_plan_node,
    decode_plan_revision,
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
    encode_external_execution_ref,
    encode_goal,
    encode_plan_node,
    encode_plan_revision,
    encode_project,
    encode_run,
    encode_worker_endpoint,
    encode_worker_profile,
)


class PersistenceConflictError(RuntimeError):
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
        {PlanNodeStatus.READY, PlanNodeStatus.RUNNING, PlanNodeStatus.PRUNED}
    ),
    PlanNodeStatus.RUNNING: frozenset(
        {PlanNodeStatus.RUNNING, PlanNodeStatus.CANDIDATE, PlanNodeStatus.FAILED}
    ),
    PlanNodeStatus.CANDIDATE: frozenset(
        {PlanNodeStatus.CANDIDATE, PlanNodeStatus.VERIFYING, PlanNodeStatus.FAILED}
    ),
    PlanNodeStatus.VERIFYING: frozenset(
        {PlanNodeStatus.VERIFYING, PlanNodeStatus.COMPLETED, PlanNodeStatus.FAILED}
    ),
    PlanNodeStatus.FAILED: frozenset({PlanNodeStatus.FAILED, PlanNodeStatus.READY}),
    PlanNodeStatus.COMPLETED: frozenset({PlanNodeStatus.COMPLETED}),
    PlanNodeStatus.PRUNED: frozenset({PlanNodeStatus.PRUNED}),
}
_BRANCH_STATUS_TRANSITIONS = {
    BranchStatus.ACTIVE: frozenset(
        {BranchStatus.ACTIVE, BranchStatus.SELECTED, BranchStatus.PRUNED}
    ),
    BranchStatus.SELECTED: frozenset({BranchStatus.SELECTED}),
    BranchStatus.PRUNED: frozenset({BranchStatus.PRUNED}),
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
        if snapshot is None:
            return None
        nodes = tuple(
            decode_plan_node(value)
            for value in self._snapshots(
                """
                SELECT snapshot_json FROM plan_nodes
                WHERE plan_revision_id = ? ORDER BY sort_index
                """,
                (plan_revision_id,),
            )
        )
        edges = tuple(
            decode_edge(value)
            for value in self._snapshots(
                """
                SELECT snapshot_json FROM edges
                WHERE plan_revision_id = ? ORDER BY sort_index
                """,
                (plan_revision_id,),
            )
        )
        branches = tuple(
            decode_branch(value)
            for value in self._snapshots(
                """
                SELECT snapshot_json FROM branches
                WHERE plan_revision_id = ? ORDER BY sort_index
                """,
                (plan_revision_id,),
            )
        )
        return decode_plan_revision(snapshot, nodes, edges, branches)

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

    def put_run(self, run: Run) -> None:
        plan_revision = self._required_plan_revision(run.plan_revision_id)
        if plan_revision.goal_id != run.goal_id:
            raise PersistenceConflictError(
                f"Run {run.run_id} Goal does not match PlanRevision {run.plan_revision_id}"
            )
        existing = self.get_run(run.run_id)
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
        run = self._required_run(attempt.run_id)
        plan_revision = self._required_plan_revision(run.plan_revision_id)
        _required_plan_node(plan_revision, attempt.plan_node_id)
        existing = self.get_attempt(attempt.attempt_id)
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
            SELECT snapshot_json FROM dispatch_work
            WHERE status = 'pending'
               OR (status = 'claimed' AND lease_expires_at <= ?)
            ORDER BY CASE status WHEN 'claimed' THEN 0 ELSE 1 END,
                     created_at, dispatch_work_id
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
        run = self._required_run(attempt.run_id)
        plan = self._required_plan_revision(run.plan_revision_id)
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
        if attempt.run_id != check_run.run_id or attempt.plan_node_id != check_run.plan_node_id:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} does not match Attempt {attempt.attempt_id}"
            )
        run = self._required_run(check_run.run_id)
        node = _required_plan_node(
            self._required_plan_revision(run.plan_revision_id),
            check_run.plan_node_id,
        )
        if check_run.check_id not in node.required_check_ids:
            raise PersistenceConflictError(
                f"CheckRun {check_run.check_run_id} Check is not required by PlanNode "
                f"{node.plan_node_id}"
            )
        existing = self.get_check_run(check_run.check_run_id)
        if existing is not None:
            if _check_run_identity(existing) != _check_run_identity(check_run):
                raise PersistenceConflictError(
                    f"CheckRun {check_run.check_run_id} identity changed"
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
        self._validate_checkpoint_references(checkpoint)
        snapshot = encode_checkpoint(checkpoint)
        existing = self._snapshot("checkpoints", "checkpoint_id", checkpoint.checkpoint_id)
        if existing is not None:
            if existing != snapshot:
                raise PersistenceConflictError(
                    f"Checkpoint {checkpoint.checkpoint_id} is immutable"
                )
            return
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
        current_plan = self._required_plan_revision(checkpoint.plan_revision_id)
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
            "UPDATE plan_revisions SET snapshot_json = ? WHERE plan_revision_id = ?",
            (encode_plan_revision(checkpoint.plan_revision), checkpoint.plan_revision_id),
        )
        self._connection.executemany(
            "UPDATE plan_nodes SET snapshot_json = ? WHERE plan_node_id = ?",
            (
                (encode_plan_node(node), node.plan_node_id)
                for node in checkpoint.plan_revision.nodes
            ),
        )
        self._connection.executemany(
            "UPDATE branches SET snapshot_json = ? WHERE branch_id = ?",
            (
                (encode_branch(branch), branch.branch_id)
                for branch in checkpoint.plan_revision.branches
            ),
        )
        self._connection.execute(
            "UPDATE runs SET snapshot_json = ? WHERE run_id = ?",
            (encode_run(restored_run), restored_run.run_id),
        )

    def put_artifact(self, artifact: Artifact) -> None:
        if artifact.run_id is not None:
            run = self._required_run(artifact.run_id)
            plan_revision = self._required_plan_revision(run.plan_revision_id)
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
                    SELECT a.snapshot_json FROM attempts a JOIN runs r ON r.run_id = a.run_id
                    WHERE a.plan_node_id = ? AND r.plan_revision_id = ?
                    ORDER BY a.sequence DESC LIMIT 1
                    """,
                    (current_node.plan_node_id, plan_revision.plan_revision_id),
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
        persisted_plan = self._required_plan_revision(checkpoint.plan_revision_id)
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
        if attempt.run_id != decision.run_id or attempt.plan_node_id != decision.plan_node_id:
            raise PersistenceConflictError(
                f"Checkpoint {checkpoint.checkpoint_id} Gate Attempt ownership does not match"
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
                artifact.run_id != decision.run_id
                or artifact.plan_node_id != decision.plan_node_id
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


def _plan_structure(plan_revision: PlanRevision) -> tuple[object, ...]:
    nodes = tuple(
        (
            node.plan_node_id,
            node.title,
            node.instruction,
            node.kind,
            node.required_dependency_ids,
            node.required_check_ids,
            node.required_capabilities,
            node.session_policy,
        )
        for node in plan_revision.nodes
    )
    edges = tuple(
        (
            edge.edge_id,
            edge.source_node_id,
            edge.target_node_id,
            edge.edge_type,
            edge.branch_id,
            edge.condition,
        )
        for edge in plan_revision.edges
    )
    branches = tuple(
        (
            branch.branch_id,
            branch.label,
            branch.fork_node_id,
            branch.node_ids,
            branch.merge_node_id,
        )
        for branch in plan_revision.branches
    )
    return (
        plan_revision.plan_revision_id,
        plan_revision.goal_id,
        plan_revision.version,
        plan_revision.completion_contract_id,
        plan_revision.completion_contract_version,
        plan_revision.created_at,
        plan_revision.supersedes_plan_revision_id,
        plan_revision.design_document,
        nodes,
        edges,
        branches,
    )


def _run_identity(run: Run) -> tuple[object, ...]:
    return (run.run_id, run.goal_id, run.plan_revision_id, run.created_at)


def _attempt_identity(attempt: Attempt) -> tuple[object, ...]:
    return (
        attempt.attempt_id,
        attempt.run_id,
        attempt.plan_node_id,
        attempt.sequence,
        attempt.created_at,
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
