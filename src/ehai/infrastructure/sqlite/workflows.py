"""Transactional persistence for fixed workflows and their life task records."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ehai import JsonValue, json_loads, normalize_id
from ehai.application.queries import QueryNotFoundError
from ehai.application.workflow_execution_models import WorkflowExecutionRecord
from ehai.application.workflow_models import WorkflowModel
from ehai.application.workflow_recording import project_workflow_run
from ehai.application.workflows import WorkflowEntity, WorkflowTransaction
from ehai.domain.events import Event
from ehai.infrastructure.sqlite.database import SQLiteDatabase
from ehai.infrastructure.sqlite.repository import SQLiteCommandReceiptStore, SQLiteEventLog
from ehai.infrastructure.sqlite.workflow_execution import SQLiteWorkflowExecutionRecords


class SQLiteWorkflowStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[WorkflowTransaction]:
        connection = self.database.connect() if write else self.database._connect_read_only()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
            yield _Transaction(connection)
            connection.commit()
        finally:
            connection.close()


class _Transaction(SQLiteWorkflowExecutionRecords):
    def require_project(self, project_id: str) -> None:
        if (
            self.connection.execute(
                "SELECT 1 FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            is None
        ):
            raise QueryNotFoundError("Project", normalize_id(project_id))

    def get(self, kind: WorkflowEntity, entity_id: str) -> dict[str, JsonValue]:
        if kind == "run":
            record = self.execution_record(entity_id)
            if record is None:
                raise QueryNotFoundError("Workflow run", normalize_id(entity_id))
            return project_workflow_run(record).model_dump(mode="json")
        row = self.connection.execute(
            "SELECT snapshot_json FROM workflow_documents WHERE kind=? AND entity_id=?",
            (kind, entity_id),
        ).fetchone()
        if row is None:
            raise QueryNotFoundError("Workflow " + kind, normalize_id(entity_id))
        return _document(row[0])

    def list(self, kind: WorkflowEntity, project_id: str | None) -> list[dict[str, JsonValue]]:
        if kind == "run":
            rows = self.connection.execute(
                "SELECT snapshot_json FROM workflow_execution_runs "
                "WHERE (? IS NULL OR project_id=?) ORDER BY rowid",
                (project_id, project_id),
            )
            return [
                project_workflow_run(
                    WorkflowExecutionRecord.model_validate_json(row[0]),
                ).model_dump(mode="json")
                for row in rows
            ]
        rows = self.connection.execute(
            "SELECT snapshot_json FROM workflow_documents "
            "WHERE kind=? AND (? IS NULL OR project_id=?) ORDER BY rowid",
            (kind, project_id, project_id),
        )
        return [_document(row[0]) for row in rows]

    def save(self, kind: WorkflowEntity, entity_id: str, value: WorkflowModel) -> None:
        if kind == "run":
            raise ValueError("Workflow Runs must be saved through independent execution records")
        document = value.model_dump(mode="json")
        self.connection.execute(
            "INSERT INTO workflow_documents VALUES (?,?,?,?) "
            "ON CONFLICT(kind,entity_id) DO UPDATE SET snapshot_json=excluded.snapshot_json",
            (kind, entity_id, document["project_id"], value.model_dump_json()),
        )

    @property
    def receipts(self) -> SQLiteCommandReceiptStore:
        return SQLiteCommandReceiptStore(self.connection)

    def emit(self, event: Event) -> None:
        SQLiteEventLog(self.connection).append(event)


def _document(raw: str) -> dict[str, JsonValue]:
    value = json_loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Stored workflow document must be an object")
    return value
