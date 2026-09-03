"""Single-process P2 Scheduler and stable capability/capacity Dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from ehai import ID, normalize_id, utc_now
from ehai.application.async_runtime import (
    HealthAwareConnector,
    RuntimeConnector,
    SingleSlotRuntime,
)
from ehai.application.execution_policy import (
    EndpointHealthStatus,
    ExecutionPolicy,
)
from ehai.application.orchestrator import Orchestrator
from ehai.application.ports import UnitOfWork
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanNode
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import (
    WorkerEndpoint,
    WorkerEndpointStatus,
    WorkerProfile,
)
from ehai.domain.workspaces import WorkspaceLease, WorkspaceRef


class WorkspaceAllocationPort(Protocol):
    @property
    def reference(self) -> WorkspaceRef: ...

    @property
    def lease(self) -> WorkspaceLease: ...


class WorkspaceManagerPort(Protocol):
    def can_isolate_writes(self) -> bool: ...

    def allocate(
        self,
        *,
        run_id: ID,
        attempt_id: ID,
        write_capable: bool,
        isolate: bool,
    ) -> WorkspaceAllocationPort: ...

    def cleanup(self, allocation: WorkspaceAllocationPort) -> WorkspaceLease: ...

    def allocation_for_attempt(self, attempt_id: ID) -> WorkspaceAllocationPort | None: ...


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    """Integer capacity limits enforced inside one Execution Plane process."""

    global_capacity: int
    project_capacity: int
    run_capacity: int
    profile_capacity: Mapping[ID, int]
    endpoint_capacity: Mapping[ID, int]

    def __post_init__(self) -> None:
        for name in ("global_capacity", "project_capacity", "run_capacity"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if any(type(value) is not int or value < 1 for value in self.profile_capacity.values()):
            raise ValueError("profile capacities must be positive integers")
        if any(type(value) is not int or value < 1 for value in self.endpoint_capacity.values()):
            raise ValueError("endpoint capacities must be positive integers")


@dataclass(frozen=True, slots=True)
class CapacityUsage:
    """Current slots held by active Connector executions."""

    total: int
    projects: Mapping[ID, int]
    runs: Mapping[ID, int]
    profiles: Mapping[ID, int]
    endpoints: Mapping[ID, int]


@dataclass(frozen=True, slots=True)
class DispatchAssignment:
    profile: WorkerProfile
    endpoint: WorkerEndpoint


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    assignment: DispatchAssignment | None
    reason: str | None


class Dispatcher:
    """Select a stable eligible Profile/Endpoint pair without executing work."""

    def __init__(
        self,
        *,
        profiles: tuple[WorkerProfile, ...],
        endpoints: tuple[WorkerEndpoint, ...],
        capacity: CapacityPolicy,
    ) -> None:
        self.profiles = profiles
        self.endpoints = endpoints
        self.capacity = capacity

    def select(
        self,
        node: PlanNode,
        *,
        project_id: ID,
        run_id: ID,
        usage: CapacityUsage,
        workspace_conflict: bool,
        endpoint_health: Mapping[ID, EndpointHealthStatus] | None = None,
    ) -> DispatchDecision:
        if workspace_conflict:
            return DispatchDecision(None, "workspace conflict")
        if usage.total >= self.capacity.global_capacity:
            return DispatchDecision(None, "global capacity exhausted")
        if usage.projects.get(project_id, 0) >= self.capacity.project_capacity:
            return DispatchDecision(None, "Project capacity exhausted")
        if usage.runs.get(run_id, 0) >= self.capacity.run_capacity:
            return DispatchDecision(None, "Run capacity exhausted")
        profiles = sorted(
            (
                profile
                for profile in self.profiles
                if node.required_capabilities.issubset(profile.capabilities)
                and usage.profiles.get(profile.worker_profile_id, 0)
                < self.capacity.profile_capacity.get(
                    profile.worker_profile_id,
                    self.capacity.global_capacity,
                )
            ),
            key=lambda item: (-item.priority, item.worker_profile_id),
        )
        for profile in profiles:
            endpoints = sorted(
                (
                    endpoint
                    for endpoint in self.endpoints
                    if endpoint.worker_kind is profile.kind
                    and endpoint.status is WorkerEndpointStatus.ENABLED
                    and (endpoint_health or {}).get(
                        endpoint.worker_endpoint_id,
                        EndpointHealthStatus.UNKNOWN,
                    )
                    is not EndpointHealthStatus.UNHEALTHY
                    and usage.endpoints.get(endpoint.worker_endpoint_id, 0)
                    < min(
                        endpoint.capacity,
                        self.capacity.endpoint_capacity.get(
                            endpoint.worker_endpoint_id,
                            endpoint.capacity,
                        ),
                    )
                ),
                key=lambda item: item.worker_endpoint_id,
            )
            if endpoints:
                return DispatchDecision(DispatchAssignment(profile, endpoints[0]), None)
        return DispatchDecision(None, "no eligible WorkerProfile/Endpoint capacity")


@dataclass(frozen=True, slots=True)
class _ActiveExecution:
    work: DispatchWork
    attempt: Attempt
    project_id: ID
    assignment: DispatchAssignment
    helper: SingleSlotRuntime
    workspace: WorkspaceAllocationPort | None


class ConcurrentRuntime:
    """Dispatch multiple queued Attempts concurrently within explicit capacities."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        orchestrator: Orchestrator,
        dispatcher: Dispatcher,
        connectors: Mapping[ID, RuntimeConnector],
        policy: ExecutionPolicy | None = None,
        workspace_manager: WorkspaceManagerPort | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._orchestrator = orchestrator
        self._dispatcher = dispatcher
        self._connectors = dict(connectors)
        self._policy = (
            ExecutionPolicy(max_concurrency=dispatcher.capacity.global_capacity)
            if policy is None
            else policy
        )
        self._endpoint_health = {
            endpoint.worker_endpoint_id: EndpointHealthStatus.UNKNOWN
            for endpoint in dispatcher.endpoints
        }
        self._workspace_manager = workspace_manager
        self._claim_owner = f"scheduler:{utc_now().timestamp()}"
        self._active_helpers: dict[ID, SingleSlotRuntime] = {}
        self._active_tasks: dict[ID, asyncio.Task[Run]] = {}
        self._controlled_runs: set[ID] = set()

    @property
    def orchestrator(self) -> Orchestrator:
        return self._orchestrator

    def connector_for(self, worker_endpoint_id: ID) -> RuntimeConnector | None:
        return self._connectors.get(worker_endpoint_id)

    async def cancel_attempt(self, attempt_id: ID) -> Run:
        normalized_id = normalize_id(attempt_id)
        helper = self._active_helpers.get(normalized_id)
        task = self._active_tasks.get(normalized_id)
        if helper is None or task is None:
            raise RuntimeError(f"Attempt {normalized_id} is not active in this Runtime")
        await helper.request_cancel(normalized_id)
        return await asyncio.shield(task)

    async def quiesce_run(self, run_id: ID) -> None:
        """Stop new scheduling and settle every active Attempt for one Run."""
        normalized_id = normalize_id(run_id)
        self._controlled_runs.add(normalized_id)
        active: list[tuple[ID, SingleSlotRuntime, asyncio.Task[Run]]] = []
        with self._uow_factory() as uow:
            for attempt_id, task in tuple(self._active_tasks.items()):
                attempt = uow.states.get_attempt(attempt_id)
                helper = self._active_helpers.get(attempt_id)
                if (
                    attempt is not None
                    and attempt.run_id == normalized_id
                    and helper is not None
                    and not task.done()
                ):
                    active.append((attempt_id, helper, task))
        grace_seconds = self._policy.cancel_grace.total_seconds()
        cancellations = await asyncio.gather(
            *(
                asyncio.wait_for(helper.request_cancel(attempt_id), timeout=grace_seconds)
                for attempt_id, helper, _ in active
            ),
            return_exceptions=True,
        )
        cancellation_error = next(
            (result for result in cancellations if isinstance(result, BaseException)),
            None,
        )
        active_tasks = tuple(task for _, _, task in active)
        if active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=grace_seconds,
                )
            except TimeoutError as error:
                for task in active_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*active_tasks, return_exceptions=True)
                if cancellation_error is None:
                    cancellation_error = error
        with self._uow_factory() as uow:
            unsettled = tuple(
                attempt
                for attempt in uow.states.list_attempts(normalized_id)
                if attempt.status is AttemptStatus.RUNNING
            )
        for attempt in unsettled:
            self._orchestrator.interrupt_attempt(
                attempt.attempt_id,
                "Runtime control quiesced an unsettled execution",
            )
        if cancellation_error is not None:
            raise RuntimeError(
                f"Runtime could not quiesce Run {normalized_id}: "
                f"{type(cancellation_error).__name__}"
            ) from cancellation_error

    def resume_run_scheduling(self, run_id: ID) -> None:
        self._controlled_runs.discard(normalize_id(run_id))

    async def run_until_idle(self) -> tuple[Run, ...]:
        """Run until every dispatchable Run is terminal or only blocked work remains."""
        await self._refresh_endpoint_health()
        works = self._claim_all_work()
        tasks: dict[asyncio.Task[Run], _ActiveExecution] = {}
        terminal: dict[ID, Run] = {}
        while works or tasks:
            works.update(self._claim_all_work())
            with self._uow_factory() as uow:
                inactive = tuple(
                    (run_id, work, uow.states.get_run(run_id)) for run_id, work in works.items()
                )
            for run_id, work, run in inactive:
                if run is None:
                    continue
                should_finish = run.status in {
                    RunStatus.CANCELLED,
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                } or (run.status is RunStatus.PAUSED and run_id not in self._controlled_runs)
                if should_finish:
                    self._helper_for_first_endpoint().finish_dispatch_work(work)
                    works.pop(run_id, None)
                    terminal[run_id] = run
            scheduled = self._schedule(works, tasks)
            if not tasks:
                if not scheduled:
                    break
                continue
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                active = tasks.pop(task)
                self._active_helpers.pop(active.attempt.attempt_id, None)
                self._active_tasks.pop(active.attempt.attempt_id, None)
                self._endpoint_health[active.assignment.endpoint.worker_endpoint_id] = (
                    active.helper.endpoint_health.status
                )
                try:
                    run = task.result()
                finally:
                    if active.workspace is not None and self._workspace_manager is not None:
                        self._workspace_manager.cleanup(active.workspace)
                if run.status is not RunStatus.RUNNING:
                    if not (run.status is RunStatus.PAUSED and run.run_id in self._controlled_runs):
                        active.helper.finish_dispatch_work(active.work)
                    works.pop(active.work.run_id, None)
                    terminal[run.run_id] = run
        return tuple(terminal[key] for key in sorted(terminal))

    async def recover_startup(self) -> tuple[Run, ...]:
        """Recover every bound Attempt using its original Endpoint and Workspace."""
        await self._refresh_endpoint_health()
        works = self._claim_all_work()
        tasks: dict[asyncio.Task[Run], _ActiveExecution] = {}
        with self._uow_factory() as uow:
            for work in works.values():
                run = uow.states.get_run(work.run_id)
                if run is None:
                    continue
                goal = uow.states.get_goal(run.goal_id)
                if goal is None:
                    raise RuntimeError(f"Run {run.run_id} Goal is missing")
                for attempt in uow.states.list_attempts(run.run_id):
                    if attempt.status is not AttemptStatus.RUNNING:
                        continue
                    assignment = self._persisted_assignment(attempt)
                    connector = self._connectors.get(assignment.endpoint.worker_endpoint_id)
                    if connector is None:
                        raise RuntimeError(
                            f"WorkerEndpoint {assignment.endpoint.worker_endpoint_id} "
                            "has no Connector"
                        )
                    workspace = (
                        None
                        if self._workspace_manager is None
                        else self._workspace_manager.allocation_for_attempt(attempt.attempt_id)
                    )
                    helper = SingleSlotRuntime(
                        uow_factory=self._uow_factory,
                        orchestrator=self._orchestrator,
                        connector=connector,
                        profile=assignment.profile,
                        endpoint=_single_slot_endpoint(assignment.endpoint),
                        policy=self._policy,
                        workspace=(None if workspace is None else Path(workspace.reference.path)),
                    )
                    task = asyncio.create_task(helper.recover_attempt(attempt))
                    active = _ActiveExecution(
                        work,
                        attempt,
                        goal.project_id,
                        assignment,
                        helper,
                        workspace,
                    )
                    tasks[task] = active
                    self._active_helpers[attempt.attempt_id] = helper
                    self._active_tasks[attempt.attempt_id] = task
        if not tasks:
            return ()
        completed = await asyncio.gather(*tasks, return_exceptions=True)
        terminal: dict[ID, Run] = {}
        first_error: BaseException | None = None
        for task, result in zip(tasks, completed, strict=True):
            active = tasks[task]
            self._active_helpers.pop(active.attempt.attempt_id, None)
            self._active_tasks.pop(active.attempt.attempt_id, None)
            try:
                if isinstance(result, BaseException):
                    if first_error is None:
                        first_error = result
                    continue
                if result.status is not RunStatus.RUNNING:
                    active.helper.finish_dispatch_work(active.work)
                    terminal[result.run_id] = result
            finally:
                if active.workspace is not None and self._workspace_manager is not None:
                    self._workspace_manager.cleanup(active.workspace)
        if first_error is not None:
            raise first_error
        return tuple(terminal[key] for key in sorted(terminal))

    async def _refresh_endpoint_health(self) -> None:
        for endpoint_id, connector in self._connectors.items():
            if isinstance(connector, HealthAwareConnector):
                try:
                    self._endpoint_health[endpoint_id] = await connector.health()
                except Exception:
                    self._endpoint_health[endpoint_id] = EndpointHealthStatus.UNHEALTHY
            else:
                self._endpoint_health[endpoint_id] = EndpointHealthStatus.UNKNOWN

    def cancel_queued_attempt(self, attempt_id: ID) -> Run:
        run = self._orchestrator.cancel_queued_attempt(attempt_id)
        with self._uow_factory() as uow:
            works = tuple(
                work
                for work in uow.states.list_dispatch_work(DispatchWorkStatus.CLAIMED)
                if work.run_id == run.run_id
            )
        if works:
            self._helper_for_first_endpoint().finish_dispatch_work(works[0])
        return run

    def _schedule(
        self,
        works: dict[ID, DispatchWork],
        tasks: dict[asyncio.Task[Run], _ActiveExecution],
    ) -> bool:
        for run_id in sorted(works):
            self._orchestrator.queue_ready_attempts(run_id, limit=100)
        scheduled = False
        while True:
            if len(tasks) >= self._policy.max_concurrency:
                return scheduled
            usage = _capacity_usage(tasks.values())
            candidate = self._next_assignment(works, tasks, usage)
            if candidate is None:
                return scheduled
            work, attempt, project_id, assignment, write_capable, isolate = candidate
            connector = self._connectors.get(assignment.endpoint.worker_endpoint_id)
            if connector is None:
                raise RuntimeError(
                    f"WorkerEndpoint {assignment.endpoint.worker_endpoint_id} has no Connector"
                )
            workspace = (
                None
                if self._workspace_manager is None
                else self._workspace_manager.allocate(
                    run_id=attempt.run_id,
                    attempt_id=attempt.attempt_id,
                    write_capable=write_capable,
                    isolate=isolate,
                )
            )
            try:
                request = self._orchestrator.start_queued_attempt(attempt.attempt_id)
                helper = SingleSlotRuntime(
                    uow_factory=self._uow_factory,
                    orchestrator=self._orchestrator,
                    connector=connector,
                    profile=assignment.profile,
                    endpoint=_single_slot_endpoint(assignment.endpoint),
                    policy=self._policy,
                    workspace=(None if workspace is None else Path(workspace.reference.path)),
                )
                task = asyncio.create_task(helper.execute_started_request(request))
            except BaseException:
                if workspace is not None and self._workspace_manager is not None:
                    self._workspace_manager.cleanup(workspace)
                raise
            tasks[task] = _ActiveExecution(
                work,
                request.attempt,
                project_id,
                assignment,
                helper,
                workspace,
            )
            self._active_helpers[request.attempt.attempt_id] = helper
            self._active_tasks[request.attempt.attempt_id] = task
            scheduled = True

    def _next_assignment(
        self,
        works: dict[ID, DispatchWork],
        tasks: dict[asyncio.Task[Run], _ActiveExecution],
        usage: CapacityUsage,
    ) -> tuple[DispatchWork, Attempt, ID, DispatchAssignment, bool, bool] | None:
        with self._uow_factory() as uow:
            for run_id in sorted(works):
                if run_id in self._controlled_runs:
                    continue
                run = uow.states.get_run(run_id)
                if run is None or run.status is not RunStatus.RUNNING:
                    continue
                goal = uow.states.get_goal(run.goal_id)
                plan = uow.states.get_plan_revision(run.plan_revision_id)
                if goal is None or plan is None:
                    raise RuntimeError(f"Run {run_id} context is missing")
                pending = tuple(
                    attempt
                    for attempt in uow.states.list_attempts(run_id)
                    if attempt.status is AttemptStatus.PENDING
                )
                for attempt in pending:
                    node = next(
                        node for node in plan.nodes if node.plan_node_id == attempt.plan_node_id
                    )
                    write_capable = _may_write_workspace(node, self._dispatcher.profiles)
                    workspace_conflict = (
                        write_capable
                        and (
                            self._workspace_manager is None
                            or not self._workspace_manager.can_isolate_writes()
                        )
                        and any(
                            _active_may_write_workspace(
                                active,
                                self._dispatcher.profiles,
                                uow,
                            )
                            for active in tasks.values()
                        )
                    )
                    decision = self._dispatcher.select(
                        node,
                        project_id=goal.project_id,
                        run_id=run_id,
                        usage=usage,
                        workspace_conflict=workspace_conflict,
                        endpoint_health=self._endpoint_health,
                    )
                    if decision.assignment is not None:
                        isolate = write_capable and (
                            self._workspace_manager is not None
                            and self._workspace_manager.can_isolate_writes()
                        )
                        return (
                            works[run_id],
                            attempt,
                            goal.project_id,
                            decision.assignment,
                            write_capable,
                            isolate,
                        )
                    uow.states.put_attempt(attempt.queue(decision.reason or "waiting for dispatch"))
            uow.commit()
        return None

    def _claim_all_work(self) -> dict[ID, DispatchWork]:
        claimed: dict[ID, DispatchWork] = {}
        with self._uow_factory() as uow:
            for profile in self._dispatcher.profiles:
                uow.worker_registry.put_worker_profile(profile)
            for endpoint in self._dispatcher.endpoints:
                uow.worker_registry.put_worker_endpoint(endpoint)
            for work in uow.states.list_dispatch_work(DispatchWorkStatus.CLAIMED):
                if work.claim_owner == self._claim_owner:
                    claimed[work.run_id] = work
            while True:
                claimed_at = utc_now()
                claimed_work = uow.states.claim_next_dispatch_work(
                    owner=self._claim_owner,
                    at=claimed_at,
                    lease_expires_at=claimed_at + timedelta(minutes=5),
                )
                if claimed_work is None:
                    break
                claimed[claimed_work.run_id] = claimed_work
                uow.events.append(
                    Event(
                        type=EventType.DISPATCH_WORK_CLAIMED,
                        correlation_id=claimed_work.dispatch_work_id,
                        run_id=claimed_work.run_id,
                        payload={
                            "dispatch_work_id": claimed_work.dispatch_work_id,
                            "claim_owner": self._claim_owner,
                        },
                        occurred_at=claimed_at,
                    )
                )
            uow.commit()
        return claimed

    def _helper_for_first_endpoint(self) -> SingleSlotRuntime:
        endpoint = self._dispatcher.endpoints[0]
        profile = next(
            profile for profile in self._dispatcher.profiles if profile.kind is endpoint.worker_kind
        )
        connector = self._connectors[endpoint.worker_endpoint_id]
        return SingleSlotRuntime(
            uow_factory=self._uow_factory,
            orchestrator=self._orchestrator,
            connector=connector,
            profile=profile,
            endpoint=_single_slot_endpoint(endpoint),
            policy=self._policy,
        )

    def _persisted_assignment(self, attempt: Attempt) -> DispatchAssignment:
        if attempt.worker_profile_id is None or attempt.worker_endpoint_id is None:
            raise RuntimeError(f"Attempt {attempt.attempt_id} has no persisted assignment")
        profile = next(
            (
                profile
                for profile in self._dispatcher.profiles
                if profile.worker_profile_id == attempt.worker_profile_id
            ),
            None,
        )
        endpoint = next(
            (
                endpoint
                for endpoint in self._dispatcher.endpoints
                if endpoint.worker_endpoint_id == attempt.worker_endpoint_id
            ),
            None,
        )
        if profile is None or endpoint is None:
            raise RuntimeError(f"Attempt {attempt.attempt_id} assignment is not configured")
        return DispatchAssignment(profile, endpoint)


def _single_slot_endpoint(endpoint: WorkerEndpoint) -> WorkerEndpoint:
    if endpoint.capacity == 1:
        return endpoint
    return WorkerEndpoint(
        endpoint.name,
        endpoint.worker_kind,
        endpoint.endpoint_type,
        endpoint.endpoint_ref,
        1,
        endpoint.status,
        endpoint.worker_endpoint_id,
    )


def _capacity_usage(active: Iterable[_ActiveExecution]) -> CapacityUsage:
    projects: dict[ID, int] = {}
    runs: dict[ID, int] = {}
    profiles: dict[ID, int] = {}
    endpoints: dict[ID, int] = {}
    items: tuple[_ActiveExecution, ...] = tuple(active)
    for item in items:
        projects[item.project_id] = projects.get(item.project_id, 0) + 1
        runs[item.work.run_id] = runs.get(item.work.run_id, 0) + 1
        profile_id = item.assignment.profile.worker_profile_id
        endpoint_id = item.assignment.endpoint.worker_endpoint_id
        profiles[profile_id] = profiles.get(profile_id, 0) + 1
        endpoints[endpoint_id] = endpoints.get(endpoint_id, 0) + 1
    return CapacityUsage(len(items), projects, runs, profiles, endpoints)


def _may_write_workspace(
    node: PlanNode,
    profiles: tuple[WorkerProfile, ...],
) -> bool:
    if any(capability.name == "workspace.write" for capability in node.required_capabilities):
        return True
    return any(
        node.required_capabilities.issubset(profile.capabilities)
        and any(capability.name == "workspace.write" for capability in profile.capabilities)
        for profile in profiles
    )


def _active_may_write_workspace(
    active: _ActiveExecution,
    profiles: tuple[WorkerProfile, ...],
    uow: UnitOfWork,
) -> bool:
    run = uow.states.get_run(active.work.run_id)
    if run is None:
        return False
    plan = uow.states.get_plan_revision(run.plan_revision_id)
    if plan is None:
        return False
    node = next(
        (item for item in plan.nodes if item.plan_node_id == active.attempt.plan_node_id),
        None,
    )
    return node is not None and _may_write_workspace(node, profiles)
