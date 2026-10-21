"""Thread navigation projected from Goal, planning, execution and inbox facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ehai import ID, JsonValue, normalize_id, utc_now
from ehai.application.inbox import InboxItem, InboxQuery
from ehai.application.ports import ReadSession, StoredEvent
from ehai.application.queries import QueryNotFoundError
from ehai.domain.events import EventType
from ehai.domain.execution import RunStatus
from ehai.domain.goal import Goal, GoalStatus, Project
from ehai.domain.planning import PlanRevisionStatus

ThreadPhase = Literal[
    "needs_attention",
    "running",
    "paused",
    "awaiting_approval",
    "planning",
    "completed",
    "failed",
    "idle",
]
ThreadActionKind = Literal[
    "start_discussion",
    "continue_discussion",
    "review_plan",
    "handle_inbox",
    "watch_run",
    "resume_run",
    "review_result",
    "none",
]
ThreadTimelineKind = Literal[
    "goal_created",
    "discussion_message",
    "plan_revision_created",
    "plan_approved",
    "run_started",
    "run_status_changed",
    "node_completed",
    "inbox_opened",
    "inbox_resolved",
    "succession_started",
    "integration_created",
]
_RUN_STATUS_CHANGES = {
    EventType.RUN_PAUSED: RunStatus.PAUSED,
    EventType.RUN_RESUMED: RunStatus.RUNNING,
    EventType.RUN_COMPLETED: RunStatus.COMPLETED,
    EventType.RUN_FAILED: RunStatus.FAILED,
    EventType.RUN_CANCELLED: RunStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class ThreadNextAction:
    kind: ThreadActionKind
    refs: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    goal_id: ID
    project_id: ID
    project_name: str
    objective: str
    goal_status: GoalStatus
    phase: ThreadPhase
    next_action: ThreadNextAction
    attention_count: int
    created_at: datetime
    last_activity_at: datetime
    plan_revision_id: ID | None
    run_id: ID | None
    run_status: RunStatus | None
    observed_at: datetime
    event_offset: int


@dataclass(frozen=True, slots=True)
class ThreadListView:
    observed_at: datetime
    event_offset: int
    threads: tuple[ThreadSummary, ...]


@dataclass(frozen=True, slots=True)
class ThreadTimelineItem:
    """A persisted fact; run_status_changed refs include the new RunStatus as status."""

    at: datetime
    kind: ThreadTimelineKind
    refs: dict[str, JsonValue]
    summary: str


@dataclass(frozen=True, slots=True)
class ThreadTimelineView:
    observed_at: datetime
    event_offset: int
    goal_id: ID
    items: tuple[ThreadTimelineItem, ...]
    next_after: int | None


class ThreadQuery:
    """All facts for a response belong to the caller's single read snapshot."""

    def __init__(self, session: ReadSession) -> None:
        self.session = session
        self.offset = session.events.latest_offset()
        self.observed_at = utc_now()
        self._projects = session.states.list_projects()
        self._goals_by_project = {
            p.project_id: session.states.list_goals(p.project_id) for p in self._projects
        }
        self._goals = {g.goal_id: g for goals in self._goals_by_project.values() for g in goals}
        self._plans_by_goal = {
            goal_id: session.states.list_plan_revisions(goal_id) for goal_id in self._goals
        }
        self._runs_by_goal = {goal_id: session.states.list_runs(goal_id) for goal_id in self._goals}
        self._events_by_goal: dict[ID, tuple[StoredEvent, ...]] = {}
        self._conversations_by_goal: dict[ID, tuple[ID, ...]] = {}
        self._index_events(session.events.list_events())

    def _index_events(self, events: tuple[StoredEvent, ...]) -> None:
        run_goals = {
            run.run_id: goal_id for goal_id, runs in self._runs_by_goal.items() for run in runs
        }
        owner_goals = {goal_id: goal_id for goal_id in self._goals}
        owner_goals.update(
            (plan.plan_revision_id, goal_id)
            for goal_id, plans in self._plans_by_goal.items()
            for plan in plans
        )
        by_correlation: dict[ID, list[StoredEvent]] = {}
        by_goal: dict[ID, dict[int, StoredEvent]] = {goal_id: {} for goal_id in self._goals}
        conversations: dict[ID, dict[ID, int]] = {}
        for stored in events:
            event = stored.event
            payload = event.payload
            value = payload.get("goal_id")
            payload_goal = ID(value) if isinstance(value, str) and value in self._goals else None
            if event.type is EventType.PLANNING_TURN_STARTED and payload_goal is not None:
                owner_goals[event.correlation_id] = payload_goal
                conversations.setdefault(payload_goal, {})[event.correlation_id] = stored.offset
            elif event.type is EventType.NOTE_CREATED:
                source = payload.get("source")
                source_goal = source.get("goal_id") if isinstance(source, dict) else None
                if isinstance(source_goal, str) and source_goal in self._goals:
                    owner_goals[event.correlation_id] = ID(source_goal)
            by_correlation.setdefault(event.correlation_id, []).append(stored)
            run_goal = None if event.run_id is None else run_goals.get(event.run_id)
            for goal_id in {run_goal, payload_goal}:
                if goal_id is not None:
                    by_goal[goal_id][stored.offset] = stored

        # Resolve correlation ownership after discovery, retaining earlier facts too.
        for owner_id, facts in by_correlation.items():
            goal_id = owner_goals.get(owner_id)
            if goal_id is not None:
                by_goal[goal_id].update((stored.offset, stored) for stored in facts)
        self._events_by_goal = {
            goal_id: tuple(facts.values()) for goal_id, facts in by_goal.items()
        }
        self._conversations_by_goal = {
            goal_id: tuple(sorted(latest, key=latest.__getitem__))
            for goal_id, latest in conversations.items()
        }

    def list_threads(self) -> ThreadListView:
        inbox = InboxQuery(self.session, None).list_items(None, None).items
        threads = [
            self._summary(project, goal, inbox)
            for project in self._projects
            for goal in self._goals_by_project[project.project_id]
        ]
        threads.sort(key=lambda t: (t.last_activity_at, t.goal_id), reverse=True)
        return ThreadListView(self.observed_at, self.offset, tuple(threads))

    def get_thread(self, goal_id: ID) -> ThreadSummary:
        project, goal = self._owner(goal_id)
        inbox = InboxQuery(self.session, None).list_items(project.project_id, None).items
        return self._summary(project, goal, inbox)

    def _owner(self, goal_id: ID) -> tuple[Project, Goal]:
        goal_id = normalize_id(goal_id)
        goal = self.session.states.get_goal(goal_id)
        if goal is None:
            raise QueryNotFoundError("Goal", goal_id)
        project = self.session.states.get_project(goal.project_id)
        if project is None:
            raise QueryNotFoundError("Project", goal.project_id)
        return project, goal

    def _events(self, goal: Goal) -> tuple[StoredEvent, ...]:
        return self._events_by_goal.get(goal.goal_id, ())

    def _summary(self, project: Project, goal: Goal, inbox: tuple[InboxItem, ...]) -> ThreadSummary:
        plans = self._plans_by_goal[goal.goal_id]
        runs = self._runs_by_goal[goal.goal_id]
        plan = plans[-1] if plans else None
        run = runs[-1] if runs else None
        conversations = self._conversations_by_goal.get(goal.goal_id, ())
        run_ids = {r.run_id for r in runs}
        pending = tuple(i for i in inbox if i.owner.run_id in run_ids)
        phase: ThreadPhase = "idle"
        action = ThreadNextAction("none", {})
        if pending:
            phase = "needs_attention"
            item = pending[0]
            action = ThreadNextAction(
                "handle_inbox",
                {"kind": item.kind, "request_id": item.request_id, "run_id": item.owner.run_id},
            )
        elif run is not None and run.status in {RunStatus.PENDING, RunStatus.RUNNING}:
            phase = "running"
            action = ThreadNextAction("watch_run", {"run_id": run.run_id})
        elif run is not None and run.status is RunStatus.PAUSED:
            phase = "paused"
            action = ThreadNextAction("resume_run", {"run_id": run.run_id})
        elif (
            plan is not None
            and plan.status is PlanRevisionStatus.DRAFT
            and not any(r.plan_revision_id == plan.plan_revision_id for r in runs)
        ):
            phase = "awaiting_approval"
        elif conversations and plan is None:
            phase = "planning"
            action = ThreadNextAction(
                "continue_discussion",
                {"goal_id": goal.goal_id, "conversation_id": conversations[-1]},
            )
        elif run is not None and run.status is RunStatus.COMPLETED:
            phase = "completed"
            action = ThreadNextAction("review_result", {"run_id": run.run_id})
        elif run is not None and run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
            phase = "failed"
        if phase == "awaiting_approval" or (phase == "idle" and plan is not None):
            assert plan is not None
            action = ThreadNextAction(
                "review_plan",
                {
                    "plan_revision_id": plan.plan_revision_id,
                    "completion_contract_id": plan.completion_contract_id,
                },
            )
        elif phase == "idle" and not runs:
            action = ThreadNextAction("start_discussion", {"goal_id": goal.goal_id})
        activity = [goal.created_at, *(s.event.occurred_at for s in self._events(goal))]
        return ThreadSummary(
            goal.goal_id,
            project.project_id,
            project.name,
            goal.objective,
            goal.status,
            phase,
            action,
            len(pending),
            goal.created_at,
            max(activity),
            None if plan is None else plan.plan_revision_id,
            None if run is None else run.run_id,
            None if run is None else run.status,
            self.observed_at,
            self.offset,
        )

    def timeline(self, goal_id: ID, *, limit: int = 100, after: int = 0) -> ThreadTimelineView:
        if not 1 <= limit <= 1000 or after < 0:
            raise ValueError("limit must be 1..1000 and after must be nonnegative")
        _, goal = self._owner(goal_id)
        events = sorted(self._events(goal), key=lambda s: (s.event.occurred_at, s.offset))
        items = [item for stored in events for item in self._timeline_items(stored)]
        end = after + limit
        return ThreadTimelineView(
            self.observed_at,
            self.offset,
            goal.goal_id,
            tuple(items[after:end]),
            end if end < len(items) else None,
        )

    def _timeline_items(self, stored: StoredEvent) -> tuple[ThreadTimelineItem, ...]:
        event = stored.event
        payload = event.payload
        refs: dict[str, JsonValue] = {"event_id": event.id}
        if event.run_id is not None:
            refs["run_id"] = event.run_id
        kind: ThreadTimelineKind
        summary: str
        if event.type is EventType.GOAL_CREATED:
            kind, summary = "goal_created", "Goal created"
            refs["goal_id"] = event.correlation_id
        elif event.type in {EventType.PLANNING_TURN_STARTED, EventType.PLANNING_TURN_COMPLETED}:
            kind = "discussion_message"
            refs.update({"conversation_id": event.correlation_id, "turn_id": payload["turn_id"]})
            field = "message" if event.type is EventType.PLANNING_TURN_STARTED else "reply"
            value = payload.get(field)
            if not isinstance(value, str):
                return ()
            speaker = "User" if field == "message" else "Planner"
            summary = f"{speaker}: {value}"
            if payload.get("plan_revision_id") is not None:
                refs["plan_revision_id"] = payload["plan_revision_id"]
        elif event.type in {EventType.PLAN_REVISION_PROPOSED, EventType.PLAN_REVISION_APPROVED}:
            kind, summary = (
                ("plan_revision_created", "Plan revision created")
                if event.type is EventType.PLAN_REVISION_PROPOSED
                else ("plan_approved", "Plan approved")
            )
            refs["plan_revision_id"] = event.correlation_id
            succession = payload.get("succession")
            resolutions = succession.get("resolutions") if isinstance(succession, dict) else None
            resolved = (
                tuple(
                    ThreadTimelineItem(
                        event.occurred_at,
                        "inbox_resolved",
                        {**refs, "kind": r["kind"], "request_id": r["request_id"]},
                        "Inbox request disposition recorded",
                    )
                    for r in resolutions
                    if isinstance(r, dict)
                    and r.get("kind") in {"intervention", "human_check"}
                    and "request_id" in r
                )
                if isinstance(resolutions, list)
                else ()
            )
            return (ThreadTimelineItem(event.occurred_at, kind, refs, summary), *resolved)
        elif event.type is EventType.RUN_STARTED:
            kind, summary = "run_started", "Run started"
        elif event.type in _RUN_STATUS_CHANGES:
            refs["status"] = _RUN_STATUS_CHANGES[event.type].value
            kind, summary = (
                "run_status_changed",
                "Run " + event.type.value.removeprefix("Run").lower(),
            )
        elif event.type is EventType.RUN_SUCCESSOR_CREATED:
            kind, summary = "succession_started", "Successor Run created"
            refs["predecessor_run_id"] = payload["predecessor_run_id"]
        elif event.type is EventType.INTEGRATION_CREATED:
            kind, summary = "integration_created", f"Code integration {payload['status']}"
            refs.update({"integration_id": event.correlation_id, "status": payload["status"]})
        elif event.type is EventType.PLAN_NODE_COMPLETED:
            kind, summary = "node_completed", "Node completed"
            refs["plan_node_id"] = event.correlation_id
            if event.run_id is not None:
                if isinstance(attempt_id := payload.get("attempt_id"), str):
                    refs["attempt_id"] = attempt_id
                    if payload.get("adoption_id") is not None:
                        refs["adoption_id"] = payload["adoption_id"]
                    attempt = self.session.states.get_attempt(normalize_id(attempt_id))
                    if attempt is not None:
                        refs["artifact_ids"] = list(attempt.artifact_ids)
                else:
                    checkpoints = [
                        c
                        for c in self.session.states.list_checkpoints(event.run_id)
                        if c.gate_decision.plan_node_id == event.correlation_id
                        and c.event_offset < stored.offset
                    ]
                    if checkpoints:
                        checkpoint = max(checkpoints, key=lambda c: c.event_offset)
                        decision = checkpoint.gate_decision
                        refs.update(
                            {
                                "checkpoint_id": checkpoint.checkpoint_id,
                                "gate_id": decision.gate_id,
                                "attempt_id": decision.attempt_id,
                                "artifact_ids": list(decision.evidence_artifact_ids),
                            }
                        )
                        if decision.adoption_id is not None:
                            refs["adoption_id"] = decision.adoption_id
        elif event.type in {EventType.INTERVENTION_OPENED, EventType.INTERVENTION_REPLIED}:
            kind, summary = (
                ("inbox_opened", "Intervention opened")
                if event.type is EventType.INTERVENTION_OPENED
                else ("inbox_resolved", "Intervention replied")
            )
            refs.update({"kind": "intervention", "request_id": event.correlation_id})
        elif event.type in {
            EventType.CHECK_STARTED,
            EventType.CHECK_PASSED,
            EventType.CHECK_FAILED,
        }:
            check = self.session.states.get_check_run(event.correlation_id)
            if check is None or check.human_request is None:
                return ()
            if event.type is EventType.CHECK_STARTED and payload.get("awaiting_human") is not True:
                return ()
            kind, summary = (
                ("inbox_opened", check.human_request.question)
                if event.type is EventType.CHECK_STARTED
                else ("inbox_resolved", "Human Check closed")
            )
            refs.update({"kind": "human_check", "request_id": check.check_run_id})
        elif event.type in {EventType.NOTE_CREATED, EventType.NOTE_DECISION_COMPLETED}:
            kind, summary = (
                ("inbox_opened", "Note opened")
                if event.type is EventType.NOTE_CREATED
                else ("inbox_resolved", "Note decision completed")
            )
            refs.update({"kind": "note", "request_id": event.correlation_id})
        else:
            return ()
        return (ThreadTimelineItem(event.occurred_at, kind, refs, summary),)
