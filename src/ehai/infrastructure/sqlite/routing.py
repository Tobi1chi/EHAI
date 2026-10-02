"""Routing experiment documents and event facts share the core transaction."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from ehai import JsonValue, json_loads, normalize_id
from ehai.application.connector_models import ConnectorModel
from ehai.application.queries import QueryNotFoundError
from ehai.application.routing_store import RoutingDocumentKind, RoutingTransaction
from ehai.domain.events import Event
from ehai.infrastructure.sqlite.database import SQLiteDatabase
from ehai.infrastructure.sqlite.repository import SQLiteCommandReceiptStore, SQLiteEventLog


class SQLiteRoutingStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[RoutingTransaction]:
        connection = self.database.connect() if write else self.database._connect_read_only()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")
            yield _Transaction(connection)
            connection.commit()
        finally:
            connection.close()


class _Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection

    def get(self, kind: RoutingDocumentKind, entity_id: str) -> dict[str, JsonValue]:
        row = self.db.execute(
            "SELECT snapshot_json FROM routing_documents WHERE kind=? AND entity_id=?",
            (kind, entity_id),
        ).fetchone()
        if row is None:
            raise QueryNotFoundError("Routing " + kind, normalize_id(entity_id))
        return _document(row[0])

    def list(
        self, kind: RoutingDocumentKind, lab_id: str | None = None
    ) -> list[dict[str, JsonValue]]:
        return [
            _document(r[0])
            for r in self.db.execute(
                "SELECT snapshot_json FROM routing_documents "
                "WHERE kind=? AND (? IS NULL OR lab_id=?) ORDER BY rowid",
                (kind, lab_id, lab_id),
            )
        ]

    def save(self, kind: RoutingDocumentKind, entity_id: str, document: ConnectorModel) -> None:
        value = document.model_dump(mode="json")
        self.db.execute(
            "INSERT INTO routing_documents VALUES (?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET snapshot_json=excluded.snapshot_json",
            (entity_id, kind, value["project_id"], value["lab_id"], document.model_dump_json()),
        )

    @property
    def receipts(self) -> SQLiteCommandReceiptStore:
        return SQLiteCommandReceiptStore(self.db)

    def emit(self, event: Event) -> None:
        SQLiteEventLog(self.db).append(event)


def _document(raw: str) -> dict[str, JsonValue]:
    value = json_loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError("Routing document must be an object")
    return value
