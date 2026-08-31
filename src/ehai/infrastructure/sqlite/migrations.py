"""Forward-only SQLite schema migrations for the P1 execution plane."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

LATEST_SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    """Raised when a database schema cannot be safely opened by this build."""


_MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
    )
    """,
    """
    CREATE TABLE goals (
        goal_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        current_completion_contract_id TEXT
            REFERENCES completion_contracts(completion_contract_id)
            DEFERRABLE INITIALLY DEFERRED,
        created_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
    )
    """,
    """
    CREATE INDEX goals_project_idx ON goals(project_id, goal_id)
    """,
    """
    CREATE TABLE completion_contracts (
        completion_contract_id TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL REFERENCES goals(goal_id),
        version INTEGER NOT NULL CHECK (version > 0),
        supersedes_completion_contract_id TEXT
            REFERENCES completion_contracts(completion_contract_id),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        UNIQUE (goal_id, version)
    )
    """,
    """
    CREATE INDEX completion_contracts_goal_idx
        ON completion_contracts(goal_id, version)
    """,
    """
    CREATE TABLE plan_revisions (
        plan_revision_id TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL REFERENCES goals(goal_id),
        completion_contract_id TEXT NOT NULL
            REFERENCES completion_contracts(completion_contract_id),
        version INTEGER NOT NULL CHECK (version > 0),
        supersedes_plan_revision_id TEXT REFERENCES plan_revisions(plan_revision_id),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        UNIQUE (goal_id, version)
    )
    """,
    """
    CREATE INDEX plan_revisions_goal_idx ON plan_revisions(goal_id, version)
    """,
    """
    CREATE TABLE plan_nodes (
        plan_node_id TEXT PRIMARY KEY,
        plan_revision_id TEXT NOT NULL
            REFERENCES plan_revisions(plan_revision_id) ON DELETE CASCADE,
        sort_index INTEGER NOT NULL CHECK (sort_index >= 0),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        UNIQUE (plan_revision_id, sort_index)
    )
    """,
    """
    CREATE INDEX plan_nodes_revision_idx ON plan_nodes(plan_revision_id, sort_index)
    """,
    """
    CREATE TABLE branches (
        branch_id TEXT PRIMARY KEY,
        plan_revision_id TEXT NOT NULL
            REFERENCES plan_revisions(plan_revision_id) ON DELETE CASCADE,
        fork_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        merge_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        sort_index INTEGER NOT NULL CHECK (sort_index >= 0),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        UNIQUE (plan_revision_id, sort_index)
    )
    """,
    """
    CREATE INDEX branches_revision_idx ON branches(plan_revision_id, sort_index)
    """,
    """
    CREATE TABLE edges (
        edge_id TEXT PRIMARY KEY,
        plan_revision_id TEXT NOT NULL
            REFERENCES plan_revisions(plan_revision_id) ON DELETE CASCADE,
        source_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        target_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        branch_id TEXT REFERENCES branches(branch_id),
        sort_index INTEGER NOT NULL CHECK (sort_index >= 0),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        UNIQUE (plan_revision_id, sort_index)
    )
    """,
    """
    CREATE INDEX edges_revision_idx ON edges(plan_revision_id, sort_index)
    """,
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL REFERENCES goals(goal_id),
        plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(plan_revision_id),
        created_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
    )
    """,
    """
    CREATE INDEX runs_plan_revision_idx ON runs(plan_revision_id, run_id)
    """,
    """
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        plan_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
        UNIQUE (run_id, plan_node_id, sequence)
    )
    """,
    """
    CREATE INDEX attempts_run_idx ON attempts(run_id, plan_node_id, sequence)
    """,
    """
    CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY,
        run_id TEXT REFERENCES runs(run_id),
        plan_node_id TEXT REFERENCES plan_nodes(plan_node_id),
        attempt_id TEXT REFERENCES attempts(attempt_id),
        sha256 TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
    )
    """,
    """
    CREATE INDEX artifacts_run_idx ON artifacts(run_id, artifact_id)
    """,
    """
    CREATE TABLE check_runs (
        check_run_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        plan_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        check_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
    )
    """,
    """
    CREATE INDEX check_runs_run_idx ON check_runs(run_id, plan_node_id, check_run_id)
    """,
    """
    CREATE TABLE checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(plan_revision_id),
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        event_offset INTEGER NOT NULL REFERENCES event_log(event_offset),
        gate_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json))
    )
    """,
    """
    CREATE INDEX checkpoints_run_idx ON checkpoints(run_id, event_offset)
    """,
    """
    CREATE TABLE checkpoint_branch_selections (
        checkpoint_id TEXT NOT NULL
            REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
        fork_node_id TEXT NOT NULL REFERENCES plan_nodes(plan_node_id),
        branch_id TEXT NOT NULL REFERENCES branches(branch_id),
        PRIMARY KEY (checkpoint_id, fork_node_id)
    )
    """,
    """
    CREATE TABLE checkpoint_artifacts (
        checkpoint_id TEXT NOT NULL
            REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        PRIMARY KEY (checkpoint_id, artifact_id)
    )
    """,
    """
    CREATE TABLE event_log (
        event_offset INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        run_id TEXT REFERENCES runs(run_id),
        event_type TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        event_json TEXT NOT NULL CHECK (json_valid(event_json))
    )
    """,
    """
    CREATE INDEX event_log_run_offset_idx ON event_log(run_id, event_offset)
    """,
    """
    CREATE TABLE command_receipts (
        idempotency_key TEXT PRIMARY KEY,
        command_name TEXT NOT NULL,
        command_fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL CHECK (json_valid(result_json)),
        created_at TEXT NOT NULL
    )
    """,
)

_MIGRATIONS: dict[int, Sequence[str]] = {1: _MIGRATION_1}


def migrate(connection: sqlite3.Connection) -> None:
    """Apply every pending migration atomically and reject future schemas."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema version {current} is newer than supported "
            f"version {LATEST_SCHEMA_VERSION}"
        )

    for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
        statements = _MIGRATIONS.get(version)
        if statements is None:
            raise SchemaVersionError(f"missing migration for schema version {version}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
