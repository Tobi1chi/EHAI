"""Durable, project-scoped connector registrations, calls and transport receipts."""

CONNECTOR_MIGRATION = (
    """CREATE TABLE connector_connections (
        connector_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        credential_sha256 TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json))
    )""",
    "CREATE INDEX connector_project ON connector_connections(project_id)",
    """CREATE TABLE connector_calls (
        call_id TEXT PRIMARY KEY,
        connector_id TEXT NOT NULL REFERENCES connector_connections(connector_id),
        owner TEXT,
        claim_token TEXT,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json))
    )""",
    "CREATE INDEX connector_call_connection ON connector_calls(connector_id)",
    """CREATE TABLE connector_receipts (
        scope TEXT NOT NULL,
        receipt_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        response_json TEXT NOT NULL CHECK(json_valid(response_json)),
        PRIMARY KEY(scope,receipt_key)
    )""",
)
