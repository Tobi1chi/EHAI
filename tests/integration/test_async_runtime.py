from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ehai import ID, JsonValue, new_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    RuntimeConnector,
    SingleSlotRuntime,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.checks import CheckRunner
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    StartRun,
)
from ehai.application.execution_policy import (
    EndpointHealthStatus,
    ExecutionPolicy,
    ExecutionReplay,
    RetrySafety,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicExplorationPlanner,
    DeterministicPlanner,
    ExplorationBudget,
    ExplorationPlanRequest,
    PlanProposal,
)
from ehai.application.runtime_control import RuntimeControlService, WorkerRequestStatus
from ehai.application.service import ExecutionService
from ehai.application.workers import WorkerRequest
from ehai.domain.checking import CheckKind
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import Goal
from ehai.domain.runtime import DispatchWorkStatus
from ehai.domain.workers import (
    AttemptActivity,
    WorkerEndpoint,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)
from ehai.infrastructure.artifacts import FilesystemArtifactStore
from ehai.infrastructure.checks import ArtifactCheckAdapter, ArtifactCheckRule
from ehai.infrastructure.sqlite import SQLiteDatabase
from ehai.infrastructure.workers import FakeWorker


class _ExplorationPlanner:
    def __init__(self) -> None:
        self._planner = DeterministicExplorationPlanner()

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        return self._planner.propose(
            ExplorationPlanRequest(
                goal=goal,
                criteria=criteria,
                budget=ExplorationBudget(max_attempts=5),
            )
        )


class _TestConnector:
    def __init__(self, worker: FakeWorker, *, fail_first_stream: bool = False) -> None:
        self.worker = worker
        self.fail_first_stream = fail_first_stream
        self.requests: dict[ID, WorkerRequest] = {}
        self.executions: dict[ID, ConnectorExecution] = {}
        self.start_calls: list[ID] = []
        self.recover_calls: list[ID] = []
        self.cancel_calls: list[ID] = []
        self._failed_streams: set[ID] = set()

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        attempt_id = request.attempt_id
        self.requests[attempt_id] = request.request
        if attempt_id not in self.executions:
            self.start_calls.append(attempt_id)
            self.executions[attempt_id] = ConnectorExecution(
                attempt_id,
                f"session-{attempt_id}",
                str(new_id()),
                True,
            )
        return self.executions[attempt_id]

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        attempt_id = execution.attempt_id
        if self.fail_first_stream and not self._failed_streams:
            self._failed_streams.add(attempt_id)
            raise RuntimeError("simulated Runtime process loss")
        result = self.worker.execute(self.requests[attempt_id])
        candidate = WorkerEvent(
            f"{attempt_id}:candidate",
            attempt_id,
            WorkerEventType.CANDIDATE,
            "1",
            result=result,
        )
        yield candidate
        yield candidate
        yield WorkerEvent(
            f"{attempt_id}:completed",
            attempt_id,
            WorkerEventType.COMPLETED,
            "2",
        )

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        return AttemptActivity.RUNNING

    async def cancel(self, execution: ConnectorExecution) -> None:
        self.cancel_calls.append(execution.attempt_id)

    async def recover(
        self,
        request: ConnectorRecoveryRequest,
    ) -> ConnectorExecution | None:
        self.recover_calls.append(request.attempt_id)
        return self.executions.get(request.attempt_id)


def _build_runtime(
    tmp_path: Path,
    *,
    exploration: bool,
    fail_first_stream: bool = False,
) -> tuple[ExecutionService, SingleSlotRuntime, _TestConnector, SQLiteDatabase, FakeWorker]:
    database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = FakeWorker()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifacts,
        check_runner=CheckRunner(
            {
                CheckKind.ARTIFACT: ArtifactCheckAdapter(
                    artifacts,
                    {},
                    default_rule=ArtifactCheckRule(minimum_count=1, require_non_empty=True),
                )
            }
        ),
        workspace=tmp_path,
        attempt_budget=5,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=_ExplorationPlanner() if exploration else DeterministicPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    connector = _TestConnector(worker, fail_first_stream=fail_first_stream)
    profile = WorkerProfile("test", WorkerKind.BUILTIN, "scripted")
    endpoint = WorkerEndpoint(
        "test",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "test",
        1,
    )
    runtime = SingleSlotRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        connector=connector,
        profile=profile,
        endpoint=endpoint,
    )
    return service, runtime, connector, database, worker


def _start(service: ExecutionService) -> tuple[ID, ID]:
    project = service.create_project(CreateProject("project", "background"))
    goal = service.create_goal(CreateGoal("goal", project.project_id, "finish in background"))
    plan = service.propose_plan(ProposePlan("plan", goal.goal_id, (NON_EMPTY_ARTIFACT_CRITERION,)))
    approved = service.approve_plan(
        ApprovePlan("approve", plan.plan_revision_id, plan.completion_contract_id)
    )
    run = service.start_run(StartRun("start", approved.plan_revision_id))
    assert run.status is RunStatus.PENDING
    return run.run_id, goal.goal_id


def test_start_run_returns_before_single_slot_runtime_completes_exploration(
    tmp_path: Path,
) -> None:
    service, runtime, connector, database, worker = _build_runtime(
        tmp_path,
        exploration=True,
    )
    run_id, _ = _start(service)

    assert connector.start_calls == []
    with database.read_session() as session:
        work = session.states.list_dispatch_work()
        assert len(work) == 1 and work[0].status is DispatchWorkStatus.PENDING

    completed = asyncio.run(runtime.run_once())

    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert len(connector.start_calls) == 5
    assert len(worker.calls) == 5
    assert asyncio.run(runtime.run_once()) is None
    with database.read_session() as session:
        assert session.states.list_dispatch_work()[0].status is DispatchWorkStatus.COMPLETED
        assert all(
            attempt.status is AttemptStatus.SUCCEEDED
            for attempt in session.states.list_attempts(run_id)
        )


def test_runtime_restart_recovers_original_execution_without_duplicate_start(
    tmp_path: Path,
) -> None:
    service, runtime, connector, database, _ = _build_runtime(
        tmp_path,
        exploration=False,
        fail_first_stream=True,
    )
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="process loss"):
        asyncio.run(runtime.run_once())
    assert len(connector.start_calls) == 1

    recovered = asyncio.run(runtime.recover_startup())

    assert len(recovered) == 1 and recovered[0].status is RunStatus.COMPLETED
    assert len(connector.start_calls) == 1
    assert len(connector.recover_calls) == 1
    connection = database.connect()
    try:
        receipts = connection.execute(
            "SELECT COUNT(*) FROM worker_event_receipts WHERE attempt_id IN "
            "(SELECT attempt_id FROM attempts WHERE run_id = ?)",
            (run_id,),
        ).fetchone()[0]
        assert receipts == 2
    finally:
        connection.close()


def test_completed_result_wins_cancel_race(tmp_path: Path) -> None:
    service, runtime, connector, database, _ = _build_runtime(
        tmp_path,
        exploration=False,
        fail_first_stream=True,
    )
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="process loss"):
        asyncio.run(runtime.run_once())
    with database.read_session() as session:
        attempt = session.states.list_attempts(run_id)[0]

    completed = asyncio.run(runtime.cancel_attempt(attempt.attempt_id))

    assert completed.status is RunStatus.COMPLETED
    assert connector.cancel_calls == [attempt.attempt_id]
    with database.read_session() as session:
        assert session.states.list_dispatch_work()[0].status is DispatchWorkStatus.COMPLETED


def test_recovery_retries_when_provider_confirms_original_execution_is_missing(
    tmp_path: Path,
) -> None:
    service, runtime, connector, database, _ = _build_runtime(
        tmp_path,
        exploration=False,
        fail_first_stream=True,
    )
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="process loss"):
        asyncio.run(runtime.run_once())
    with database.read_session() as session:
        attempt = session.states.list_attempts(run_id)[0]
    connector.executions.clear()

    recovered = asyncio.run(runtime.recover_startup())

    assert len(recovered) == 1 and recovered[0].status is RunStatus.COMPLETED
    assert connector.start_calls[0] == attempt.attempt_id
    assert len(connector.start_calls) == 2
    with database.read_session() as session:
        stored = session.states.get_attempt(attempt.attempt_id)
        attempts = session.states.list_attempts(run_id)
        events = tuple(item.event.type for item in session.events.list_events())
    assert stored is not None and stored.status is AttemptStatus.INTERRUPTED
    assert tuple(item.status for item in attempts) == (
        AttemptStatus.INTERRUPTED,
        AttemptStatus.SUCCEEDED,
    )
    assert EventType.ATTEMPT_RETRY_SCHEDULED in events


class _WatchdogConnector:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.cancel_calls = 0
        self._cursor = 0

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        return ConnectorExecution(
            request.attempt_id,
            f"session-{request.attempt_id}",
            str(new_id()),
            True,
        )

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        if self.mode == "lease":
            await asyncio.Event().wait()
            return
        if self.mode == "waiting":
            yield self._event(execution, WorkerEventType.WAITING)
        while True:
            await asyncio.sleep(0.005)
            yield self._event(execution, WorkerEventType.HEARTBEAT)

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        if self.mode == "lease":
            return AttemptActivity.STALLED
        if self.mode == "waiting":
            return AttemptActivity.WAITING
        return AttemptActivity.RUNNING

    async def cancel(self, execution: ConnectorExecution) -> None:
        del execution
        self.cancel_calls += 1

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        del request
        return None

    def _event(
        self,
        execution: ConnectorExecution,
        event_type: WorkerEventType,
    ) -> WorkerEvent:
        self._cursor += 1
        return WorkerEvent(
            f"{execution.attempt_id}:{self._cursor}",
            execution.attempt_id,
            event_type,
            str(self._cursor),
            reason=(
                "explicit approval required" if event_type is WorkerEventType.WAITING else None
            ),
        )


class _RetryConnector:
    def __init__(self, *, start_timeout_once: bool) -> None:
        self.start_timeout_once = start_timeout_once
        self.start_calls = 0
        self.requests: dict[ID, WorkerRequest] = {}
        self.worker = FakeWorker()

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        self.start_calls += 1
        self.requests[request.attempt_id] = request.request
        if self.start_timeout_once and self.start_calls == 1:
            await asyncio.sleep(1)
        return ConnectorExecution(
            request.attempt_id,
            f"session-{request.attempt_id}",
            str(new_id()),
            True,
        )

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del after_cursor
        if not self.start_timeout_once:
            yield WorkerEvent(
                f"{execution.attempt_id}:failed",
                execution.attempt_id,
                WorkerEventType.FAILED,
                "1",
                reason="provider outcome is unknown",
                retry_safety=RetrySafety.UNKNOWN,
            )
            return
        result = self.worker.execute(self.requests[execution.attempt_id])
        yield WorkerEvent(
            f"{execution.attempt_id}:candidate",
            execution.attempt_id,
            WorkerEventType.CANDIDATE,
            "1",
            result=result,
        )
        yield WorkerEvent(
            f"{execution.attempt_id}:completed",
            execution.attempt_id,
            WorkerEventType.COMPLETED,
            "2",
        )

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        return AttemptActivity.RUNNING

    async def cancel(self, execution: ConnectorExecution) -> None:
        del execution

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        del request
        return None


def test_watchdog_separates_heartbeat_progress_waiting_and_absolute_deadline(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "heartbeat",
            ExecutionPolicy(
                heartbeat_lease=timedelta(milliseconds=30),
                no_progress_timeout=timedelta(milliseconds=40),
                absolute_attempt_timeout=timedelta(milliseconds=200),
                cancel_grace=timedelta(milliseconds=10),
            ),
            "no progress",
        ),
        (
            "lease",
            ExecutionPolicy(
                heartbeat_lease=timedelta(milliseconds=20),
                no_progress_timeout=timedelta(milliseconds=200),
                absolute_attempt_timeout=timedelta(milliseconds=300),
                cancel_grace=timedelta(milliseconds=10),
            ),
            "heartbeat lease",
        ),
        (
            "waiting",
            ExecutionPolicy(
                heartbeat_lease=timedelta(milliseconds=100),
                no_progress_timeout=timedelta(milliseconds=100),
                absolute_attempt_timeout=timedelta(milliseconds=300),
                cancel_grace=timedelta(milliseconds=20),
            ),
            "absolute Attempt deadline",
        ),
    )
    for mode, policy, expected_reason in cases:
        workspace = tmp_path / mode
        workspace.mkdir()
        connector = _WatchdogConnector(mode)
        service, runtime, database = _policy_runtime(workspace, connector, policy)
        run_id, _ = _start(service)

        result = asyncio.run(runtime.run_once())

        assert result is not None and result.status is RunStatus.PAUSED
        assert connector.cancel_calls == 1
        with database.read_session() as session:
            attempt = session.states.list_attempts(run_id)[0]
        assert attempt.status is AttemptStatus.TIMED_OUT
        assert expected_reason in (attempt.outcome_reason or "")


def test_retry_requires_safe_knowledge_and_replay_matches_persisted_facts(
    tmp_path: Path,
) -> None:
    safe_workspace = tmp_path / "safe"
    safe_workspace.mkdir()
    safe_connector = _RetryConnector(start_timeout_once=True)
    policy = ExecutionPolicy(
        start_timeout=timedelta(milliseconds=20),
        heartbeat_lease=timedelta(seconds=1),
        no_progress_timeout=timedelta(seconds=1),
        absolute_attempt_timeout=timedelta(seconds=2),
        cancel_grace=timedelta(milliseconds=20),
    )
    service, runtime, database = _policy_runtime(safe_workspace, safe_connector, policy)
    run_id, _ = _start(service)

    completed = asyncio.run(runtime.run_once())

    assert completed is not None and completed.status is RunStatus.COMPLETED
    assert safe_connector.start_calls == 2
    with database.read_session() as session:
        attempts = session.states.list_attempts(run_id)
        work = session.states.list_dispatch_work()[0]
        stored_events = session.events.list_events()
    replayed = ExecutionReplay().replay(stored_events)
    assert tuple(attempt.status for attempt in attempts) == (
        AttemptStatus.TIMED_OUT,
        AttemptStatus.SUCCEEDED,
    )
    assert replayed.claimed_work_ids[run_id] == work.dispatch_work_id
    assert replayed.dispatched_attempt_ids == {attempt.attempt_id for attempt in attempts}
    assert set(replayed.bindings) == {attempts[1].attempt_id}
    assert replayed.retries == {attempts[0].attempt_id: RetrySafety.NO_EXECUTION_HANDLE}
    assert attempts[1].worker_endpoint_id is not None
    assert replayed.endpoint_health[attempts[1].worker_endpoint_id] is EndpointHealthStatus.HEALTHY
    usage = tuple(
        item.event.payload
        for item in stored_events
        if item.event.type is EventType.PROVIDER_USAGE_RECORDED
    )
    assert usage[-1]["provider_cost"] is None
    assert usage[-1]["provider_cost_available"] is False

    unknown_workspace = tmp_path / "unknown"
    unknown_workspace.mkdir()
    unknown_connector = _RetryConnector(start_timeout_once=False)
    service, runtime, database = _policy_runtime(unknown_workspace, unknown_connector, policy)
    unknown_run_id, _ = _start(service)

    paused = asyncio.run(runtime.run_once())

    assert paused is not None and paused.status is RunStatus.PAUSED
    assert unknown_connector.start_calls == 1
    with database.read_session() as session:
        unknown_attempts = session.states.list_attempts(unknown_run_id)
    assert len(unknown_attempts) == 1
    assert unknown_attempts[0].status is AttemptStatus.INTERRUPTED


def test_expired_dispatch_claim_is_reclaimed_without_duplicate_work(tmp_path: Path) -> None:
    service, _, _, database, _ = _build_runtime(tmp_path, exploration=False)
    run_id, _ = _start(service)
    claimed_at = datetime(2026, 9, 2, tzinfo=UTC)
    with database.unit_of_work() as uow:
        original = uow.states.claim_next_dispatch_work(
            owner="runtime-old",
            at=claimed_at,
            lease_expires_at=claimed_at + timedelta(seconds=1),
        )
        uow.commit()
    assert original is not None and original.run_id == run_id

    with database.unit_of_work() as uow:
        before_expiry = uow.states.claim_next_dispatch_work(
            owner="runtime-new",
            at=claimed_at + timedelta(milliseconds=500),
            lease_expires_at=claimed_at + timedelta(seconds=2),
        )
        uow.commit()
    assert before_expiry is None

    with database.unit_of_work() as uow:
        reclaimed = uow.states.claim_next_dispatch_work(
            owner="runtime-new",
            at=claimed_at + timedelta(seconds=1),
            lease_expires_at=claimed_at + timedelta(seconds=3),
        )
        uow.commit()
    assert reclaimed is not None
    assert reclaimed.dispatch_work_id == original.dispatch_work_id
    assert reclaimed.claim_owner == "runtime-new"


class _PendingRequest:
    def __init__(self, execution: ConnectorExecution) -> None:
        self.request_id = 77
        self.method = "item/commandExecution/requestApproval"
        self.thread_id = execution.provider_session_id
        self.turn_id = execution.provider_execution_id
        self.item_id = "item-approval"

    @property
    def params(self) -> dict[str, JsonValue]:
        return {"reason": "internal path C:\\private\\workspace"}


class _InteractiveConnector:
    def __init__(self) -> None:
        self.execution: ConnectorExecution | None = None
        self.resolutions: list[tuple[int | str, dict[str, JsonValue]]] = []

    async def start(self, request: ConnectorStartRequest) -> ConnectorExecution:
        self.execution = ConnectorExecution(
            request.attempt_id,
            f"session-{request.attempt_id}",
            str(new_id()),
            True,
        )
        return self.execution

    async def events(
        self,
        execution: ConnectorExecution,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[WorkerEvent]:
        del execution, after_cursor
        raise RuntimeError("hold execution for Runtime Control")
        yield

    async def inspect(self, execution: ConnectorExecution) -> AttemptActivity:
        del execution
        return AttemptActivity.WAITING

    async def cancel(self, execution: ConnectorExecution) -> None:
        del execution

    async def recover(self, request: ConnectorRecoveryRequest) -> ConnectorExecution | None:
        del request
        return self.execution

    def pending_requests(
        self,
        execution: ConnectorExecution | None = None,
    ) -> tuple[_PendingRequest, ...]:
        assert execution == self.execution and execution is not None
        return (_PendingRequest(execution),)

    async def resolve_request(
        self,
        request_id: int | str,
        result: dict[str, JsonValue],
    ) -> None:
        self.resolutions.append((request_id, dict(result)))


def test_runtime_control_sanitizes_resolves_and_extends_waiting_attempt(
    tmp_path: Path,
) -> None:
    connector = _InteractiveConnector()
    policy = ExecutionPolicy(
        heartbeat_lease=timedelta(seconds=1),
        no_progress_timeout=timedelta(seconds=1),
        absolute_attempt_timeout=timedelta(minutes=5),
    )
    service, runtime, database = _policy_runtime(tmp_path, connector, policy)
    run_id, _ = _start(service)
    with pytest.raises(RuntimeError, match="hold execution"):
        asyncio.run(runtime.run_once())
    with database.read_session() as session:
        attempt = session.states.list_attempts(run_id)[0]
    assert attempt.worker_endpoint_id is not None and attempt.deadline_at is not None
    control = RuntimeControlService(
        uow_factory=database.unit_of_work,
        orchestrator=runtime.orchestrator,
        runtimes={attempt.worker_endpoint_id: runtime},
    )

    waiting = control.list_waiting_requests(attempt.attempt_id)

    assert len(waiting) == 1
    assert waiting[0].summary == "command execution approval required"
    assert "private" not in waiting[0].summary
    resolved = asyncio.run(
        control.resolve_worker_request(waiting[0].worker_request_id, {"decision": "accept"})
    )
    assert resolved.status is WorkerRequestStatus.RESOLVED
    assert connector.resolutions == [(77, {"decision": "accept"})]

    extended = control.extend_deadline(
        attempt.attempt_id,
        attempt.deadline_at + timedelta(minutes=1),
    )
    assert extended.deadline_at == attempt.deadline_at + timedelta(minutes=1)


def _policy_runtime(
    tmp_path: Path,
    connector: RuntimeConnector,
    policy: ExecutionPolicy,
) -> tuple[ExecutionService, SingleSlotRuntime, SQLiteDatabase]:
    database = SQLiteDatabase(tmp_path / "policy.sqlite3")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    worker = FakeWorker()
    orchestrator = Orchestrator(
        uow_factory=database.unit_of_work,
        worker=worker,
        artifact_store=artifacts,
        check_runner=CheckRunner(
            {
                CheckKind.ARTIFACT: ArtifactCheckAdapter(
                    artifacts,
                    {},
                    default_rule=ArtifactCheckRule(minimum_count=1, require_non_empty=True),
                )
            }
        ),
        workspace=tmp_path,
        attempt_budget=5,
    )
    service = ExecutionService(
        uow_factory=database.unit_of_work,
        planner=DeterministicPlanner(),
        orchestrator=orchestrator,
        background_start=True,
    )
    profile = WorkerProfile("policy", WorkerKind.BUILTIN, "scripted")
    endpoint = WorkerEndpoint(
        "policy",
        WorkerKind.BUILTIN,
        WorkerEndpointType.IN_PROCESS,
        "policy",
        1,
    )
    runtime = SingleSlotRuntime(
        uow_factory=database.unit_of_work,
        orchestrator=orchestrator,
        connector=connector,
        profile=profile,
        endpoint=endpoint,
        policy=policy,
    )
    return service, runtime, database
