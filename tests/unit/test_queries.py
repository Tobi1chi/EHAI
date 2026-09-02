from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from ehai import ID, new_id
from ehai.application.ports import (
    CurrentStateReader,
    EventReader,
    StoredEvent,
    WorkerRegistryReader,
)
from ehai.application.queries import QueryNotFoundError, QueryService
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.events import Event, EventType
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode, PlanRevision

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


class _States:
    def __init__(
        self,
        *,
        plan: PlanRevision | None,
        run: Run | None,
        attempts: tuple[Attempt, ...] = (),
        artifacts: tuple[Artifact, ...] = (),
        check_specs: tuple[CheckSpec, ...] = (),
    ) -> None:
        self.plan = plan
        self.run = run
        self.attempts = attempts
        self.artifacts = artifacts
        self.check_specs = check_specs

    def get_plan_revision(self, plan_revision_id: ID) -> PlanRevision | None:
        if self.plan is not None and self.plan.plan_revision_id == plan_revision_id:
            return self.plan
        return None

    def get_run(self, run_id: ID) -> Run | None:
        if self.run is not None and self.run.run_id == run_id:
            return self.run
        return None

    def list_attempts(self, run_id: ID) -> tuple[Attempt, ...]:
        del run_id
        return self.attempts

    def list_artifacts_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        del run_id
        return self.artifacts

    def list_check_specs(self, plan_revision_id: ID) -> tuple[CheckSpec, ...]:
        del plan_revision_id
        return self.check_specs

    def list_check_runs(self, run_id: ID) -> tuple[()]:
        del run_id
        return ()

    def list_checkpoints(self, run_id: ID) -> tuple[()]:
        del run_id
        return ()

    def get_artifact(self, artifact_id: ID) -> Artifact | None:
        return next(
            (artifact for artifact in self.artifacts if artifact.artifact_id == artifact_id),
            None,
        )


class _Events:
    def __init__(self, events: tuple[StoredEvent, ...]) -> None:
        self.events = events

    def list_events(
        self,
        *,
        after_event_id: ID | None = None,
        limit: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        start = 0
        if after_event_id is not None:
            for index, stored in enumerate(self.events):
                if stored.event.id == after_event_id:
                    start = index + 1
                    break
            else:
                raise LookupError(f"unknown Event cursor {after_event_id}")
        selected = self.events[start:]
        return selected if limit is None else selected[:limit]

    def latest_offset(self) -> int:
        return 0 if not self.events else max(stored.offset for stored in self.events)


class _ReadSession:
    def __init__(self, states: _States, events: _Events) -> None:
        self.states = cast(CurrentStateReader, states)
        self.events = cast(EventReader, events)
        self.worker_registry = cast(WorkerRegistryReader, object())
        self.closed = False

    def __enter__(self) -> _ReadSession:
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.closed = True


class _SessionFactory:
    def __init__(self, states: _States, events: _Events) -> None:
        self._states = states
        self._events = events
        self.sessions: list[_ReadSession] = []

    def __call__(self) -> _ReadSession:
        session = _ReadSession(self._states, self._events)
        self.sessions.append(session)
        return session


def _context() -> tuple[
    PlanRevision,
    Run,
    tuple[Attempt, Attempt],
    Artifact,
    CheckSpec,
    tuple[StoredEvent, StoredEvent],
]:
    goal_id = new_id()
    check_spec = CheckSpec(
        name="artifact",
        kind=CheckKind.ARTIFACT,
        description="candidate exists",
        check_id=new_id(),
    )
    contract = CompletionContract.draft(
        goal_id,
        ("candidate exists",),
        (check_spec.check_id,),
        completion_contract_id=new_id(),
        created_at=NOW,
    )
    node = PlanNode(
        plan_node_id=new_id(),
        title="work",
        instruction="produce a candidate",
        required_check_ids=(check_spec.check_id,),
    )
    plan = PlanRevision.draft(
        goal_id,
        contract,
        (node,),
        (),
        plan_revision_id=new_id(),
        created_at=NOW,
    )
    run = Run(
        goal_id=goal_id,
        plan_revision_id=plan.plan_revision_id,
        run_id=new_id(),
        created_at=NOW,
    )
    later_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        sequence=2,
        attempt_id=new_id(),
        created_at=NOW + timedelta(seconds=2),
    )
    earlier_attempt = Attempt(
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        sequence=1,
        attempt_id=new_id(),
        created_at=NOW + timedelta(seconds=1),
    )
    artifact = Artifact(
        artifact_id=new_id(),
        kind=ArtifactKind.CANDIDATE,
        name="candidate.txt",
        media_type="text/plain",
        size_bytes=9,
        sha256="a" * 64,
        relative_path="private/candidate.txt",
        created_at=NOW + timedelta(seconds=3),
        run_id=run.run_id,
        plan_node_id=node.plan_node_id,
        attempt_id=earlier_attempt.attempt_id,
    )
    run_event = Event(
        type=EventType.RUN_STARTED,
        correlation_id=run.run_id,
        run_id=run.run_id,
        payload={"run_id": run.run_id},
        occurred_at=NOW,
    )
    unrelated_event = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=new_id(),
        payload={"project_id": new_id()},
        occurred_at=NOW,
    )
    return (
        plan,
        run,
        (later_attempt, earlier_attempt),
        artifact,
        check_spec,
        (StoredEvent(2, run_event), StoredEvent(1, unrelated_event)),
    )


def test_graph_and_trace_views_are_frozen_separate_and_sanitized() -> None:
    plan, run, attempts, artifact, check_spec, events = _context()
    factory = _SessionFactory(
        _States(
            plan=plan,
            run=run,
            attempts=attempts,
            artifacts=(artifact,),
            check_specs=(check_spec,),
        ),
        _Events(events),
    )
    queries = QueryService(read_session_factory=factory)

    graph = queries.get_plan_graph(plan.plan_revision_id)
    trace = queries.get_execution_trace(run.run_id)

    assert {field.name for field in fields(graph)}.isdisjoint(
        {"run", "attempts", "artifacts", "check_runs", "checkpoints", "events"}
    )
    assert {field.name for field in fields(trace)}.isdisjoint(
        {"plan_revision_id", "nodes", "edges", "branches"}
    )
    assert tuple(attempt.sequence for attempt in trace.attempts) == (1, 2)
    assert tuple(event.offset for event in trace.events) == (2,)
    assert not hasattr(trace.artifacts[0], "relative_path")
    with pytest.raises(FrozenInstanceError):
        trace.run.status_reason = "tampered"  # type: ignore[misc]
    assert all(session.closed for session in factory.sessions)


def test_resource_lists_validate_owners_and_each_use_a_fresh_session() -> None:
    plan, run, _, artifact, check_spec, events = _context()
    factory = _SessionFactory(
        _States(
            plan=plan,
            run=run,
            artifacts=(artifact,),
            check_specs=(check_spec,),
        ),
        _Events(events),
    )
    queries = QueryService(read_session_factory=factory)

    assert queries.get_run(run.run_id).run_id == run.run_id
    assert queries.list_check_specs(plan.plan_revision_id)[0].check_id == check_spec.check_id
    assert queries.list_check_runs(run.run_id) == ()
    assert queries.list_checkpoints(run.run_id) == ()
    assert queries.list_artifacts(run.run_id)[0].artifact_id == artifact.artifact_id
    assert queries.get_artifact(artifact.artifact_id).artifact_id == artifact.artifact_id
    assert len(factory.sessions) == 6
    assert all(session.closed for session in factory.sessions)


@pytest.mark.parametrize(
    "invoke",
    [
        lambda service, entity_id: service.get_run(entity_id),
        lambda service, entity_id: service.get_plan_graph(entity_id),
        lambda service, entity_id: service.get_execution_trace(entity_id),
        lambda service, entity_id: service.list_check_specs(entity_id),
        lambda service, entity_id: service.list_check_runs(entity_id),
        lambda service, entity_id: service.list_checkpoints(entity_id),
        lambda service, entity_id: service.list_artifacts(entity_id),
        lambda service, entity_id: service.get_artifact(entity_id),
    ],
)
def test_entity_queries_fail_closed_when_owner_or_entity_is_missing(
    invoke: Callable[[QueryService, ID], object],
) -> None:
    factory = _SessionFactory(_States(plan=None, run=None), _Events(()))
    missing_id = new_id()

    with pytest.raises(QueryNotFoundError, match="was not found"):
        invoke(QueryService(read_session_factory=factory), missing_id)

    assert factory.sessions[-1].closed


def test_event_pages_are_ordered_resumable_and_fail_closed() -> None:
    *_, stored = _context()
    ordered = tuple(sorted(stored, key=lambda item: item.offset))
    factory = _SessionFactory(_States(plan=None, run=None), _Events(ordered))
    queries = QueryService(read_session_factory=factory)

    first = queries.list_events(limit=1)
    assert first.events == ordered[:1]
    assert first.next_after_event_id == ordered[0].event.id
    assert first.latest_offset == 2
    assert first.has_more
    second = queries.list_events(after_event_id=first.next_after_event_id, limit=1)
    assert second.events == ordered[1:]
    assert not second.has_more

    with pytest.raises(QueryNotFoundError, match="Event"):
        queries.list_events(after_event_id=new_id())
    with pytest.raises(ValueError, match="between 1 and 1000"):
        queries.list_events(limit=0)
    assert all(session.closed for session in factory.sessions)
