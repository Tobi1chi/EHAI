"""Record local workflow effects in the owning business transaction.

Recorded times describe observation in that transaction, not per-step timing traces.
External connectors will need intent-before-I/O; no connector is invoked here.
"""

from hashlib import sha256
from typing import Literal, Protocol

from ehai import JsonValue, json_dumps, new_id, utc_now
from ehai.application.ports import StateConflictError
from ehai.application.workflow_execution_models import (
    WorkflowContractRef,
    WorkflowExecutionRecord,
    WorkflowInvocationRecord,
    WorkflowStepExecution,
    WorkflowWaitRecord,
)
from ehai.application.workflow_models import WorkflowModel, WorkflowRun

WorkflowRecordKind = Literal["run", "step", "invocation", "wait"]


class WorkflowRecordStore(Protocol):
    def execution_record(self, run_id: str) -> WorkflowExecutionRecord | None: ...
    def execution_children(
        self,
        kind: Literal["step", "invocation", "wait"],
        run_id: str,
    ) -> list[dict[str, JsonValue]]: ...
    def save_execution_record(
        self,
        kind: WorkflowRecordKind,
        record_id: str,
        record: WorkflowModel,
    ) -> None: ...


def builtin_definition(workflow: str) -> dict[str, JsonValue]:
    steps: list[JsonValue] = (
        ["validate", "confirm_if_requested", "create_tasks"]
        if workflow == "life.capture"
        else ["read_project_tasks", "save_review"]
    )
    return {"workflow": workflow, "version": 1, "steps": steps}


def split_workflow_run(
    run: WorkflowRun,
    *,
    origin: Literal["native", "legacy_snapshot"],
) -> WorkflowExecutionRecord:
    document = run.model_dump(mode="json")
    definition = builtin_definition(run.workflow) if origin == "native" else None
    return WorkflowExecutionRecord(
        workflow_run_id=run.workflow_run_id,
        project_id=run.project_id,
        definition=WorkflowContractRef(
            kind="workflow",
            name=run.workflow,
            version=str(run.workflow_version),
            digest=None
            if definition is None
            else sha256(json_dumps(definition).encode()).hexdigest(),
        ),
        definition_snapshot=definition,
        status=run.status,
        version=run.version,
        inputs={"source_text": document["source_text"], "tasks": document["proposed_tasks"]},
        result={key: document[key] for key in ("task_ids", "review", "decision")},
        trigger={
            key: document[key]
            for key in (
                "routine_snapshot",
                "scheduled_for",
                "coalesced_occurrences",
            )
        },
        recording_origin=origin,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def project_workflow_run(record: WorkflowExecutionRecord) -> WorkflowRun:
    """Keep the existing public life workflow response without duplicating stored Run state."""
    return WorkflowRun.model_validate(
        {
            "workflow_run_id": record.workflow_run_id,
            "project_id": record.project_id,
            "workflow": record.definition.name,
            "workflow_version": int(record.definition.version),
            "status": record.status,
            "version": record.version,
            "source_text": record.inputs["source_text"],
            "proposed_tasks": record.inputs["tasks"],
            **record.result,
            **record.trigger,
            "created_at": record.created_at,
            "completed_at": record.completed_at,
        }
    )


def record_workflow_run(store: WorkflowRecordStore, run: WorkflowRun) -> None:
    previous = store.execution_record(run.workflow_run_id)
    origin = "native" if previous is None else previous.recording_origin
    record = split_workflow_run(run, origin=origin)
    if previous is not None:
        # Pinned identity is never silently replaced by a newer installed definition.
        record = record.model_copy(
            update={
                "definition": previous.definition,
                "definition_snapshot": previous.definition_snapshot,
            }
        )
    store.save_execution_record("run", run.workflow_run_id, record)
    now = utc_now()

    def step(
        node: str,
        status: Literal["completed", "waiting", "skipped"],
        inputs: dict[str, JsonValue],
        output: dict[str, JsonValue] | None,
        *,
        local_action: str | None = None,
    ) -> WorkflowStepExecution:
        entry = WorkflowStepExecution(
            step_execution_id=str(new_id()),
            workflow_run_id=run.workflow_run_id,
            node_id=node,
            execution_index=1,
            contract=WorkflowContractRef(kind="step", name=node, version="1"),
            status=status,
            inputs=inputs,
            output=output,
            recorded_at=now,
            completed_at=None if status == "waiting" else now,
        )
        store.save_execution_record("step", entry.step_execution_id, entry)
        if local_action is not None:
            invocation = WorkflowInvocationRecord(
                invocation_id=str(new_id()),
                workflow_run_id=run.workflow_run_id,
                step_execution_id=entry.step_execution_id,
                kind="local",
                action=WorkflowContractRef(kind="action", name=local_action, version="1"),
                status="completed",
                idempotency_key=entry.step_execution_id,
                inputs=inputs,
                output=output,
                external_operation_ref=None,
                error=None,
                recorded_at=now,
                completed_at=now,
            )
            store.save_execution_record("invocation", invocation.invocation_id, invocation)
        return entry

    if previous is None:
        if run.workflow == "life.review":
            step("read_project_tasks", "completed", {"project_id": run.project_id}, None)
            step(
                "save_review",
                "completed",
                {"project_id": run.project_id},
                record.result,
                local_action="life.save_review",
            )
            return
        step("validate", "completed", record.inputs, {"valid": True})
        pending = run.status == "awaiting_confirmation"
        confirmation = step(
            "confirm_if_requested",
            "waiting" if pending else "skipped",
            record.inputs,
            None if pending else {"require_confirmation": False},
        )
        if pending:
            wait = WorkflowWaitRecord(
                wait_id=str(new_id()),
                workflow_run_id=run.workflow_run_id,
                step_execution_id=confirmation.step_execution_id,
                kind="human",
                status="waiting",
                condition={"expected_version": run.version, "question": "Confirm proposed tasks"},
                resolution=None,
                recorded_at=now,
                resolved_at=None,
            )
            store.save_execution_record("wait", wait.wait_id, wait)
            return
    else:
        if previous.status != "awaiting_confirmation" or run.decision is None:
            raise StateConflictError("Only a pending workflow decision may advance its records")
        decision = run.decision.model_dump(mode="json")
        steps = [
            WorkflowStepExecution.model_validate(s)
            for s in store.execution_children("step", run.workflow_run_id)
        ]
        existing_confirmation = next(
            (s for s in steps if s.node_id == "confirm_if_requested"), None
        )
        if existing_confirmation is None:
            # Legacy history had no step trace. Record only the newly observed decision.
            step("confirm_if_requested", "completed", record.inputs, decision)
        else:
            store.save_execution_record(
                "step",
                existing_confirmation.step_execution_id,
                existing_confirmation.model_copy(
                    update={
                        "status": "completed",
                        "output": decision,
                        "completed_at": now,
                    }
                ),
            )
        for raw in store.execution_children("wait", run.workflow_run_id):
            wait = WorkflowWaitRecord.model_validate(raw)
            if wait.status == "waiting":
                store.save_execution_record(
                    "wait",
                    wait.wait_id,
                    wait.model_copy(
                        update={
                            "status": "resolved",
                            "resolution": decision,
                            "resolved_at": now,
                        }
                    ),
                )
    step(
        "create_tasks",
        "completed" if run.status == "completed" else "skipped",
        record.inputs,
        {"task_ids": list(run.task_ids)},
        local_action="life.create_tasks" if run.status == "completed" else None,
    )
