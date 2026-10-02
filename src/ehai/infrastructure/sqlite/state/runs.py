"""Runs, Attempts, dispatch work, Worker events and execution references."""

from __future__ import annotations

from datetime import datetime

from ehai import ID, format_utc_datetime, new_id
from ehai.application.process_blocks import initial_block_changes
from ehai.domain.checking import CheckKind, CheckRunStatus
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.process import (
    ProcessRevision,
    ProcessRevisionSource,
)
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import (
    AgentSessionRef,
    BuiltinExecutionRef,
    ExternalExecutionRef,
)
from ehai.infrastructure.sqlite.codec import (
    decode_agent_session_ref,
    decode_attempt,
    decode_builtin_execution_ref,
    decode_dispatch_work,
    decode_external_execution_ref,
    decode_run,
    encode_agent_session_ref,
    encode_attempt,
    encode_builtin_execution_ref,
    encode_dispatch_work,
    encode_execution_plan,
    encode_external_execution_ref,
    encode_process_revision,
    encode_run,
)
from ehai.infrastructure.sqlite.state.common import (
    _ATTEMPT_STATUS_TRANSITIONS,
    _RUN_STATUS_TRANSITIONS,
    PersistenceConflictError,
    SQLiteWorkerRegistry,
    _required_plan_node,
    _row_index_string,
)
from ehai.infrastructure.sqlite.state.host import StateRepositoryHost


class RunStateMixin:
    """Runs, Attempts, dispatch work, Worker events and execution references."""

    def put_run(self: StateRepositoryHost, run: Run) -> None:
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
                block_changes=initial_block_changes(plan_revision),
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

    def get_run(self: StateRepositoryHost, run_id: ID) -> Run | None:
        snapshot = self._snapshot("runs", "run_id", run_id)
        return None if snapshot is None else decode_run(snapshot)

    def list_runs(self: StateRepositoryHost, goal_id: ID) -> tuple[Run, ...]:
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

    def put_attempt(self: StateRepositoryHost, attempt: Attempt) -> None:
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

    def get_attempt(self: StateRepositoryHost, attempt_id: ID) -> Attempt | None:
        snapshot = self._snapshot("attempts", "attempt_id", attempt_id)
        return None if snapshot is None else decode_attempt(snapshot)

    def list_attempts(self: StateRepositoryHost, run_id: ID) -> tuple[Attempt, ...]:
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

    def put_dispatch_work(self: StateRepositoryHost, work: DispatchWork) -> None:
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

    def get_dispatch_work(self: StateRepositoryHost, dispatch_work_id: ID) -> DispatchWork | None:
        snapshot = self._snapshot("dispatch_work", "dispatch_work_id", dispatch_work_id)
        return None if snapshot is None else decode_dispatch_work(snapshot)

    def list_dispatch_work(
        self: StateRepositoryHost,
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
        self: StateRepositoryHost,
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
        self: StateRepositoryHost,
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

    def put_agent_session_ref(self: StateRepositoryHost, session: AgentSessionRef) -> None:
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

    def get_agent_session_ref(
        self: StateRepositoryHost, agent_session_ref_id: ID
    ) -> AgentSessionRef | None:
        snapshot = self._snapshot(
            "agent_session_refs", "agent_session_ref_id", agent_session_ref_id
        )
        return None if snapshot is None else decode_agent_session_ref(snapshot)

    def list_agent_session_refs(
        self: StateRepositoryHost, run_id: ID
    ) -> tuple[AgentSessionRef, ...]:
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

    def put_external_execution_ref(
        self: StateRepositoryHost, reference: ExternalExecutionRef
    ) -> None:
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
        self: StateRepositoryHost, external_execution_ref_id: ID
    ) -> ExternalExecutionRef | None:
        snapshot = self._snapshot(
            "external_execution_refs",
            "external_execution_ref_id",
            external_execution_ref_id,
        )
        return None if snapshot is None else decode_external_execution_ref(snapshot)

    def put_builtin_execution_ref(
        self: StateRepositoryHost, reference: BuiltinExecutionRef
    ) -> None:
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

    def get_builtin_execution_ref(
        self: StateRepositoryHost, builtin_execution_ref_id: ID
    ) -> BuiltinExecutionRef | None:
        snapshot = self._snapshot(
            "builtin_execution_refs",
            "builtin_execution_ref_id",
            builtin_execution_ref_id,
        )
        return None if snapshot is None else decode_builtin_execution_ref(snapshot)

    def _validate_attempt_runtime_refs(self: StateRepositoryHost, attempt: Attempt) -> None:
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
        self: StateRepositoryHost,
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

    def _required_run(self: StateRepositoryHost, run_id: ID) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise PersistenceConflictError(f"Run {run_id} is not persisted")
        return run

    def _required_attempt(self: StateRepositoryHost, attempt_id: ID) -> Attempt:
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            raise PersistenceConflictError(f"Attempt {attempt_id} is not persisted")
        return attempt

    def _attempt_plan(self: StateRepositoryHost, attempt: Attempt) -> PlanRevision:
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


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else format_utc_datetime(value)
