"""Project-only workflow requests projected from the same durable event snapshot."""

from ehai import ID, JsonValue, json_dumps, normalize_id, parse_utc_datetime
from ehai.application.inbox import (
    AuthorizationLimit,
    InboxAction,
    InboxAuthorization,
    InboxItem,
    InboxOwner,
    inbox_authorization,
)
from ehai.application.ports import ReadSession
from ehai.application.workflow_execution_models import (
    WorkflowExecutionRecord,
    WorkflowInvocationRecord,
    WorkflowWaitRecord,
)
from ehai.application.workflow_models import WorkflowRun
from ehai.domain.events import EventType
from ehai.domain.goal import Project


def workflow_inbox_items(session: ReadSession, project_id: ID | None) -> list[InboxItem]:
    latest: dict[str, WorkflowRun] = {}
    executions: dict[str, dict[str, JsonValue]] = {}
    for stored in session.events.list_events():
        if stored.event.type is EventType.WORKFLOW_RUN_CHANGED:
            run = WorkflowRun.model_validate(stored.event.payload)
            latest[run.workflow_run_id] = run
        elif stored.event.type is EventType.WORKFLOW_EXECUTION_CHANGED:
            executions[str(stored.event.correlation_id)] = stored.event.payload
    items: list[InboxItem] = []
    for run in latest.values():
        if project_id is not None and run.project_id != project_id:
            continue
        if run.workflow != "life.capture":
            continue
        project = session.states.get_project(normalize_id(run.project_id))
        if project is None:
            continue
        pending = run.status == "awaiting_confirmation"
        items.append(
            InboxItem(
                kind="workflow_confirmation",
                request_id=normalize_id(run.workflow_run_id),
                owner=_owner(project, run.workflow_run_id),
                source_status=run.status,
                pending=pending,
                actionable=pending,
                unavailable_reason=None if pending else "Workflow decision is already complete",
                created_at=run.created_at,
                first_observed_at=None,
                question="Confirm these life tasks before creating them",
                evidence_summary=run.source_text,
                evidence=(),
                request_token=str(run.version),
                actions=(
                    InboxAction(
                        operation="decide-workflow",
                        label="Approve or reject proposed tasks",
                        effect="Approve stores tasks; reject stores none. Tasks are not executed.",
                        input_fields=("decision", "actor", "reason", "idempotency_key"),
                        arguments={
                            "workflow_run_id": run.workflow_run_id,
                            "expected_version": run.version,
                        },
                    ),
                )
                if pending
                else (),
                next_step="Read proposed_tasks on the workflow run, then approve or reject"
                if pending
                else "Read the workflow result and its task_ids",
                disposition=None if run.decision is None else run.decision.model_dump(mode="json"),
                authorization=inbox_authorization(
                    {
                        "proposed_tasks": [
                            task.model_dump(mode="json") for task in run.proposed_tasks
                        ],
                    }
                ),
            )
        )
    for payload in executions.values():
        record = WorkflowExecutionRecord.model_validate(payload["run"])
        if project_id is not None and record.project_id != project_id:
            continue
        project = session.states.get_project(normalize_id(record.project_id))
        if project is None:
            continue
        items.extend(_custom_items(project, record, payload))
    return items


def _owner(project: Project, workflow_run_id: str) -> InboxOwner:
    return InboxOwner(
        project_id=project.project_id,
        project_name=project.name,
        goal_id=None,
        goal_objective=None,
        run_id=None,
        run_status=None,
        plan_node_id=None,
        node_title=None,
        attempt_id=None,
        workflow_run_id=normalize_id(workflow_run_id),
    )


def _custom_items(
    project: Project, record: WorkflowExecutionRecord, payload: dict[str, JsonValue]
) -> list[InboxItem]:
    """Human steps of a custom workflow, and connector results it cannot continue past."""
    waits, invocations = payload.get("waits"), payload.get("invocations")
    items: list[InboxItem] = []
    for raw in waits if isinstance(waits, list) else []:
        wait = WorkflowWaitRecord.model_validate(raw)
        if wait.kind != "human":
            continue
        pending = wait.status == "waiting" and record.status == "waiting"
        question = wait.condition.get("question")
        items.append(
            InboxItem(
                kind="workflow_confirmation",
                request_id=normalize_id(wait.wait_id),
                owner=_owner(project, record.workflow_run_id),
                source_status=wait.status,
                pending=pending,
                actionable=pending,
                unavailable_reason=None if pending else "Workflow decision is already complete",
                created_at=wait.recorded_at,
                first_observed_at=None,
                question=question if isinstance(question, str) else "Decide this workflow step",
                evidence_summary=json_dumps(wait.condition.get("subject")),
                evidence=(),
                request_token=str(record.version),
                actions=(
                    InboxAction(
                        operation="decide-workflow-step",
                        label="Approve or reject this step",
                        effect="The workflow continues on the approve or reject path of the step.",
                        input_fields=("decision", "actor", "reason", "idempotency_key"),
                        arguments={
                            "workflow_run_id": record.workflow_run_id,
                            "expected_version": record.version,
                        },
                    ),
                )
                if pending
                else (),
                next_step="Read the subject, then approve or reject"
                if pending
                else "Read the workflow execution",
                disposition=wait.resolution,
                authorization=_wait_authorization(wait),
            )
        )
    for raw in invocations if isinstance(invocations, list) else []:
        invocation = WorkflowInvocationRecord.model_validate(raw)
        if invocation.status != "unknown" or invocation.connector_call_id is None:
            continue
        pending = record.status == "waiting"
        items.append(
            InboxItem(
                kind="workflow_reconciliation",
                request_id=normalize_id(invocation.invocation_id),
                owner=_owner(project, record.workflow_run_id),
                source_status=invocation.status,
                pending=pending,
                actionable=pending,
                unavailable_reason=None if pending else "Workflow run is no longer waiting",
                created_at=invocation.recorded_at,
                first_observed_at=None,
                question="The connector could not confirm whether this call took effect",
                evidence_summary=invocation.error,
                evidence=(),
                request_token=None,
                actions=(
                    InboxAction(
                        operation="reconcile-connector-call",
                        label="Ask the connector to verify the call",
                        effect="The original worker checks the result; the write is not replayed.",
                        input_fields=("idempotency_key",),
                        arguments={"call_id": invocation.connector_call_id},
                    ),
                )
                if pending
                else (),
                next_step="Reconcile the connector call; the workflow continues on its result",
                disposition=None,
                authorization=inbox_authorization(
                    {
                        "call_id": invocation.connector_call_id,
                        "action": invocation.action.name,
                    }
                ),
            )
        )
    return items


def _wait_authorization(wait: WorkflowWaitRecord) -> InboxAuthorization:
    subject = wait.condition.get("subject")
    grant = wait.condition.get("grant")
    limit = None
    expires_at = None
    if isinstance(grant, dict):
        raw_limit = grant.get("limit")
        if isinstance(raw_limit, dict):
            kind, value, unit = (raw_limit.get(k) for k in ("kind", "value", "unit"))
            if kind in {"amount", "count"} and isinstance(value, (int, float)):
                limit = AuthorizationLimit(
                    "amount" if kind == "amount" else "count",
                    value,
                    unit if isinstance(unit, str) else None,
                )
    deadline = wait.condition.get("deadline")
    authorization = None if wait.resolution is None else wait.resolution.get("authorization")
    if isinstance(authorization, dict):
        approved_expiry = authorization.get("expires_at")
        expires_at = (
            parse_utc_datetime(approved_expiry) if isinstance(approved_expiry, str) else None
        )
    elif wait.status == "waiting" and isinstance(deadline, str):
        expires_at = parse_utc_datetime(deadline)
    return inbox_authorization(
        subject if isinstance(subject, dict) else {},
        limit,
        expires_at,
    )
