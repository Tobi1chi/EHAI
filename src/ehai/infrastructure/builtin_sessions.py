"""SQLite append-only storage for Built-in Agent Session events."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from ehai import (
    ID,
    JsonValue,
    format_utc_datetime,
    json_dumps,
    json_loads,
    normalize_id,
    parse_utc_datetime,
)
from ehai.application.builtin_agent import (
    BuiltinSession,
    BuiltinSessionEvent,
    BuiltinSessionEventType,
    BuiltinSessionStateError,
)
from ehai.infrastructure.sqlite.database import SQLiteDatabase


class SQLiteBuiltinSessionStore:
    """Persist contiguous BuiltinSessionEvent batches in SQLite transactions."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def load(self, agent_session_ref_id: ID) -> BuiltinSession:
        session_id = normalize_id(agent_session_ref_id)
        connection = self._database.connect()
        try:
            if not _session_exists(connection, session_id):
                raise LookupError(f"AgentSessionRef {session_id} is not persisted")
            rows = connection.execute(
                """
                SELECT event_json FROM builtin_session_events
                WHERE agent_session_ref_id = ? ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
            return BuiltinSession(
                session_id,
                tuple(_decode_event(_row_text(row, 0)) for row in rows),
            )
        finally:
            connection.close()

    def append(
        self,
        agent_session_ref_id: ID,
        expected_sequence: int,
        events: tuple[BuiltinSessionEvent, ...],
    ) -> None:
        session_id = normalize_id(agent_session_ref_id)
        if type(expected_sequence) is not int or expected_sequence < 0:
            raise ValueError("expected_sequence must be a non-negative integer")
        if not events:
            return
        connection = self._database.connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if not _session_exists(connection, session_id):
                raise LookupError(f"AgentSessionRef {session_id} is not persisted")
            current = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) FROM builtin_session_events
                    WHERE agent_session_ref_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
            )
            if current != expected_sequence:
                raise BuiltinSessionStateError(
                    f"Session {session_id} expected sequence {expected_sequence}, found {current}"
                )
            for offset, event in enumerate(events, start=1):
                if (
                    event.agent_session_ref_id != session_id
                    or event.sequence != expected_sequence + offset
                ):
                    raise BuiltinSessionStateError("SessionEvent append batch is not contiguous")
                connection.execute(
                    """
                    INSERT INTO builtin_session_events(
                        agent_session_ref_id, sequence, attempt_id,
                        event_type, occurred_at, event_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        event.sequence,
                        event.attempt_id,
                        event.type.value,
                        format_utc_datetime(event.occurred_at),
                        _encode_event(event),
                    ),
                )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()


def _session_exists(connection: sqlite3.Connection, session_id: ID) -> bool:
    row = connection.execute(
        "SELECT 1 FROM agent_session_refs WHERE agent_session_ref_id = ?",
        (session_id,),
    ).fetchone()
    return row is not None


def _encode_event(event: BuiltinSessionEvent) -> str:
    return json_dumps(
        {
            "agent_session_ref_id": event.agent_session_ref_id,
            "attempt_id": event.attempt_id,
            "sequence": event.sequence,
            "type": event.type.value,
            "occurred_at": format_utc_datetime(event.occurred_at),
            "payload": event.payload,
        }
    )


def _decode_event(document: str) -> BuiltinSessionEvent:
    value = json_loads(document)
    if not isinstance(value, dict):
        raise BuiltinSessionStateError("stored SessionEvent is not an object")
    payload = value.get("payload")
    if not isinstance(payload, Mapping):
        raise BuiltinSessionStateError("stored SessionEvent payload is not an object")
    return BuiltinSessionEvent(
        agent_session_ref_id=ID(_string(value, "agent_session_ref_id")),
        attempt_id=ID(_string(value, "attempt_id")),
        sequence=_integer(value, "sequence"),
        event_type=BuiltinSessionEventType(_string(value, "type")),
        occurred_at=parse_utc_datetime(_string(value, "occurred_at")),
        payload=payload,
    )


def _string(document: Mapping[str, JsonValue], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        raise BuiltinSessionStateError(f"stored SessionEvent {key} must be text")
    return value


def _integer(document: Mapping[str, JsonValue], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BuiltinSessionStateError(f"stored SessionEvent {key} must be an integer")
    return value


def _row_text(row: sqlite3.Row, index: int) -> str:
    value = row[index]
    if not isinstance(value, str):
        raise RuntimeError("SQLite SessionEvent row is not text")
    return value
