"""Split Workflow Run storage without inventing historical execution traces."""

import sqlite3

WORKFLOW_EXECUTION_MIGRATION = (
    """CREATE TABLE workflow_execution_runs (
        workflow_run_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        CHECK(json_extract(snapshot_json,'$.workflow_run_id') = workflow_run_id),
        CHECK(json_extract(snapshot_json,'$.project_id') = project_id)
    )""",
    "CREATE INDEX workflow_execution_project ON workflow_execution_runs(project_id)",
    """CREATE TABLE workflow_step_executions (
        step_execution_id TEXT PRIMARY KEY,
        workflow_run_id TEXT NOT NULL REFERENCES workflow_execution_runs(workflow_run_id),
        node_id TEXT NOT NULL,
        execution_index INTEGER NOT NULL CHECK(execution_index > 0),
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        UNIQUE(workflow_run_id, node_id, execution_index),
        UNIQUE(workflow_run_id, step_execution_id),
        CHECK(json_extract(snapshot_json,'$.step_execution_id') = step_execution_id),
        CHECK(json_extract(snapshot_json,'$.workflow_run_id') = workflow_run_id),
        CHECK(json_extract(snapshot_json,'$.node_id') = node_id),
        CHECK(json_extract(snapshot_json,'$.execution_index') = execution_index)
    )""",
    """CREATE TABLE workflow_invocations (
        invocation_id TEXT PRIMARY KEY,
        workflow_run_id TEXT NOT NULL,
        step_execution_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        FOREIGN KEY(workflow_run_id, step_execution_id)
            REFERENCES workflow_step_executions(workflow_run_id, step_execution_id),
        UNIQUE(step_execution_id, idempotency_key),
        CHECK(json_extract(snapshot_json,'$.invocation_id') = invocation_id),
        CHECK(json_extract(snapshot_json,'$.workflow_run_id') = workflow_run_id),
        CHECK(json_extract(snapshot_json,'$.step_execution_id') = step_execution_id),
        CHECK(json_extract(snapshot_json,'$.idempotency_key') = idempotency_key)
    )""",
    "CREATE INDEX workflow_invocation_run ON workflow_invocations(workflow_run_id)",
    """CREATE TABLE workflow_waits (
        wait_id TEXT PRIMARY KEY,
        workflow_run_id TEXT NOT NULL,
        step_execution_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        FOREIGN KEY(workflow_run_id, step_execution_id)
            REFERENCES workflow_step_executions(workflow_run_id, step_execution_id),
        CHECK(json_extract(snapshot_json,'$.wait_id') = wait_id),
        CHECK(json_extract(snapshot_json,'$.workflow_run_id') = workflow_run_id),
        CHECK(json_extract(snapshot_json,'$.step_execution_id') = step_execution_id)
    )""",
    "CREATE INDEX workflow_wait_run ON workflow_waits(workflow_run_id)",
)


def migrate_workflow_execution_records(connection: sqlite3.Connection) -> None:
    """Called inside migration 20's transaction, including removal of the old Run rows."""
    from ehai.application.workflow_models import WorkflowRun
    from ehai.application.workflow_recording import split_workflow_run

    rows = connection.execute(
        "SELECT snapshot_json FROM workflow_documents WHERE kind='run' ORDER BY rowid"
    ).fetchall()
    for row in rows:
        run = WorkflowRun.model_validate_json(row[0])
        record = split_workflow_run(run, origin="legacy_snapshot")
        connection.execute(
            "INSERT INTO workflow_execution_runs VALUES (?,?,?)",
            (record.workflow_run_id, record.project_id, record.model_dump_json()),
        )
    # Event history, receipts, task provenance and original UUIDs are unchanged.
    connection.execute("DELETE FROM workflow_documents WHERE kind='run'")
