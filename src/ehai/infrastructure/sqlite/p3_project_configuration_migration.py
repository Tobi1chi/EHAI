"""Immutable project configuration versions, separate from private Pi settings."""

PROJECT_CONFIGURATION_MIGRATION: tuple[str, ...] = (
    """CREATE TABLE project_configuration_versions (
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        version INTEGER NOT NULL CHECK(version > 0),
        idempotency_key TEXT NOT NULL UNIQUE,
        request_json TEXT NOT NULL CHECK(json_valid(request_json)),
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        PRIMARY KEY(project_id, version)
    )""",
)
