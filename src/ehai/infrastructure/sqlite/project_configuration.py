"""SQLite storage for append-only configuration versions."""

from __future__ import annotations

from ehai import ID, JsonValue, json_dumps, json_loads
from ehai.application.project_configuration import (
    ProjectConfigurationConflict,
    ProjectConfigurationNotFound,
    run_configuration,
)
from ehai.infrastructure.sqlite.database import SQLiteDatabase


class SQLiteProjectConfigurationStore:
    """Keep configuration writes serialized and idempotent with immutable old versions."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def versions(self, project_id: ID) -> list[dict[str, JsonValue]]:
        connection = self._database._connect_read_only()
        try:
            connection.execute("BEGIN DEFERRED")
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                is None
            ):
                raise ProjectConfigurationNotFound(f"Project {project_id} not found")
            rows = connection.execute(
                "SELECT snapshot_json FROM project_configuration_versions "
                "WHERE project_id=? ORDER BY version",
                (project_id,),
            ).fetchall()
            return [_document(row[0]) for row in rows]
        finally:
            connection.close()

    def put(
        self,
        project_id: ID,
        expected_version: int,
        idempotency_key: str,
        request: dict[str, JsonValue],
        snapshot: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        connection = self._database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request_json = json_dumps(request)
            existing = connection.execute(
                "SELECT request_json,snapshot_json FROM project_configuration_versions "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing[0] != request_json:
                    raise ProjectConfigurationConflict(
                        "idempotency_key already belongs to another configuration request"
                    )
                return _document(existing[1])
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                is None
            ):
                raise ProjectConfigurationNotFound(f"Project {project_id} not found")
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM project_configuration_versions "
                "WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            if version != expected_version:
                raise ProjectConfigurationConflict(
                    f"Expected project configuration version {expected_version}; "
                    f"current version is {version}"
                )
            connection.execute(
                "INSERT INTO project_configuration_versions VALUES (?,?,?,?,?)",
                (project_id, version + 1, idempotency_key, request_json, json_dumps(snapshot)),
            )
            connection.commit()
            return snapshot
        finally:
            connection.close()

    def run_snapshot(self, run_id: ID) -> dict[str, JsonValue] | None:
        with self._database.read_session() as session:
            if session.states.get_run(run_id) is None:
                raise ProjectConfigurationNotFound(f"Run {run_id} not found")
            return run_configuration(session.events, run_id)


def _document(raw: str) -> dict[str, JsonValue]:
    document = json_loads(raw)
    if not isinstance(document, dict):
        raise RuntimeError("Stored project configuration must be an object")
    return document
