"""SQLite repository for persistent Built-in Session messages."""

from __future__ import annotations

import sqlite3

from ehai import ID, format_utc_datetime, normalize_id, parse_utc_datetime
from ehai.domain.session_messages import SessionMessage, SessionMessageStatus
from ehai.infrastructure.sqlite.database import SQLiteDatabase


class SQLiteSessionMailboxRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def list_session_ids(self) -> tuple[ID, ...]:
        connection = self._database.connect()
        try:
            rows = connection.execute(
                """
                SELECT agent_session_ref_id FROM agent_session_refs
                UNION
                SELECT agent_session_ref_id FROM builtin_role_sessions
                ORDER BY agent_session_ref_id
                """
            ).fetchall()
            return tuple(ID(_row_text(row, 0)) for row in rows)
        finally:
            connection.close()

    def add(self, message: SessionMessage) -> None:
        connection = self._database.connect()
        try:
            connection.execute(
                """
                INSERT INTO session_messages(
                    message_id, source_session_id, target_session_id,
                    correlation_id, content, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                _values(message),
            )
            connection.commit()
        finally:
            connection.close()

    def list_for_target(
        self,
        target_session_id: ID,
        *,
        correlation_id: str | None = None,
        include_read: bool = True,
    ) -> tuple[SessionMessage, ...]:
        target = normalize_id(target_session_id)
        connection = self._database.connect()
        try:
            conditions = ["target_session_id = ?"]
            parameters: list[str] = [target]
            if correlation_id is not None:
                conditions.append("correlation_id = ?")
                parameters.append(correlation_id)
            if not include_read:
                conditions.append("status != 'read'")
            rows = connection.execute(
                """
                SELECT message_id, source_session_id, target_session_id,
                       correlation_id, content, created_at, status
                FROM session_messages WHERE
                """
                + " AND ".join(conditions)
                + " ORDER BY created_at, message_id",
                tuple(parameters),
            ).fetchall()
            return tuple(_message(row) for row in rows)
        finally:
            connection.close()

    def save(self, message: SessionMessage) -> None:
        connection = self._database.connect()
        try:
            cursor = connection.execute(
                """
                UPDATE session_messages SET status = ?
                WHERE message_id = ? AND source_session_id = ? AND target_session_id = ?
                """,
                (
                    message.status.value,
                    message.message_id,
                    message.source_session_id,
                    message.target_session_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LookupError(f"Session Message {message.message_id} is not persisted")
            connection.commit()
        finally:
            connection.close()


def _values(message: SessionMessage) -> tuple[str, ...]:
    return (
        message.message_id,
        message.source_session_id,
        message.target_session_id,
        message.correlation_id,
        message.content,
        format_utc_datetime(message.created_at),
        message.status.value,
    )


def _message(row: sqlite3.Row) -> SessionMessage:
    return SessionMessage(
        source_session_id=ID(_row_text(row, 1)),
        target_session_id=ID(_row_text(row, 2)),
        correlation_id=_row_text(row, 3),
        content=_row_text(row, 4),
        message_id=ID(_row_text(row, 0)),
        created_at=parse_utc_datetime(_row_text(row, 5)),
        status=SessionMessageStatus(_row_text(row, 6)),
    )


def _row_text(row: sqlite3.Row, index: int) -> str:
    value = row[index]
    if not isinstance(value, str):
        raise RuntimeError("SQLite Session Message row is not text")
    return value
