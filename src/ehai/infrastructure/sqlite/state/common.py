"""Errors, status transition tables and row helpers shared by the SQLite state repository."""

from __future__ import annotations

import sqlite3

from ehai import ID
from ehai.application.ports import StateConflictError
from ehai.domain.checking import CheckRunStatus
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.planning import (
    BranchStatus,
    PlanNode,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.workers import (
    WorkerEndpoint,
    WorkerProfile,
)
from ehai.infrastructure.sqlite.codec import (
    decode_worker_endpoint,
    decode_worker_profile,
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
            PlanNodeStatus.SUSPENDED,
            PlanNodeStatus.STALLED,
        }
    ),
    PlanNodeStatus.SUSPENDED: frozenset({PlanNodeStatus.SUSPENDED, PlanNodeStatus.PENDING}),
    PlanNodeStatus.STALLED: frozenset(
        {PlanNodeStatus.STALLED, PlanNodeStatus.PENDING, PlanNodeStatus.SUSPENDED}
    ),
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


def _row_string(row: sqlite3.Row, column: str) -> str:
    value = row[column]
    if not isinstance(value, str):
        raise RuntimeError(f"SQLite column {column} is not text")
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


def _worker_endpoint_identity(endpoint: WorkerEndpoint) -> tuple[object, ...]:
    return (
        endpoint.worker_endpoint_id,
        endpoint.name,
        endpoint.worker_kind,
        endpoint.endpoint_type,
        endpoint.endpoint_ref,
        endpoint.capacity,
    )


def _required_plan_node(plan_revision: PlanRevision, plan_node_id: ID) -> PlanNode:
    for node in plan_revision.nodes:
        if node.plan_node_id == plan_node_id:
            return node
    raise PersistenceConflictError(
        f"PlanNode {plan_node_id} is not in PlanRevision {plan_revision.plan_revision_id}"
    )
