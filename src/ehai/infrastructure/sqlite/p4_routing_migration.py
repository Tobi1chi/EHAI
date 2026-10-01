"""Opt-in feedback-lab records, separate from timed Routine and production plans."""

ROUTING_MIGRATION = (
    """CREATE TABLE routing_documents (
        entity_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('lab','recipe','request','replay')),
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        lab_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json))
    )""",
    "CREATE INDEX routing_lab_documents ON routing_documents(lab_id,kind)",
    """CREATE TABLE routing_receipts (
        scope TEXT NOT NULL,
        receipt_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        response_json TEXT NOT NULL CHECK(json_valid(response_json)),
        PRIMARY KEY(scope,receipt_key)
    )""",
)
