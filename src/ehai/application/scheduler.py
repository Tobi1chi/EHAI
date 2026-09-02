"""Single-process P2 Scheduler and stable capability/capacity Dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta

from ehai import ID, utc_now
from ehai.application.async_runtime import RuntimeConnector, SingleSlotRuntime
from ehai.application.orchestrator import Orchestrator
from ehai.application.ports import UnitOfWork
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanNode
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import (
    WorkerEndpoint,
    WorkerEndpointStatus,
    WorkerProfile,
)


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


class ConcurrentRuntime:
    """Dispatch multiple queued Attempts concurrently within explicit capacities."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        orchestrator: Orchestrator,
        dispatcher: Dispatcher,
        connectors: Mapping[ID, RuntimeConnector],
    ) -> None:
        self._uow_factory = uow_factory
        self._orchestrator = orchestrator
        self._dispatcher = dispatcher
        self._connectors = dict(connectors)
        self._claim_owner = f"scheduler:{utc_now().timestamp()}"

    async def run_until_idle(self) -> tuple[Run, ...]:
        """Run until every dispatchable Run is terminal or only blocked work remains."""
        works = self._claim_all_work()
        tasks: dict[asyncio.Task[Run], _ActiveExecution] = {}
        terminal: dict[ID, Run] = {}
        while works or tasks:
            works.update(self._claim_all_work())
            scheduled = self._schedule(works, tasks)
            if not tasks:
                if not scheduled:
                    break
                continue
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                active = tasks.pop(task)
                run = task.result()
                if run.status is not RunStatus.RUNNING:
                    active.helper.finish_dispatch_work(active.work)
                    works.pop(active.work.run_id, None)
                    terminal[run.run_id] = run
        return tuple(terminal[key] for key in sorted(terminal))

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
            usage = _capacity_usage(tasks.values())
            candidate = self._next_assignment(works, tasks, usage)
            if candidate is None:
                return scheduled
            work, attempt, project_id, assignment = candidate
            request = self._orchestrator.start_queued_attempt(attempt.attempt_id)
            connector = self._connectors.get(assignment.endpoint.worker_endpoint_id)
            if connector is None:
                raise RuntimeError(
                    f"WorkerEndpoint {assignment.endpoint.worker_endpoint_id} has no Connector"
                )
            helper = SingleSlotRuntime(
                uow_factory=self._uow_factory,
                orchestrator=self._orchestrator,
                connector=connector,
                profile=assignment.profile,
                endpoint=_single_slot_endpoint(assignment.endpoint),
            )
            task = asyncio.create_task(helper.execute_started_request(request))
            tasks[task] = _ActiveExecution(work, request.attempt, project_id, assignment, helper)
            scheduled = True

    def _next_assignment(
        self,
        works: dict[ID, DispatchWork],
        tasks: dict[asyncio.Task[Run], _ActiveExecution],
        usage: CapacityUsage,
    ) -> tuple[DispatchWork, Attempt, ID, DispatchAssignment] | None:
        with self._uow_factory() as uow:
            for run_id in sorted(works):
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
                    write_capable = any(
                        capability.name == "workspace.write"
                        for capability in node.required_capabilities
                    )
                    workspace_conflict = write_capable and any(
                        active.work.run_id == run_id
                        and any(
                            capability.name == "workspace.write"
                            for capability in next(
                                node
                                for node in plan.nodes
                                if node.plan_node_id == active.attempt.plan_node_id
                            ).required_capabilities
                        )
                        for active in tasks.values()
                    )
                    decision = self._dispatcher.select(
                        node,
                        project_id=goal.project_id,
                        run_id=run_id,
                        usage=usage,
                        workspace_conflict=workspace_conflict,
                    )
                    if decision.assignment is not None:
                        return works[run_id], attempt, goal.project_id, decision.assignment
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
        )


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
