"""Project-only workflow requests projected from the same durable event snapshot."""

from ehai import ID, normalize_id
from ehai.application.inbox import InboxAction, InboxItem, InboxOwner
from ehai.application.ports import ReadSession
from ehai.application.workflow_models import WorkflowRun
from ehai.domain.events import EventType


def workflow_inbox_items(session: ReadSession, project_id: ID | None) -> list[InboxItem]:
    latest: dict[str, WorkflowRun] = {}
    for stored in session.events.list_events():
        if stored.event.type is EventType.WORKFLOW_RUN_CHANGED:
            run = WorkflowRun.model_validate(stored.event.payload)
            latest[run.workflow_run_id] = run
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
                owner=InboxOwner(
                    project_id=project.project_id,
                    project_name=project.name,
                    goal_id=None,
                    goal_objective=None,
                    run_id=None,
                    run_status=None,
                    plan_node_id=None,
                    node_title=None,
                    attempt_id=None,
                    workflow_run_id=normalize_id(run.workflow_run_id),
                ),
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
            )
        )
    return items
