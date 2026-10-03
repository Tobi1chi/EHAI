"""One receipt table for every idempotent command, keyed by (scope, idempotency_key)."""

import sqlite3
from hashlib import sha256

from ehai import json_loads

RECEIPT_MIGRATION = (
    """CREATE TABLE unified_command_receipts (
        scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        command_name TEXT NOT NULL,
        command_fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL CHECK (json_valid(result_json)),
        created_at TEXT,
        PRIMARY KEY (scope, idempotency_key)
    )""",
)

# Connector and routing scopes are "<operation>" or "<operation>:<target>".
_OPERATION = (
    "CASE instr(scope, ':') WHEN 0 THEN scope ELSE substr(scope, 1, instr(scope, ':') - 1) END"
)


def merge_receipt_tables(connection: sqlite3.Connection) -> None:
    """Called inside migration 23's transaction; keeps every key in its original namespace.

    Workflow, connector and routing receipts never recorded a time, so theirs stays NULL.
    """
    connection.execute(
        "INSERT INTO unified_command_receipts "
        "SELECT 'command', idempotency_key, command_name, command_fingerprint, "
        "result_json, created_at FROM command_receipts"
    )
    # Workflow receipts kept the whole request; its hash is the fingerprint used from now on.
    for key, request, response in connection.execute(
        "SELECT idempotency_key, request_json, response_json FROM workflow_receipts"
    ).fetchall():
        document = json_loads(request)
        if not isinstance(document, dict) or not isinstance(document.get("operation"), str):
            raise RuntimeError("stored workflow receipt request has no operation")
        connection.execute(
            "INSERT INTO unified_command_receipts VALUES ('workflow', ?, ?, ?, ?, NULL)",
            (key, document["operation"], sha256(request.encode()).hexdigest(), response),
        )
    for prefix, table in (("connector", "connector_receipts"), ("routing", "routing_receipts")):
        connection.execute(
            f"INSERT INTO unified_command_receipts "
            f"SELECT '{prefix}:' || scope, receipt_key, {_OPERATION}, fingerprint, "
            f"response_json, NULL FROM {table}"
        )
    for table in (
        "command_receipts",
        "workflow_receipts",
        "connector_receipts",
        "routing_receipts",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("ALTER TABLE unified_command_receipts RENAME TO command_receipts")
