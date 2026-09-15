"""Explicit adoption of an advisory review, scoped to one active Attempt."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256

from ehai import ID, JsonValue, json_dumps, normalize_id, utc_now
from ehai.application.interventions import WorkerBlocker, intervention_id_for, list_interventions
from ehai.application.ports import CommandReceipt, StateConflictError, UnitOfWork
from ehai.application.queries import QueryService
from ehai.application.runtime_control import RuntimeControlService
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.planning import PlanNodeStatus


class TrajectorySuspensions:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        queries: QueryService,
        control: RuntimeControlService,
    ) -> None:
        self._uow = uow_factory
        self._queries = queries
        self._control = control
        self._tasks: dict[ID, asyncio.Task[None]] = {}

    async def suspend(
        self,
        attempt_id: ID,
        *,
        review_id: ID,
        through_sequence: int,
        idempotency_key: str,
        actor: str,
        reason: str,
    ) -> dict[str, JsonValue]:
        attempt_id, review_id = normalize_id(attempt_id), normalize_id(review_id)
        if type(through_sequence) is not int or through_sequence < 1:
            raise ValueError("through_sequence must identify the reviewed snapshot")
        if not actor.strip() or not reason.strip() or len(actor) > 1000 or len(reason) > 4000:
            raise ValueError("Provide a concise actor and reason for the suspension decision")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        request: dict[str, JsonValue] = {
            "attempt_id": attempt_id,
            "review_id": review_id,
            "through_sequence": through_sequence,
            "actor": actor.strip(),
            "reason": reason.strip(),
        }
        fingerprint = sha256(json_dumps(request).encode()).hexdigest()
        with self._uow() as uow:
            old = uow.command_receipts.get(idempotency_key)
            if old is not None and (
                old.command_name != "SuspendAttemptFromReview"
                or old.command_fingerprint != fingerprint
            ):
                raise StateConflictError(
                    "Idempotency key belongs to a different suspension request"
                )
            attempt = uow.states.get_attempt(attempt_id)
            if attempt is None:
                raise StateConflictError("Attempt does not exist")
            run_id = attempt.run_id
        if old is not None:
            # Do not re-send a retained request after a crash/unknown outcome.
            return self._result(attempt_id, review_id, fingerprint)
        reviews = [
            r
            for r in self._queries.list_trajectory_reviews(run_id)
            if r.get("attempt_id") == attempt_id and r.get("state") == "completed"
        ]
        if not reviews or reviews[-1].get("review_id") != review_id:
            raise StateConflictError("Use the latest completed review of this Attempt")
        review = reviews[-1]
        scope = review.get("scope")
        opinion = review.get("opinion")
        if (
            review.get("through_sequence") != through_sequence
            or review.get("payload_truncated")
            or not isinstance(scope, dict)
            or not isinstance(opinion, dict)
        ):
            raise StateConflictError("Review does not match the acknowledged snapshot")
        with self._uow() as uow:
            attempt = uow.states.get_attempt(attempt_id)
            run = uow.states.get_run(run_id)
            plan = uow.states.get_execution_plan(run_id)
            process = uow.states.get_active_process_revision(run_id)
            node = (
                None
                if plan is None
                else next(
                    (
                        n
                        for n in plan.nodes
                        if attempt is not None and n.plan_node_id == attempt.plan_node_id
                    ),
                    None,
                )
            )
            if (
                attempt is None
                or attempt.status is not AttemptStatus.RUNNING
                or run is None
                or run.status is not RunStatus.RUNNING
                or node is None
                or node.status is not PlanNodeStatus.RUNNING
                or review.get("worker_session_id") != attempt.agent_session_ref_id
                or scope.get("plan_node_id") != node.plan_node_id
                or scope.get("approved_plan_revision_id") != run.plan_revision_id
                or scope.get("process_revision_id") != attempt.process_revision_id
                or (
                    process is not None
                    and scope.get("process_revision_id") != process.process_revision_id
                )
                or scope.get("objective") != node.instruction
            ):
                raise StateConflictError("Review is stale: execution or graph ownership changed")
            if attempt_id in self._tasks:
                raise StateConflictError("This Attempt already has a suspension in progress")
            evidence = json_dumps(
                {"suspension_request": fingerprint, **request, "opinion": opinion}
            )
            blocker = WorkerBlocker(
                reason=f"Trajectory review adopted: {reason.strip()}",
                evidence=evidence,
                needed="Inspect retained progress and tool effects. Clarify or adjust the route "
                "within the approved boundary before resuming; this grants no new permissions.",
                kind="external_effects",
            )
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=idempotency_key,
                    command_name="SuspendAttemptFromReview",
                    command_fingerprint=fingerprint,
                    result={
                        **request,
                        "run_id": run_id,
                        "status": "requested",
                        "intervention_id": intervention_id_for(attempt_id),
                    },
                    created_at=utc_now(),
                )
            )
            uow.commit()

        async def stop() -> None:
            await self._control.suspend_attempt(attempt_id, blocker)

        task = asyncio.create_task(stop(), name=f"ehai-suspend-{attempt_id}")
        self._tasks[attempt_id] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                self._tasks.pop(attempt_id, None)
            else:
                task.add_done_callback(lambda done: self._completed(attempt_id, done))
        return self._result(attempt_id, review_id, fingerprint)

    def _completed(self, attempt_id: ID, task: asyncio.Task[None]) -> None:
        self._tasks.pop(attempt_id, None)
        if not task.cancelled():
            task.exception()

    def _result(self, attempt_id: ID, review_id: ID, fingerprint: str) -> dict[str, JsonValue]:
        with self._uow() as uow:
            attempt = uow.states.get_attempt(attempt_id)
            if attempt is None:
                raise StateConflictError("Suspension target no longer exists")
            intervention = next(
                (
                    r
                    for r in list_interventions(uow.events, attempt.run_id)
                    if r.get("attempt_id") == attempt_id and fingerprint in str(r.get("evidence"))
                ),
                None,
            )
            plan = uow.states.get_execution_plan(attempt.run_id)
            node = (
                None
                if plan is None
                else next((n for n in plan.nodes if n.plan_node_id == attempt.plan_node_id), None)
            )
            return {
                "attempt_id": attempt_id,
                "run_id": attempt.run_id,
                "review_id": review_id,
                "status": "suspended"
                if intervention is not None
                else ("requested" if attempt.status is AttemptStatus.RUNNING else "not_suspended"),
                "attempt_status": attempt.status.value,
                "node_status": None if node is None else node.status.value,
                "intervention": intervention,
            }

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)
