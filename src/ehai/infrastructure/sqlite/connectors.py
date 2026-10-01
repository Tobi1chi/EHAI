"""Connector records share the core event transaction, never the adapter's process."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256

from ehai import JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.connector_models import ConnectorCall, ConnectorConnection
from ehai.application.connectors import ConnectorTransaction
from ehai.application.ports import StateConflictError
from ehai.application.queries import QueryNotFoundError
from ehai.domain.events import Event
from ehai.infrastructure.sqlite.database import SQLiteDatabase
from ehai.infrastructure.sqlite.repository import SQLiteEventLog


class SQLiteConnectorStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[ConnectorTransaction]:
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

    def require_project(self, project_id: str) -> None:
        if (
            self.db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone()
            is None
        ):
            raise QueryNotFoundError("Project", normalize_id(project_id))

    def connection(self, connector_id: str) -> tuple[ConnectorConnection, str]:
        row = self.db.execute(
            "SELECT snapshot_json,credential_sha256 FROM connector_connections "
            "WHERE connector_id=?",
            (connector_id,),
        ).fetchone()
        if row is None:
            raise QueryNotFoundError("Connector", normalize_id(connector_id))
        return ConnectorConnection.model_validate_json(row[0]), str(row[1])

    def connections(self, project_id: str) -> list[ConnectorConnection]:
        return [
            ConnectorConnection.model_validate_json(r[0])
            for r in self.db.execute(
                "SELECT snapshot_json FROM connector_connections WHERE project_id=? ORDER BY rowid",
                (project_id,),
            )
        ]

    def save_connection(self, connection: ConnectorConnection, digest: str) -> None:
        self.db.execute(
            "INSERT INTO connector_connections VALUES (?,?,?,?)",
            (connection.connector_id, connection.project_id, digest, connection.model_dump_json()),
        )

    def call(self, call_id: str) -> tuple[ConnectorCall, str | None, str | None]:
        row = self.db.execute(
            "SELECT snapshot_json,owner,claim_token FROM connector_calls WHERE call_id=?",
            (call_id,),
        ).fetchone()
        if row is None:
            raise QueryNotFoundError("Connector call", normalize_id(call_id))
        return ConnectorCall.model_validate_json(row[0]), row[1], row[2]

    def calls(self, connector_id: str) -> list[ConnectorCall]:
        return [
            ConnectorCall.model_validate_json(r[0])
            for r in self.db.execute(
                "SELECT snapshot_json FROM connector_calls WHERE connector_id=? ORDER BY rowid",
                (connector_id,),
            )
        ]

    def save_call(self, call: ConnectorCall, owner: str | None, token: str | None) -> None:
        self.db.execute(
            "INSERT INTO connector_calls VALUES (?,?,?,?,?) "
            "ON CONFLICT(call_id) DO UPDATE SET owner=excluded.owner,"
            "claim_token=excluded.claim_token,snapshot_json=excluded.snapshot_json",
            (call.call_id, call.connector_id, owner, token, call.model_dump_json()),
        )

    def receipt(self, scope: str, key: str, fingerprint: str) -> dict[str, JsonValue] | None:
        row = self.db.execute(
            "SELECT fingerprint,response_json FROM connector_receipts "
            "WHERE scope=? AND receipt_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != sha256(fingerprint.encode()).hexdigest():
            raise StateConflictError("Connector receipt ID already belongs to different content")
        document = json_loads(row[1])
        if not isinstance(document, dict):
            raise RuntimeError("Connector receipt must be an object")
        return document

    def remember(
        self, scope: str, key: str, fingerprint: str, result: dict[str, JsonValue]
    ) -> None:
        self.db.execute(
            "INSERT INTO connector_receipts VALUES (?,?,?,?)",
            (scope, key, sha256(fingerprint.encode()).hexdigest(), json_dumps(result)),
        )

    def emit(self, event: Event) -> None:
        SQLiteEventLog(self.db).append(event)
