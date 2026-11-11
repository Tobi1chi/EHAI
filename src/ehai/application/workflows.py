"""Project workflows: the two fixed life workflows and custom step graphs.

Life workflows are small and atomic, with no Worker, Git, model loop or external effects.
Custom workflows reach external services only through registered connector calls.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, TypeVar

from ehai import JsonValue, new_id, normalize_id, utc_now
from ehai.application.agent_delegation import AgentDelegation
from ehai.application.custom_workflow_models import (
    SaveWorkflowDefinitionRequest,
    StartCustomWorkflowRequest,
    WorkflowDefinitionBody,
    WorkflowDefinitionRevision,
)
from ehai.application.custom_workflows import (
    CustomWorkflowTransaction,
    decide_custom_run,
    definition_digest,
    poll_custom_run,
    start_custom_run,
    validate_definition,
    validate_inputs,
)
from ehai.application.idempotency import (
    IdempotencyConflictError,
    content_fingerprint,
    record_result,
    recorded_result,
)
from ehai.application.ports import CommandReceipt, CommandReceiptStore, StateConflictError
from ehai.application.queries import QueryNotFoundError
from ehai.application.workflow_agents import WorkflowAgents
from ehai.application.workflow_compatibility import WorkflowCompatibility
from ehai.application.workflow_execution_models import (
    WorkflowExecutionRecord,
    WorkflowExecutionView,
    WorkflowInvocationRecord,
    WorkflowStepExecution,
    WorkflowWaitRecord,
)
from ehai.application.workflow_models import (
    CreateRoutineRequest,
    DecideWorkflowRequest,
    LifeReview,
    LifeRoutine,
    LifeTask,
    RoutineSchedulerStatus,
    StartWorkflowRequest,
    UpdateLifeTaskRequest,
    UpdateRoutineRequest,
    WorkflowDecision,
    WorkflowDefinition,
    WorkflowModel,
    WorkflowRun,
)
from ehai.application.workflow_recording import record_workflow_run
from ehai.domain.events import Event, EventType

WorkflowEntity = Literal["run", "task", "routine"]
_RECEIPT_SCOPE = "workflow"
T = TypeVar("T", bound=WorkflowModel)


class WorkflowTransaction(CustomWorkflowTransaction, Protocol):
    def require_project(self, project_id: str) -> None: ...
    def get(self, kind: WorkflowEntity, entity_id: str) -> dict[str, JsonValue]: ...
    def list(self, kind: WorkflowEntity, project_id: str | None) -> list[dict[str, JsonValue]]: ...
    def save(self, kind: WorkflowEntity, entity_id: str, value: WorkflowModel) -> None: ...
    @property
    def receipts(self) -> CommandReceiptStore: ...
    def emit(self, event: Event) -> None: ...


def _receipt_conflict(receipt: CommandReceipt) -> Exception:
    del receipt
    return IdempotencyConflictError("idempotency_key already belongs to another workflow command")


class WorkflowStore(Protocol):
    def transaction(self, *, write: bool) -> AbstractContextManager[WorkflowTransaction]: ...


class WorkflowService:
    def __init__(self, store: WorkflowStore, delegation: AgentDelegation | None = None) -> None:
        self.store = store
        self.agents = WorkflowAgents(store, delegation)
        # Deliberately not configurable through CLI, environment, API or Routine inputs.
        self._compatibility = WorkflowCompatibility(enabled=False)
        self.scheduler_active = False
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None

    @staticmethod
    def definitions() -> list[WorkflowDefinition]:
        return [
            WorkflowDefinition(
                workflow="life.capture",
                description="Store source text and structured tasks, with optional confirmation.",
                steps=["validate", "confirm_if_requested", "create_tasks"],
            ),
            WorkflowDefinition(
                workflow="life.review",
                description="Persist a read-only snapshot of the project's current life tasks.",
                steps=["read_project_tasks", "save_review"],
            ),
        ]

    def scheduler_status(self) -> RoutineSchedulerStatus:
        return RoutineSchedulerStatus(
            active=self.scheduler_active,
            last_tick_at=self.last_tick_at,
            last_error=self.last_error,
        )

    def execution(self, run_id: str) -> WorkflowExecutionView:
        with self.store.transaction(write=False) as tx:
            return self._view(tx, run_id)

    def _view(self, tx: WorkflowTransaction, run_id: str) -> WorkflowExecutionView:
        record = tx.execution_record(run_id)
        if record is None:
            raise QueryNotFoundError("Workflow run", normalize_id(run_id))
        return WorkflowExecutionView(
            run=record,
            steps=[
                WorkflowStepExecution.model_validate(s)
                for s in tx.execution_children("step", run_id)
            ],
            invocations=[
                WorkflowInvocationRecord.model_validate(i)
                for i in tx.execution_children("invocation", run_id)
            ],
            waits=[
                WorkflowWaitRecord.model_validate(w) for w in tx.execution_children("wait", run_id)
            ],
            compatibility=self._compatibility.status(),
        )

    def executions(self, project_id: str) -> list[WorkflowExecutionRecord]:
        """Every workflow Run of the project, fixed and custom, as independent records."""
        with self.store.transaction(write=False) as tx:
            tx.require_project(project_id)
            return tx.execution_records(project_id)

    def definition(self, definition_id: str) -> WorkflowDefinitionRevision:
        with self.store.transaction(write=False) as tx:
            return tx.definition(definition_id)

    def list_definitions(self, project_id: str) -> list[WorkflowDefinitionRevision]:
        with self.store.transaction(write=False) as tx:
            tx.require_project(project_id)
            return tx.definitions(project_id)

    def save_definition(
        self, project_id: str, request: SaveWorkflowDefinitionRequest
    ) -> WorkflowDefinitionRevision:
        body = WorkflowDefinitionBody.model_validate(
            request.model_dump(exclude={"idempotency_key", "expected_revision"})
        )

        def action(tx: WorkflowTransaction) -> WorkflowDefinitionRevision:
            tx.require_project(project_id)
            latest = max(
                (d.revision for d in tx.definitions(project_id) if d.name == body.name),
                default=None,
            )
            if latest != request.expected_revision:
                raise StateConflictError(
                    f"Expected revision {request.expected_revision}; latest revision is {latest}"
                )
            validate_definition(
                body, {c.connector_id: c for c in tx.connectors.connections(project_id)}
            )
            revision = WorkflowDefinitionRevision(
                definition_id=str(new_id()),
                project_id=project_id,
                name=body.name,
                revision=1 if latest is None else latest + 1,
                digest=definition_digest(body),
                definition=body,
                created_at=utc_now(),
            )
            tx.save_definition(revision)
            return revision

        return self._command(
            "save_definition",
            project_id,
            request,
            request.idempotency_key,
            WorkflowDefinitionRevision,
            action,
        )

    def start_custom(
        self, definition_id: str, request: StartCustomWorkflowRequest
    ) -> WorkflowExecutionView:
        def action(tx: WorkflowTransaction) -> WorkflowExecutionView:
            revision = tx.definition(definition_id)
            record = start_custom_run(tx, revision, request.inputs, {"kind": "manual"}, utc_now())
            return self._view(tx, record.workflow_run_id)

        return self._command(
            "start_custom",
            definition_id,
            request,
            request.idempotency_key,
            WorkflowExecutionView,
            action,
        )

    def decide_step(self, run_id: str, request: DecideWorkflowRequest) -> WorkflowExecutionView:
        def action(tx: WorkflowTransaction) -> WorkflowExecutionView:
            record = tx.execution_record(run_id)
            if record is None:
                raise QueryNotFoundError("Workflow run", normalize_id(run_id))
            decide_custom_run(tx, record, request, utc_now())
            return self._view(tx, run_id)

        return self._command(
            "decide_step", run_id, request, request.idempotency_key, WorkflowExecutionView, action
        )

    def get(self, kind: WorkflowEntity, entity_id: str, model: type[T]) -> T:
        with self.store.transaction(write=False) as tx:
            return model.model_validate(tx.get(kind, entity_id))

    def list_records(self, kind: WorkflowEntity, project_id: str, model: type[T]) -> list[T]:
        with self.store.transaction(write=False) as tx:
            tx.require_project(project_id)
            return [model.model_validate(item) for item in tx.list(kind, project_id)]

    def _command(
        self,
        operation: str,
        target: str,
        request: WorkflowModel,
        key: str,
        model: type[T],
        action: Callable[[WorkflowTransaction], T],
    ) -> T:
        fingerprint = content_fingerprint(
            {
                "operation": operation,
                "target": target,
                "body": request.model_dump(mode="json"),
            }
        )
        with self.store.transaction(write=True) as tx:
            recorded = recorded_result(
                tx.receipts,
                key,
                operation,
                fingerprint,
                conflict=_receipt_conflict,
                scope=_RECEIPT_SCOPE,
            )
            if recorded is not None:
                return model.model_validate(recorded)
            result = action(tx)
            record_result(
                tx.receipts,
                key,
                operation,
                fingerprint,
                result.model_dump(mode="json"),
                created_at=utc_now(),
                scope=_RECEIPT_SCOPE,
            )
            return result

    def start(self, project_id: str, request: StartWorkflowRequest) -> WorkflowRun:
        def action(tx: WorkflowTransaction) -> WorkflowRun:
            tx.require_project(project_id)
            return self._run(tx, project_id, request, utc_now())

        return self._command(
            "start", project_id, request, request.idempotency_key, WorkflowRun, action
        )

    def _run(
        self,
        tx: WorkflowTransaction,
        project_id: str,
        request: StartWorkflowRequest,
        now: datetime,
        routine: LifeRoutine | None = None,
        occurrences: int = 0,
    ) -> WorkflowRun:
        waiting = request.workflow == "life.capture" and request.require_confirmation
        run = WorkflowRun(
            workflow_run_id=str(new_id()),
            project_id=project_id,
            workflow=request.workflow,
            status="awaiting_confirmation" if waiting else "completed",
            version=1,
            source_text=request.source_text,
            proposed_tasks=request.tasks,
            task_ids=[],
            review=None,
            decision=None,
            routine_snapshot=routine,
            scheduled_for=None if routine is None else routine.next_due_at,
            coalesced_occurrences=occurrences,
            created_at=now,
            completed_at=None if waiting else now,
        )
        if not waiting:
            if request.workflow == "life.capture":
                run = run.model_copy(update={"task_ids": self._create_tasks(tx, run, now)})
            else:
                tasks = [LifeTask.model_validate(t) for t in tx.list("task", project_id)]
                review = LifeReview(
                    as_of=now,
                    open_tasks=[t for t in tasks if t.status == "open"],
                    overdue_task_ids=[
                        t.task_id
                        for t in tasks
                        if t.status == "open" and t.due_at is not None and t.due_at < now
                    ],
                    done_count=sum(t.status == "done" for t in tasks),
                    cancelled_count=sum(t.status == "cancelled" for t in tasks),
                )
                run = run.model_copy(update={"review": review})
        self._save_run(tx, run)
        return run

    def _create_tasks(
        self,
        tx: WorkflowTransaction,
        run: WorkflowRun,
        now: datetime,
    ) -> list[str]:
        ids: list[str] = []
        for proposal in run.proposed_tasks:
            task = LifeTask(
                task_id=str(new_id()),
                project_id=run.project_id,
                workflow_run_id=run.workflow_run_id,
                title=proposal.title,
                due_at=proposal.due_at,
                status="open",
                version=1,
                created_at=now,
                updated_at=now,
            )
            tx.save("task", task.task_id, task)
            self._event(tx, EventType.LIFE_TASK_CHANGED, task.task_id, task)
            ids.append(task.task_id)
        return ids

    def decide(self, run_id: str, request: DecideWorkflowRequest) -> WorkflowRun:
        def action(tx: WorkflowTransaction) -> WorkflowRun:
            run = WorkflowRun.model_validate(tx.get("run", run_id))
            self._version(run.version, request.expected_version)
            if run.status != "awaiting_confirmation":
                raise StateConflictError("Workflow run is no longer awaiting confirmation")
            now = utc_now()
            task_ids = self._create_tasks(tx, run, now) if request.decision == "approve" else []
            run = run.model_copy(
                update={
                    "status": "completed" if request.decision == "approve" else "rejected",
                    "version": run.version + 1,
                    "task_ids": task_ids,
                    "completed_at": now,
                    "decision": WorkflowDecision(
                        decision=request.decision,
                        actor=request.actor,
                        reason=request.reason,
                        decided_at=now,
                    ),
                }
            )
            self._save_run(tx, run)
            return run

        return self._command(
            "decide", run_id, request, request.idempotency_key, WorkflowRun, action
        )

    def update_task(self, task_id: str, request: UpdateLifeTaskRequest) -> LifeTask:
        def action(tx: WorkflowTransaction) -> LifeTask:
            task = LifeTask.model_validate(tx.get("task", task_id))
            self._version(task.version, request.expected_version)
            task = task.model_copy(
                update={
                    "title": request.title,
                    "due_at": request.due_at,
                    "status": request.status,
                    "version": task.version + 1,
                    "updated_at": utc_now(),
                }
            )
            tx.save("task", task_id, task)
            self._event(tx, EventType.LIFE_TASK_CHANGED, task_id, task)
            return task

        return self._command(
            "update_task", task_id, request, request.idempotency_key, LifeTask, action
        )

    def create_routine(self, project_id: str, request: CreateRoutineRequest) -> LifeRoutine:
        def action(tx: WorkflowTransaction) -> LifeRoutine:
            tx.require_project(project_id)
            now = utc_now()
            routine = LifeRoutine.model_validate(
                {
                    "routine_id": str(new_id()),
                    "project_id": project_id,
                    "name": request.name,
                    "interval_seconds": request.interval_seconds,
                    "next_due_at": request.next_due_at.astimezone(UTC),
                    "enabled": request.enabled,
                    "version": 1,
                    "last_workflow_run_id": None,
                    "created_at": now,
                    "updated_at": now,
                    **self._binding(tx, project_id, request),
                }
            )
            self._save_routine(tx, routine)
            return routine

        return self._command(
            "create_routine",
            project_id,
            request,
            request.idempotency_key,
            LifeRoutine,
            action,
        )

    def update_routine(self, routine_id: str, request: UpdateRoutineRequest) -> LifeRoutine:
        def action(tx: WorkflowTransaction) -> LifeRoutine:
            routine = LifeRoutine.model_validate(tx.get("routine", routine_id))
            self._version(routine.version, request.expected_version)
            routine = routine.model_copy(
                update={
                    "name": request.name,
                    "interval_seconds": request.interval_seconds,
                    "next_due_at": request.next_due_at.astimezone(UTC),
                    "enabled": request.enabled,
                    "version": routine.version + 1,
                    "updated_at": utc_now(),
                    **self._binding(tx, routine.project_id, request),
                }
            )
            self._save_routine(tx, routine)
            return routine

        return self._command(
            "update_routine",
            routine_id,
            request,
            request.idempotency_key,
            LifeRoutine,
            action,
        )

    @staticmethod
    def _binding(
        tx: WorkflowTransaction, project_id: str, request: CreateRoutineRequest
    ) -> dict[str, JsonValue]:
        """The routine's workflow fields; a custom revision is checked against its inputs."""
        if request.workflow_definition_id is None:
            return {
                "workflow": "life.review",
                "workflow_version": 1,
                "workflow_definition_id": None,
                "inputs": None,
                "missed_run_policy": None,
            }
        revision = tx.definition(str(normalize_id(request.workflow_definition_id)))
        if revision.project_id != project_id:
            raise StateConflictError("The workflow definition belongs to another project")
        assert request.inputs is not None
        validate_inputs(revision.definition.inputs_schema, request.inputs)
        return {
            "workflow": "custom",
            "workflow_version": revision.revision,
            "workflow_definition_id": revision.definition_id,
            "inputs": request.inputs,
            "missed_run_policy": request.missed_run_policy,
        }

    def tick(self) -> None:
        """One transaction per due routine or ready custom Run; each is indivisible."""
        with self.store.transaction(write=False) as tx:
            routines = [LifeRoutine.model_validate(r) for r in tx.list("routine", None)]
        now = utc_now()
        for candidate in routines:
            if not candidate.enabled or candidate.next_due_at > now:
                continue
            with self.store.transaction(write=True) as tx:
                routine = LifeRoutine.model_validate(tx.get("routine", candidate.routine_id))
                if not routine.enabled or routine.next_due_at > now:
                    continue
                occurrences = (
                    int((now - routine.next_due_at).total_seconds()) // routine.interval_seconds + 1
                )
                next_due = routine.next_due_at + timedelta(
                    seconds=occurrences * routine.interval_seconds
                )
                update: dict[str, object] = {}
                if routine.workflow == "custom":
                    run_id = self._routine_custom_run(tx, routine, occurrences, now)
                    if run_id is None:
                        update["skipped_occurrences"] = routine.skipped_occurrences + occurrences
                    else:
                        update["last_workflow_run_id"] = run_id
                else:
                    run = self._run(
                        tx,
                        routine.project_id,
                        StartWorkflowRequest(idempotency_key="routine", workflow="life.review"),
                        now,
                        routine,
                        occurrences,
                    )
                    update["last_workflow_run_id"] = run.workflow_run_id
                self._save_routine(
                    tx,
                    routine.model_copy(
                        update={
                            **update,
                            "next_due_at": next_due,
                            "version": routine.version + 1,
                            "updated_at": now,
                        }
                    ),
                )
        with self.store.transaction(write=False) as tx:
            ready = [
                run_id
                for run_id in tx.waiting_custom_runs()
                if poll_custom_run(tx, run_id, now, apply=False)
            ]
        for run_id in ready:
            with self.store.transaction(write=True) as tx:
                poll_custom_run(tx, run_id, utc_now(), apply=True)
        self.last_tick_at = now
        self.last_error = None

    @staticmethod
    def _routine_custom_run(
        tx: WorkflowTransaction, routine: LifeRoutine, occurrences: int, now: datetime
    ) -> str | None:
        """Start the bound revision, or return None when this occurrence is skipped."""
        if routine.last_workflow_run_id is not None:
            previous = tx.execution_record(routine.last_workflow_run_id)
            if previous is not None and previous.status in ("running", "waiting"):
                return None
        if routine.missed_run_policy == "skip" and now - routine.next_due_at > timedelta(
            seconds=60
        ):
            return None
        assert routine.workflow_definition_id is not None and routine.inputs is not None
        record = start_custom_run(
            tx,
            tx.definition(routine.workflow_definition_id),
            routine.inputs,
            {
                "kind": "routine",
                "routine_id": routine.routine_id,
                "routine_version": routine.version,
                "scheduled_for": routine.next_due_at.isoformat(),
                "coalesced_occurrences": occurrences,
            },
            now,
        )
        return record.workflow_run_id

    def _save_run(self, tx: WorkflowTransaction, run: WorkflowRun) -> None:
        record_workflow_run(tx, run)
        self._event(tx, EventType.WORKFLOW_RUN_CHANGED, run.workflow_run_id, run)

    def _save_routine(self, tx: WorkflowTransaction, routine: LifeRoutine) -> None:
        tx.save("routine", routine.routine_id, routine)
        self._event(tx, EventType.ROUTINE_CHANGED, routine.routine_id, routine)

    @staticmethod
    def _event(
        tx: WorkflowTransaction, kind: EventType, entity_id: str, value: WorkflowModel
    ) -> None:
        tx.emit(Event(type=kind, correlation_id=entity_id, payload=value.model_dump(mode="json")))

    @staticmethod
    def _version(current: int, expected: int) -> None:
        if current != expected:
            raise StateConflictError(f"Expected version {expected}; current version is {current}")
