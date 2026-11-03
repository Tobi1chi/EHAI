"""Life workflow documents share the core's durable transaction boundary."""

WORKFLOW_MIGRATION = (
    """CREATE TABLE workflow_documents (
        kind TEXT NOT NULL CHECK(kind IN ('run','task','routine')),
        entity_id TEXT NOT NULL,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        PRIMARY KEY(kind, entity_id)
    )""",
    "CREATE INDEX workflow_project ON workflow_documents(kind, project_id)",
    """CREATE TABLE workflow_receipts (
        idempotency_key TEXT PRIMARY KEY,
        request_json TEXT NOT NULL,
        response_json TEXT NOT NULL CHECK(json_valid(response_json))
    )""",
)

# Custom workflow definitions: immutable revisions, one name per project.
WORKFLOW_DEFINITION_MIGRATION = (
    """CREATE TABLE workflow_definitions (
        definition_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        name TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0),
        snapshot_json TEXT NOT NULL CHECK(json_valid(snapshot_json)),
        UNIQUE(project_id, name, revision),
        CHECK(json_extract(snapshot_json,'$.definition_id') = definition_id),
        CHECK(json_extract(snapshot_json,'$.revision') = revision)
    )""",
    "CREATE INDEX workflow_definition_project ON workflow_definitions(project_id)",
)
