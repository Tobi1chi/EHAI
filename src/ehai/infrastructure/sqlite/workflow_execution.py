"""Independent workflow Run, step, invocation and wait tables, in one owning UoW."""

import sqlite3
from typing import Literal

from ehai import JsonValue, json_loads
from ehai.application.workflow_execution_models import WorkflowExecutionRecord
from ehai.application.workflow_models import WorkflowModel
from ehai.application.workflow_recording import WorkflowRecordKind

_TABLES = {
    "run": ("workflow_execution_runs", "workflow_run_id"),
    "step": ("workflow_step_executions", "step_execution_id"),
    "invocation": ("workflow_invocations", "invocation_id"),
    "wait": ("workflow_waits", "wait_id"),
}


class SQLiteWorkflowExecutionRecords:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def execution_record(self, run_id: str) -> WorkflowExecutionRecord | None:
        row = self.connection.execute(
            "SELECT snapshot_json FROM workflow_execution_runs WHERE workflow_run_id=?",
            (run_id,),
        ).fetchone()
        return None if row is None else WorkflowExecutionRecord.model_validate_json(row[0])

    def execution_children(
        self,
        kind: Literal["step", "invocation", "wait"],
        run_id: str,
    ) -> list[dict[str, JsonValue]]:
        table, _ = _TABLES[kind]
        rows = self.connection.execute(
            f"SELECT snapshot_json FROM {table} WHERE workflow_run_id=? ORDER BY rowid",
            (run_id,),
        )
        result = []
        for row in rows:
            document = json_loads(row[0])
            if not isinstance(document, dict):
                raise RuntimeError("Workflow execution record must be an object")
            result.append(document)
        return result

    def save_execution_record(
        self,
        kind: WorkflowRecordKind,
        record_id: str,
        record: WorkflowModel,
    ) -> None:
        table, id_column = _TABLES[kind]
        document = record.model_dump(mode="json")
        if document[id_column] != record_id:
            raise ValueError("Execution record identity does not match")
        columns = [id_column]
        if kind == "run":
            columns += ["project_id"]
        else:
            columns += ["workflow_run_id"]
            if kind == "step":
                columns += ["node_id", "execution_index"]
            else:
                columns += ["step_execution_id"]
                if kind == "invocation":
                    columns += ["idempotency_key"]
        values = [document[column] for column in columns] + [record.model_dump_json()]
        self.connection.execute(
            f"INSERT INTO {table} ({','.join(columns)},snapshot_json) "
            f"VALUES ({','.join('?' for _ in values)}) "
            f"ON CONFLICT({id_column}) DO UPDATE SET snapshot_json=excluded.snapshot_json",
            values,
        )
