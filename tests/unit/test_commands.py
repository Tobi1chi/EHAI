from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from ehai import ID, new_id
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    ProposePlan,
    StartRun,
)


def _command_factories(idempotency_key: str) -> tuple[Callable[[], object], ...]:
    return (
        lambda: CreateProject(idempotency_key, "project"),
        lambda: CreateGoal(idempotency_key, new_id(), "goal"),
        lambda: ProposePlan(idempotency_key, new_id(), ("criterion",)),
        lambda: ApprovePlan(idempotency_key, new_id(), new_id()),
        lambda: StartRun(idempotency_key, new_id()),
    )


@pytest.mark.parametrize("command_index", range(5))
def test_every_command_requires_non_empty_idempotency_key(command_index: int) -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        _command_factories("   ")[command_index]()


def test_command_ids_are_normalized_and_text_is_trimmed() -> None:
    project_id = new_id()
    goal = CreateGoal("key", project_id.upper(), "  produce evidence  ")
    plan = ProposePlan("key", new_id().upper(), ["  first  ", "second"])  # type: ignore[arg-type]

    assert goal.project_id == project_id
    assert goal.objective == "produce evidence"
    assert plan.criteria == ("first", "second")


def test_command_inputs_are_frozen_and_snapshot_mutable_criteria() -> None:
    criteria = ["criterion"]
    command = ProposePlan("key", new_id(), criteria)  # type: ignore[arg-type]
    criteria.append("late mutation")

    assert command.criteria == ("criterion",)
    with pytest.raises(FrozenInstanceError):
        command.criteria = ()  # type: ignore[misc]


def test_fingerprint_is_deterministic_and_excludes_idempotency_key() -> None:
    goal_id = new_id()
    first = ProposePlan("first-key", goal_id, ("criterion",))
    retry = ProposePlan("retry-key", goal_id, ("criterion",))
    changed = ProposePlan("first-key", goal_id, ("different",))

    assert first.fingerprint == retry.fingerprint
    assert first.fingerprint != changed.fingerprint
    assert len(first.fingerprint) == 64
    assert first.fingerprint == first.fingerprint


def test_fingerprint_includes_command_type() -> None:
    identifier = new_id()
    create_goal = CreateGoal("key", identifier, "same")
    start_run = StartRun("key", identifier)

    assert create_goal.fingerprint != start_run.fingerprint


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CreateGoal("key", ID("not-a-uuid"), "goal"),
        lambda: ProposePlan("key", ID("not-a-uuid"), ("criterion",)),
        lambda: ApprovePlan("key", ID("not-a-uuid"), new_id()),
        lambda: StartRun("key", ID("not-a-uuid")),
    ],
)
def test_commands_reject_invalid_ids(factory: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match="invalid"):
        factory()


def test_commands_reject_empty_domain_text() -> None:
    with pytest.raises(ValueError, match="name"):
        CreateProject("key", " ")
    with pytest.raises(ValueError, match="objective"):
        CreateGoal("key", new_id(), " ")
    with pytest.raises(ValueError, match="criteria"):
        ProposePlan("key", new_id(), ())
