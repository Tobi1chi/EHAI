"""Human inbox projections. Source objects continue to own decisions and state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ehai import ID, JsonValue, normalize_id, parse_utc_datetime, utc_now
from ehai.application.interventions import list_interventions, validate_intervention_reply_context
from ehai.application.ports import ReadSession
from ehai.application.queries import QueryNotFoundError
from ehai.application.runtime_control import (
    RuntimeControlError,
    RuntimeControlService,
    WorkerRequestDetail,
)
from ehai.application.sanitization import redact_sensitive_text
from ehai.application.worker_request_forms import WorkerRequestForm
from ehai.domain.checking import CheckRun, CheckRunStatus
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.goal import Goal, Project
from ehai.domain.planning import PlanNodeStatus

InboxKind = Literal["intervention", "human_check", "worker_request", "note"]
INBOX_KINDS = ("intervention", "human_check", "worker_request", "note")


@dataclass(frozen=True, slots=True)
class InboxOwner:
    project_id: ID
    project_name: str
    goal_id: ID
    goal_objective: str
    run_id: ID | None
    run_status: RunStatus | None
    plan_node_id: ID | None
    node_title: str | None
    attempt_id: ID | None


@dataclass(frozen=True, slots=True)
class InboxEvidence:
    artifact_id: ID
    sha256: str | None


@dataclass(frozen=True, slots=True)
class InboxAction:
    operation: Literal[
        "reply-intervention",
        "decide-human-check",
        "resolve-worker-request",
        "decline-worker-request",
        "add-note-message",
        "decide-note",
    ]
    label: str
    effect: str
    input_fields: tuple[str, ...]
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class InboxItem:
    kind: InboxKind
    request_id: ID
    owner: InboxOwner
    source_status: str
    pending: bool
    actionable: bool
    unavailable_reason: str | None
    created_at: datetime | None
    first_observed_at: datetime | None
    question: str
    evidence_summary: str | None
    evidence: tuple[InboxEvidence, ...]
    request_token: str | None
    actions: tuple[InboxAction, ...]
    next_step: str
    worker_form: WorkerRequestForm | None
    disposition: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class InboxWorkerSource:
    observed_at: datetime
    status: Literal["available", "partial", "unavailable"]
    unavailable_attempt_ids: tuple[ID, ...]


@dataclass(frozen=True, slots=True)
class InboxListView:
    observed_at: datetime
    event_offset: int
    worker_requests: InboxWorkerSource
    items: tuple[InboxItem, ...]


@dataclass(frozen=True, slots=True)
class InboxDetailView:
    observed_at: datetime
    event_offset: int
    worker_requests: InboxWorkerSource
    item: InboxItem


class InboxQuery:
    def __init__(
        self,
        session: ReadSession,
        runtime: RuntimeControlService | None,
        *,
        include_notes: bool = True,
    ) -> None:
        self.session = session
        self.runtime = runtime
        self.include_notes = include_notes
        self.offset = session.events.latest_offset()
        self.observed_at = utc_now()
        self.unavailable: set[ID] = set()
        self.dispositions: dict[tuple[str, str, str], dict[str, JsonValue]] = {}
        for stored in session.events.list_events():
            if stored.event.type is not EventType.PLAN_REVISION_APPROVED:
                continue
            succession = stored.event.payload.get("succession")
            if not isinstance(succession, dict):
                continue
            resolutions = succession.get("resolutions")
            if not isinstance(resolutions, list):
                continue
            for resolution in resolutions:
                if not isinstance(resolution, dict):
                    continue
                key = tuple(str(resolution.get(k)) for k in ("kind", "request_id", "request_token"))
                self.dispositions[(key[0], key[1], key[2])] = {
                    "disposition": resolution.get("disposition"),
                    "reason": resolution.get("reason"),
                    "actor": succession.get("actor"),
                    "plan_revision_id": stored.event.correlation_id,
                }

    def source(self) -> InboxWorkerSource:
        return InboxWorkerSource(
            utc_now(),
            "unavailable"
            if self.runtime is None or not self.runtime.runtime_health().loop_active
            else "partial"
            if self.unavailable
            else "available",
            tuple(sorted(self.unavailable)),
        )

    def list_items(self, project_id: ID | None, run_id: ID | None) -> InboxListView:
        items = self._items(project_id, run_id)
        return InboxListView(
            self.observed_at,
            self.offset,
            self.source(),
            tuple(
                sorted(
                    (i for i in items if i.pending),
                    key=lambda i: (
                        i.created_at or i.first_observed_at or self.observed_at,
                        i.kind,
                        i.request_id,
                    ),
                )
            ),
        )

    def get(self, kind: InboxKind, request_id: ID) -> InboxDetailView:
        if kind not in INBOX_KINDS:
            raise ValueError("Unknown inbox request kind")
        request_id = normalize_id(request_id)
        # Runtime IDs only live for this host process, including its response receipts.
        if kind == "worker_request" and self.runtime is not None:
            detail = self.runtime.get_worker_request_detail(request_id)
            if detail is not None:
                attempt = self.session.states.get_attempt(detail.request.attempt_id)
                if attempt is not None:
                    owner = self._owners(None, attempt.run_id)[0]
                    item = self._worker(*owner, detail)
                    if not detail.available and detail.request.status.value == "pending":
                        self.unavailable.add(attempt.attempt_id)
                    return InboxDetailView(self.observed_at, self.offset, self.source(), item)
        for item in self._items(None, None):
            if item.kind == kind and item.request_id == request_id:
                return InboxDetailView(self.observed_at, self.offset, self.source(), item)
        raise QueryNotFoundError("InboxRequest", request_id)

    def _owners(self, project_id: ID | None, run_id: ID | None) -> list[tuple[Project, Goal, Run]]:
        states = self.session.states
        if project_id is not None:
            project_id = normalize_id(project_id)
            if states.get_project(project_id) is None:
                raise QueryNotFoundError("Project", project_id)
        if run_id is not None:
            run_id = normalize_id(run_id)
            run = states.get_run(run_id)
            if run is None:
                raise QueryNotFoundError("Run", run_id)
            goal = states.get_goal(run.goal_id)
            if goal is None:
                raise QueryNotFoundError("Goal", run.goal_id)
            if project_id is not None and goal.project_id != project_id:
                raise ValueError("Run does not belong to the requested Project")
            project_id = goal.project_id
        return [
            (project, goal, run)
            for project in states.list_projects()
            if project_id is None or project.project_id == project_id
            for goal in states.list_goals(project.project_id)
            for run in states.list_runs(goal.goal_id)
            if run_id is None or run.run_id == run_id
        ]

    def _items(self, project_id: ID | None, run_id: ID | None) -> list[InboxItem]:
        items: list[InboxItem] = []
        for project, goal, run in self._owners(project_id, run_id):
            items.extend(
                self._intervention(project, goal, run, n)
                for n in list_interventions(self.session.events, run.run_id)
            )
            items.extend(
                self._human_check(project, goal, run, c)
                for c in self.session.states.list_check_runs(run.run_id)
                if c.human_request is not None
            )
            for attempt in self.session.states.list_attempts(run.run_id):
                if attempt.status is not AttemptStatus.RUNNING:
                    continue
                if self.runtime is None or not self.runtime.worker_requests_available(attempt):
                    self.unavailable.add(attempt.attempt_id)
                    continue
                try:
                    for request in self.runtime.list_waiting_requests(attempt.attempt_id):
                        detail = self.runtime.get_worker_request_detail(request.worker_request_id)
                        if detail is not None:
                            if not detail.available:
                                self.unavailable.add(attempt.attempt_id)
                            items.append(self._worker(project, goal, run, detail))
                except (RuntimeControlError, ValueError, KeyError):
                    self.unavailable.add(attempt.attempt_id)
        if self.include_notes:
            items.extend(self._notes(project_id, run_id))
        return items

    def _notes(self, project_id: ID | None, run_id: ID | None) -> list[InboxItem]:
        from typing import cast

        from ehai.application.notes import NoteDocument, note_documents, note_stale_reason

        items: list[InboxItem] = []
        for note in note_documents(self.session):
            source = cast(NoteDocument, note["source"])
            if (project_id is not None and source["project_id"] != project_id) or (
                run_id is not None and source["run_id"] != run_id
            ):
                continue
            goal = self.session.states.get_goal(cast(ID, source["goal_id"]))
            project = self.session.states.get_project(cast(ID, source["project_id"]))
            assert goal is not None and project is not None
            source_run = cast(ID | None, source["run_id"])
            run = None if source_run is None else self.session.states.get_run(source_run)
            reason = note_stale_reason(self.session, note)
            pending = note["status"] != "resolved"
            actions: tuple[InboxAction, ...] = ()
            if note["status"] == "open":
                arguments: dict[str, JsonValue] = {
                    "note_id": note["note_id"],
                    "request_token": note["request_token"],
                }
                actions = (
                    InboxAction(
                        "add-note-message",
                        "Discuss",
                        "Append a message without changing execution",
                        ("idempotency_key", "actor", "message"),
                        arguments,
                    ),
                    InboxAction(
                        "decide-note",
                        "Decide",
                        "Close, continue a request, or draft; approval remains separate",
                        ("idempotency_key", "actor", "action", "message", "passed"),
                        arguments,
                    ),
                )
            items.append(
                InboxItem(
                    "note",
                    cast(ID, note["note_id"]),
                    InboxOwner(
                        project.project_id,
                        project.name,
                        goal.goal_id,
                        goal.objective,
                        source_run,
                        None if run is None else run.status,
                        None,
                        None,
                        None,
                    ),
                    str(note["status"]),
                    pending,
                    bool(actions),
                    reason,
                    parse_utc_datetime(str(note["created_at"])),
                    None,
                    str(note["question"]),
                    str(note["evidence"]),
                    (),
                    str(note["request_token"]),
                    actions,
                    "Read and decide explicitly; discussion never unblocks execution",
                    None,
                    cast(dict[str, JsonValue] | None, note["decision"]),
                )
            )
        return items

    def _owner(
        self, project: Project, goal: Goal, run: Run, node_id: ID, attempt_id: ID
    ) -> InboxOwner:
        plan = self.session.states.get_execution_plan(run.run_id)
        node = (
            None
            if plan is None
            else next((n for n in plan.nodes if n.plan_node_id == node_id), None)
        )
        return InboxOwner(
            project.project_id,
            project.name,
            goal.goal_id,
            goal.objective,
            run.run_id,
            run.status,
            node_id,
            None if node is None else node.title,
            attempt_id,
        )

    def _baseline_reason(self, goal: Goal, run: Run, node_id: ID) -> str | None:
        if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}:
            return "Run is terminal"
        if any(
            r.predecessor_run_id == run.run_id for r in self.session.states.list_runs(goal.goal_id)
        ):
            return "Run has a successor; inspect its recorded dispositions"
        plan = self.session.states.get_execution_plan(run.run_id)
        if plan is None or not any(n.plan_node_id == node_id for n in plan.nodes):
            return "Request node is no longer in the current execution graph"
        if (
            goal.completion_contract is None
            or goal.completion_contract.completion_contract_id != plan.completion_contract_id
        ):
            return "Run approval is no longer the Goal's current contract"
        return None

    def _intervention(
        self, project: Project, goal: Goal, run: Run, notice: dict[str, JsonValue]
    ) -> InboxItem:
        request_id = normalize_id(str(notice["intervention_id"]))
        node_id = normalize_id(str(notice["plan_node_id"]))
        attempt_id = normalize_id(str(notice["attempt_id"]))
        token = str(notice["request_token"])
        disposition = self.dispositions.get(("intervention", str(request_id), token))
        reason = self._baseline_reason(goal, run, node_id)
        pending = notice["status"] == "open" and disposition is None and reason is None
        if reason is None:
            try:
                validate_intervention_reply_context(self.session, notice)
            except ValueError as error:
                reason = redact_sensitive_text(str(error))
                plan = self.session.states.get_execution_plan(run.run_id)
                pending = (
                    pending
                    and plan is not None
                    and any(
                        n.plan_node_id == node_id and n.status is PlanNodeStatus.SUSPENDED
                        for n in plan.nodes
                    )
                )
        if disposition is not None:
            reason = "Request has an explicit disposition in a later approval"
        if notice["status"] != "open":
            reason = "Intervention already has a reply"
        evidence = []
        artifact_ids = notice.get("artifact_ids")
        for value in artifact_ids if isinstance(artifact_ids, list) else []:
            artifact_id = normalize_id(str(value))
            artifact = self.session.states.get_artifact(artifact_id)
            evidence.append(
                InboxEvidence(artifact_id, None if artifact is None else artifact.sha256)
            )
        actions = (
            ()
            if not pending or reason
            else (
                InboxAction(
                    "reply-intervention",
                    "Reply",
                    "Resolve this question within the existing approval boundary",
                    ("idempotency_key", "actor", "message"),
                    {"intervention_id": request_id, "request_token": token},
                ),
            )
        )
        return InboxItem(
            "intervention",
            request_id,
            self._owner(project, goal, run, node_id, attempt_id),
            str(notice["status"]),
            pending,
            bool(actions),
            reason,
            parse_utc_datetime(str(notice["occurred_at"])),
            None,
            str(notice["needed"]),
            str(notice["reason"]) + "\n" + str(notice["evidence"]),
            tuple(evidence),
            token,
            actions,
            _next_step(run),
            None,
            disposition,
        )

    def _human_check(self, project: Project, goal: Goal, run: Run, check: CheckRun) -> InboxItem:
        request = check.human_request
        assert request is not None
        disposition = self.dispositions.get(
            ("human_check", str(check.check_run_id), request.request_token)
        )
        reason = self._baseline_reason(goal, run, check.plan_node_id)
        pending = (
            check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
            and check.human_decision is None
            and disposition is None
            and reason is None
        )
        plan = self.session.states.get_execution_plan(run.run_id)
        node = (
            None
            if plan is None
            else next((n for n in plan.nodes if n.plan_node_id == check.plan_node_id), None)
        )
        if reason is None and (node is None or node.status is not PlanNodeStatus.VERIFYING):
            reason = "Node is no longer awaiting verification"
            pending = False
        if reason is None:
            attempts = [
                a
                for a in self.session.states.list_attempts(run.run_id)
                if a.plan_node_id == check.plan_node_id
            ]
            adoption = (
                None
                if check.adoption_id is None
                else self.session.states.get_result_adoption(check.adoption_id)
            )
            current_attempt = (
                attempts[-1].attempt_id
                if attempts
                else adoption.source_attempt_id
                if adoption is not None
                else None
            )
            contract = goal.completion_contract
            if (
                current_attempt != check.attempt_id
                or contract is None
                or (
                    request.completion_contract_id != contract.completion_contract_id
                    or request.completion_contract_version != contract.version
                )
            ):
                reason = "Request producer or approval version is no longer current"
                pending = False
        if disposition is not None:
            reason = "Request has an explicit disposition in a later approval"
        elif not pending and reason is None:
            reason = "Human Check is no longer pending"
        elif run.status is not RunStatus.RUNNING and reason is None:
            reason = "Resume this Run explicitly before deciding its human Check"
        actions = (
            ()
            if not pending or reason
            else tuple(
                InboxAction(
                    "decide-human-check",
                    "Pass" if passed else "Reject",
                    "Record a decision for this evidence version; the core evaluates the Gate",
                    ("idempotency_key", "actor", "comment"),
                    {
                        "check_run_id": check.check_run_id,
                        "request_token": request.request_token,
                        "passed": passed,
                    },
                )
                for passed in (True, False)
            )
        )
        return InboxItem(
            "human_check",
            check.check_run_id,
            self._owner(project, goal, run, check.plan_node_id, check.attempt_id),
            check.status.value,
            pending,
            bool(actions),
            reason,
            check.started_at or check.created_at,
            None,
            request.question,
            None,
            tuple(InboxEvidence(e.artifact_id, e.sha256) for e in request.evidence),
            request.request_token,
            actions,
            _next_step(run),
            None,
            disposition,
        )

    def _worker(
        self, project: Project, goal: Goal, run: Run, detail: WorkerRequestDetail
    ) -> InboxItem:
        request = detail.request
        attempt = self.session.states.get_attempt(request.attempt_id)
        if attempt is None:
            raise QueryNotFoundError("Attempt", request.attempt_id)
        reason = self._baseline_reason(goal, run, attempt.plan_node_id)
        pending = request.status.value == "pending" and reason is None and detail.available
        reason = reason or detail.unavailable_reason
        actions: list[InboxAction] = []
        if pending and detail.available and reason is None:
            if detail.form.resolution_schema is not None:
                actions.append(
                    InboxAction(
                        "resolve-worker-request",
                        "Respond",
                        "Answer this live Worker request within its displayed scope",
                        ("idempotency_key", "resolution"),
                        {"worker_request_id": request.worker_request_id},
                    )
                )
            actions.append(
                InboxAction(
                    "decline-worker-request",
                    "Decline",
                    "Decline only this Worker request",
                    ("idempotency_key",),
                    {"worker_request_id": request.worker_request_id},
                )
            )
        return InboxItem(
            "worker_request",
            request.worker_request_id,
            self._owner(project, goal, run, attempt.plan_node_id, attempt.attempt_id),
            request.status.value,
            pending,
            bool(actions),
            reason,
            None,
            detail.first_observed_at,
            request.summary,
            None,
            (),
            None,
            tuple(actions),
            _next_step(run),
            detail.form,
            None,
        )


def _next_step(run: Run) -> str:
    return (
        "Run is paused. Use resume-run separately when appropriate, then re-read the Run."
        if run.status is RunStatus.PAUSED
        else "Re-read this request and its Run to confirm progress or the remaining blocker."
    )
