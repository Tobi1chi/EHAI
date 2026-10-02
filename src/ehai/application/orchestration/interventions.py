"""Suspend blocked Attempts and resume them with replies."""

from __future__ import annotations

from ehai import ID, normalize_id
from ehai.application.interventions import (
    WorkerBlocker,
    list_interventions,
    open_intervention,
    reply_intervention,
)
from ehai.application.orchestration.common import (
    OrchestrationError,
    WorkerEventReceipt,
    _replace_node,
    _required_attempt,
    _required_node,
    _required_plan,
    _required_run,
)
from ehai.application.orchestration.host import OrchestratorHost
from ehai.application.orchestration.readiness import (
    ready_nodes,
)
from ehai.application.ports import UnitOfWork
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.planning import (
    PlanNodeStatus,
)


class InterventionsMixin:
    """Suspend blocked Attempts and resume them with replies."""

    def block_attempt(
        self: OrchestratorHost,
        attempt_id: ID,
        blocker: WorkerBlocker,
        *,
        worker_event_receipts: tuple[WorkerEventReceipt, ...] = (),
    ) -> Run:
        """Settle one execution and retain its route while awaiting a user reply."""
        with self._uow_factory() as uow:
            attempt = _required_attempt(uow, attempt_id)
            run = _required_run(uow, attempt.run_id)
            if attempt.status is not AttemptStatus.RUNNING:
                return run
            plan = _required_plan(uow, run.run_id)
            node = _required_node(plan, attempt.plan_node_id)
            intervention = open_intervention(uow, attempt_id, blocker)
            interrupted = self._apply_worker_event_receipts(
                uow,
                attempt.interrupt(blocker.reason, at=self._clock()),
                worker_event_receipts,
            )
            uow.states.put_attempt(interrupted)
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, node.suspend()))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_SUSPENDED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "attempt_id": attempt_id,
                        "reason": blocker.reason,
                        "intervention_id": intervention["intervention_id"],
                    },
                )
            )
            uow.events.append(
                self._event(
                    EventType.ATTEMPT_INTERRUPTED,
                    run,
                    attempt_id,
                    {
                        "attempt_id": attempt_id,
                        "reason": blocker.reason,
                        "intervention_id": intervention["intervention_id"],
                    },
                )
            )
            uow.commit()
            return run

    def record_intervention_reply(
        self: OrchestratorHost,
        uow: UnitOfWork,
        intervention_id: ID,
        request_token: str,
        *,
        actor: str,
        message: str,
    ) -> Run:
        """Release only the explicitly answered node; retain all approved boundaries."""
        reply = reply_intervention(uow, intervention_id, request_token, actor, message)
        run_id, node_id = reply.get("run_id"), reply.get("plan_node_id")
        if not isinstance(run_id, str) or not isinstance(node_id, str):
            raise OrchestrationError("Intervention reply is missing its execution scope")
        run = _required_run(uow, normalize_id(run_id))
        plan = _required_plan(uow, run.run_id)
        node = _required_node(plan, normalize_id(node_id))
        if node.status is PlanNodeStatus.SUSPENDED:
            attempts = [
                item
                for item in uow.states.list_attempts(run.run_id)
                if item.plan_node_id == node.plan_node_id
            ]
            if not attempts or attempts[-1].attempt_id != reply.get("attempt_id"):
                raise OrchestrationError("Reply does not address the node's current blocker")
            if any(
                item.get("plan_node_id") == node.plan_node_id and item.get("status") == "open"
                for item in list_interventions(uow.events, run.run_id)
            ):
                raise OrchestrationError("The node still has an unanswered intervention")
            uow.states.put_execution_plan(run.run_id, _replace_node(plan, node.resume()))
            uow.events.append(
                self._event(
                    EventType.PLAN_NODE_RECOVERED,
                    run,
                    node.plan_node_id,
                    {
                        "plan_node_id": node.plan_node_id,
                        "intervention_id": intervention_id,
                        "recovery": "human_reply",
                    },
                )
            )
        return run

    def waiting_for_intervention(
        self: OrchestratorHost, run_id: ID, *, only_if_idle: bool = False
    ) -> bool:
        with self._uow_factory() as uow:
            run = _required_run(uow, run_id)
            if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
                return False
            plan = _required_plan(uow, run.run_id)
            if only_if_idle and (
                ready_nodes(plan)
                or any(
                    node.status in {PlanNodeStatus.READY, PlanNodeStatus.STALLED}
                    for node in plan.nodes
                )
            ):
                return False
            return any(node.status is PlanNodeStatus.SUSPENDED for node in plan.nodes)

    def waiting_for_input(
        self: OrchestratorHost, run_id: ID, *, only_if_idle: bool = False
    ) -> bool:
        return self.waiting_for_intervention(
            run_id, only_if_idle=only_if_idle
        ) or self.waiting_for_human(run_id, only_if_idle=only_if_idle)
