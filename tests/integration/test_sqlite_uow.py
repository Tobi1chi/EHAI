import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ehai import format_utc_datetime, json_dumps, json_loads, new_id
from ehai.application.ports import CommandReceipt, UnitOfWork
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract, Goal, Project
from ehai.domain.planning import PlanNode, PlanRevision, PlanRevisionStatus
from ehai.domain.workers import SessionPolicy
from ehai.infrastructure.sqlite import (
    LATEST_SCHEMA_VERSION,
    P1_SCHEMA_VERSION,
    IdempotencyConflictError,
    SchemaVersionError,
    SQLiteDatabase,
    UnknownEventCursorError,
)
from ehai.infrastructure.sqlite.codec import (
    encode_attempt,
    encode_completion_contract,
    encode_goal,
    encode_plan_node,
    encode_plan_revision,
    encode_project,
    encode_run,
)
from ehai.infrastructure.sqlite.migrations import migrate

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def test_database_applies_migration_and_connection_pragmas(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "state.sqlite3")

    connection = database.connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        connection.close()


def test_database_rejects_schema_from_the_future(tmp_path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer than supported"):
        SQLiteDatabase(path)


def test_p1_backup_migrates_with_legacy_plan_nodes_and_attempts(tmp_path) -> None:
    source_path = tmp_path / "p1-source.sqlite3"
    backup_path = tmp_path / "p1-backup.sqlite3"
    project = Project.create("P1 backup", project_id=new_id(), created_at=NOW)
    goal = Goal.create(project.project_id, "migrate", goal_id=new_id(), created_at=NOW)
    contract = CompletionContract.draft(
        goal.goal_id,
        ("preserve P1",),
        (new_id(),),
        completion_contract_id=new_id(),
        created_at=NOW,
    ).confirm(confirmed_at=NOW)
    goal = goal.use_completion_contract(contract)
    node = PlanNode(new_id(), "legacy", "preserve")
    plan = PlanRevision.rehydrate(
        plan_revision_id=new_id(),
        goal_id=goal.goal_id,
        version=1,
        completion_contract_id=contract.completion_contract_id,
        completion_contract_version=contract.version,
        nodes=(node,),
        edges=(),
        branches=(),
        created_at=NOW,
        status=PlanRevisionStatus.APPROVED,
        approved_at=NOW,
        supersedes_plan_revision_id=None,
    )
    run = Run(goal.goal_id, plan.plan_revision_id, created_at=NOW)
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=NOW)
    node_document = json_loads(encode_plan_node(node))
    attempt_document = json_loads(encode_attempt(attempt))
    assert isinstance(node_document, dict)
    assert isinstance(attempt_document, dict)
    node_document.pop("required_capabilities")
    node_document.pop("session_policy")
    for key in (
        "worker_profile_id",
        "worker_endpoint_id",
        "agent_session_ref_id",
        "execution_handle",
        "activity",
        "event_cursor",
        "heartbeat_at",
        "progress_at",
        "deadline_at",
        "lease_expires_at",
        "queue_reason",
    ):
        attempt_document.pop(key)

    source = sqlite3.connect(source_path)
    source.execute("PRAGMA foreign_keys = ON")
    migrate(source, target_version=P1_SCHEMA_VERSION)
    source.execute("BEGIN IMMEDIATE")
    source.execute(
        "INSERT INTO projects VALUES (?, ?, ?)",
        (project.project_id, format_utc_datetime(project.created_at), encode_project(project)),
    )
    source.execute(
        "INSERT INTO goals VALUES (?, ?, ?, ?, ?)",
        (
            goal.goal_id,
            goal.project_id,
            contract.completion_contract_id,
            format_utc_datetime(goal.created_at),
            encode_goal(goal),
        ),
    )
    source.execute(
        "INSERT INTO completion_contracts VALUES (?, ?, ?, ?, ?)",
        (
            contract.completion_contract_id,
            contract.goal_id,
            contract.version,
            None,
            encode_completion_contract(contract),
        ),
    )
    source.execute(
        "INSERT INTO plan_revisions VALUES (?, ?, ?, ?, ?, ?)",
        (
            plan.plan_revision_id,
            plan.goal_id,
            plan.completion_contract_id,
            plan.version,
            None,
            encode_plan_revision(plan),
        ),
    )
    source.execute(
        "INSERT INTO plan_nodes VALUES (?, ?, ?, ?)",
        (node.plan_node_id, plan.plan_revision_id, 0, json_dumps(node_document)),
    )
    source.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
        (
            run.run_id,
            run.goal_id,
            run.plan_revision_id,
            format_utc_datetime(run.created_at),
            encode_run(run),
        ),
    )
    source.execute(
        "INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
        (
            attempt.attempt_id,
            attempt.run_id,
            attempt.plan_node_id,
            attempt.sequence,
            json_dumps(attempt_document),
        ),
    )
    source.commit()
    destination = sqlite3.connect(backup_path)
    source.backup(destination)
    destination.close()
    assert source.execute("PRAGMA user_version").fetchone()[0] == P1_SCHEMA_VERSION
    source.close()

    migrated = SQLiteDatabase(backup_path)
    with migrated.unit_of_work() as uow:
        restored_plan = uow.states.get_plan_revision(plan.plan_revision_id)
        restored_attempt = uow.states.get_attempt(attempt.attempt_id)
        assert restored_plan is not None
        assert restored_plan.nodes[0].required_capabilities == frozenset()
        assert restored_plan.nodes[0].session_policy is SessionPolicy.NEW
        assert restored_attempt == attempt
    connection = migrated.connect()
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        connection.close()


def test_uow_rolls_back_uncommitted_state_and_event(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "rollback.sqlite3")
    project = Project.create("rollback", project_id=new_id(), created_at=NOW)
    event = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=project.project_id,
        payload={"project_id": project.project_id},
        occurred_at=NOW,
    )

    with database.unit_of_work() as uow:
        assert isinstance(uow, UnitOfWork)
        uow.states.put_project(project)
        uow.events.append(event)

    with database.unit_of_work() as uow:
        assert uow.states.get_project(project.project_id) is None
        assert uow.events.latest_offset() == 0


def test_state_event_and_receipt_commit_atomically_with_idempotency(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "commit.sqlite3")
    project = Project.create("committed", project_id=new_id(), created_at=NOW)
    event = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=project.project_id,
        payload={"project_id": project.project_id},
        occurred_at=NOW,
    )
    receipt = CommandReceipt(
        idempotency_key="create-project-1",
        command_name="CreateProject",
        command_fingerprint="sha256:one",
        result={"project_id": project.project_id},
        created_at=NOW,
    )

    with database.unit_of_work() as uow:
        uow.states.put_project(project)
        stored = uow.events.append(event)
        uow.command_receipts.put(receipt)
        uow.command_receipts.put(receipt)
        uow.commit()
    assert stored.offset == 1

    with database.unit_of_work() as uow:
        assert uow.states.get_project(project.project_id) == project
        assert uow.events.list_events() == (stored,)
        restored_receipt = uow.command_receipts.get(receipt.idempotency_key)
        assert restored_receipt is not None
        assert restored_receipt.result == receipt.result
        with pytest.raises(IdempotencyConflictError):
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=receipt.idempotency_key,
                    command_name=receipt.command_name,
                    command_fingerprint="sha256:different",
                    result={},
                    created_at=NOW,
                )
            )
        with pytest.raises(IdempotencyConflictError):
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=receipt.idempotency_key,
                    command_name="AnotherCommand",
                    command_fingerprint=receipt.command_fingerprint,
                    result=receipt.result,
                    created_at=NOW,
                )
            )
        with pytest.raises(IdempotencyConflictError):
            uow.command_receipts.put(
                CommandReceipt(
                    idempotency_key=receipt.idempotency_key,
                    command_name=receipt.command_name,
                    command_fingerprint=receipt.command_fingerprint,
                    result={"project_id": new_id()},
                    created_at=NOW,
                )
            )
        uow.command_receipts.put(
            CommandReceipt(
                idempotency_key=receipt.idempotency_key,
                command_name=receipt.command_name,
                command_fingerprint=receipt.command_fingerprint,
                result=receipt.result,
                created_at=NOW + timedelta(seconds=1),
            )
        )


def test_event_offsets_are_monotonic_and_cursor_is_fail_closed(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "events.sqlite3")
    correlation_id = new_id()
    events = tuple(
        Event(
            type=EventType.PROJECT_CREATED,
            correlation_id=correlation_id,
            payload={"sequence": index},
            occurred_at=NOW + timedelta(seconds=index),
        )
        for index in range(3)
    )
    with database.unit_of_work() as uow:
        stored = tuple(uow.events.append(event) for event in events)
        uow.commit()

    assert tuple(item.offset for item in stored) == (1, 2, 3)
    with database.unit_of_work() as uow:
        assert uow.events.latest_offset() == 3
        assert uow.events.list_events(after_event_id=events[1].id) == (stored[2],)
        assert uow.events.list_events(limit=2) == stored[:2]
        with pytest.raises(UnknownEventCursorError):
            uow.events.list_events(after_event_id=new_id())
