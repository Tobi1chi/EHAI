"""Read-only evidence context for reviewing a retained process draft."""

from __future__ import annotations

from dataclasses import dataclass

from ehai import ID, JsonValue, normalize_id
from ehai.application.interventions import process_intervention_context
from ehai.application.ports import StoredEvent, UnitOfWork
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import CheckRun, CheckSpec
from ehai.domain.events import EventType
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal
from ehai.domain.planning import PlanNode, PlanNodeStatus, PlanRevision, PlanRevisionStatus
from ehai.domain.process import (
    ProcessRevision,
    process_approval_identity,
)
from ehai.domain.process_drafts import ProcessDraft, ProcessDraftStatus


class ProcessReviewContextError(ValueError):
    """A retained process draft cannot be reviewed from a coherent read snapshot."""


@dataclass(frozen=True, slots=True)
class RetainedResultEvidence:
    """One reused completed candidate node and only its retained execution evidence."""

    node: PlanNode
    attempt: Attempt | None
    artifacts: tuple[Artifact, ...]
    check_runs: tuple[CheckRun, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.node, PlanNode):
            raise TypeError("RetainedResultEvidence node must be a PlanNode")
        if self.attempt is not None and not isinstance(self.attempt, Attempt):
            raise TypeError("RetainedResultEvidence attempt must be an Attempt or None")
        artifacts = tuple(self.artifacts)
        check_runs = tuple(self.check_runs)
        if not all(isinstance(artifact, Artifact) for artifact in artifacts):
            raise TypeError("RetainedResultEvidence artifacts must contain Artifacts")
        if not all(isinstance(check_run, CheckRun) for check_run in check_runs):
            raise TypeError("RetainedResultEvidence check_runs must contain CheckRuns")
        if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise ValueError("RetainedResultEvidence artifacts must not contain duplicates")
        if len({check_run.check_run_id for check_run in check_runs}) != len(check_runs):
            raise ValueError("RetainedResultEvidence check_runs must not contain duplicates")
        if self.attempt is None and (artifacts or check_runs):
            raise ValueError("Missing retained Attempt cannot carry execution evidence")
        if self.attempt is not None and (
            self.attempt.plan_node_id != self.node.plan_node_id
            or self.attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise ValueError("RetainedResultEvidence Attempt does not match its completed node")
        if self.attempt is not None:
            if any(
                artifact.run_id != self.attempt.run_id
                or artifact.plan_node_id != self.node.plan_node_id
                or artifact.attempt_id != self.attempt.attempt_id
                for artifact in artifacts
            ):
                raise ValueError("RetainedResultEvidence Artifact is outside its Attempt scope")
            if any(
                check_run.run_id != self.attempt.run_id
                or check_run.plan_node_id != self.node.plan_node_id
                or check_run.attempt_id != self.attempt.attempt_id
                for check_run in check_runs
            ):
                raise ValueError("RetainedResultEvidence CheckRun is outside its Attempt scope")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "check_runs", check_runs)


@dataclass(frozen=True, slots=True)
class ProcessReviewContext:
    """Immutable, provider-neutral input for an independent process boundary review."""

    draft: ProcessDraft
    run: Run
    goal: Goal
    approved: PlanRevision
    contract: CompletionContract
    checks: tuple[CheckSpec, ...]
    previous: ProcessRevision
    retained_results: tuple[RetainedResultEvidence, ...]
    authorization_events: tuple[StoredEvent, ...]
    interventions: tuple[dict[str, JsonValue], ...]

    def __post_init__(self) -> None:
        values = {
            "draft": self.draft,
            "run": self.run,
            "goal": self.goal,
            "approved": self.approved,
            "contract": self.contract,
            "previous": self.previous,
        }
        expected = {
            "draft": ProcessDraft,
            "run": Run,
            "goal": Goal,
            "approved": PlanRevision,
            "contract": CompletionContract,
            "previous": ProcessRevision,
        }
        for name, value in values.items():
            if not isinstance(value, expected[name]):
                raise TypeError(f"ProcessReviewContext {name} has an invalid type")
        if self.draft.status is not ProcessDraftStatus.READY or self.draft.candidate is None:
            raise ValueError("ProcessReviewContext requires a ready process draft")
        if self.run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
            raise ValueError("ProcessReviewContext requires a running or paused Run")
        checks = tuple(self.checks)
        retained_results = tuple(self.retained_results)
        authorization_events = tuple(self.authorization_events)
        if not all(isinstance(check, CheckSpec) for check in checks):
            raise TypeError("ProcessReviewContext checks must contain CheckSpecs")
        if not all(isinstance(item, RetainedResultEvidence) for item in retained_results):
            raise TypeError("ProcessReviewContext retained_results must contain evidence records")
        if not all(isinstance(item, StoredEvent) for item in authorization_events):
            raise TypeError("ProcessReviewContext authorization_events must contain StoredEvents")
        if len({check.check_id for check in checks}) != len(checks):
            raise ValueError("ProcessReviewContext checks must not contain duplicates")
        if len({event.event.id for event in authorization_events}) != len(authorization_events):
            raise ValueError(
                "ProcessReviewContext authorization_events must not contain duplicates"
            )
        if any(event.event.run_id != self.run.run_id for event in authorization_events):
            raise ValueError("ProcessReviewContext authorization event belongs to another Run")
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "retained_results", retained_results)
        object.__setattr__(self, "authorization_events", authorization_events)
        object.__setattr__(self, "interventions", tuple(dict(item) for item in self.interventions))


def load_process_review_context(uow: UnitOfWork, draft_id: ID) -> ProcessReviewContext:
    """Load a ready draft and its bounded, durable review evidence without side effects."""

    draft = uow.states.get_process_draft(normalize_id(draft_id))
    if draft is None:
        raise ProcessReviewContextError(f"ProcessDraft {draft_id} is not persisted")
    if draft.status is not ProcessDraftStatus.READY or draft.candidate is None:
        raise ProcessReviewContextError("Process review requires a ready draft with a candidate")

    run = uow.states.get_run(draft.run_id)
    if run is None:
        raise ProcessReviewContextError(f"Run {draft.run_id} is not persisted")
    if run.status not in {RunStatus.RUNNING, RunStatus.PAUSED}:
        raise ProcessReviewContextError("Process review requires a running or paused Run")

    current = uow.states.get_execution_plan(run.run_id)
    if current is None or current != draft.base_execution_plan:
        raise ProcessReviewContextError("ProcessDraft base execution plan is no longer current")
    previous = uow.states.get_active_process_revision(run.run_id)
    if previous is None or previous.process_revision_id != draft.parent_process_revision_id:
        raise ProcessReviewContextError(
            "ProcessDraft parent is no longer the active ProcessRevision"
        )

    candidate = draft.candidate
    if (
        candidate.run_id != run.run_id
        or candidate.parent_process_revision_id != previous.process_revision_id
        or candidate.version != previous.version + 1
        or process_approval_identity(candidate.graph)
        != process_approval_identity(draft.base_execution_plan)
    ):
        raise ProcessReviewContextError("ProcessDraft candidate does not match its retained parent")

    approved = uow.states.get_plan_revision(run.plan_revision_id)
    if approved is None or approved.status is not PlanRevisionStatus.APPROVED:
        raise ProcessReviewContextError("Run has no original approved PlanRevision")
    goal = uow.states.get_goal(run.goal_id)
    if goal is None:
        raise ProcessReviewContextError(f"Goal {run.goal_id} is not persisted")
    contract = uow.states.get_completion_contract(approved.completion_contract_id)
    if contract is None or not contract.is_confirmed:
        raise ProcessReviewContextError(
            "Original approved CompletionContract is missing or unconfirmed"
        )
    if (
        approved.goal_id != run.goal_id
        or approved.completion_contract_id != contract.completion_contract_id
        or approved.completion_contract_version != contract.version
        or contract.goal_id != goal.goal_id
        or goal.completion_contract != contract
        or process_approval_identity(approved)
        != process_approval_identity(draft.base_execution_plan)
    ):
        raise ProcessReviewContextError(
            "ProcessDraft does not retain the original approval identity"
        )

    checks = _approved_check_specs(uow, approved)
    retained_results = _retained_results(uow, run, previous, candidate.graph)
    authorization_events = _authorization_events(uow, run)
    interventions = process_intervention_context(uow, draft, require_current=True)
    return ProcessReviewContext(
        draft=draft,
        run=run,
        goal=goal,
        approved=approved,
        contract=contract,
        checks=checks,
        previous=previous,
        retained_results=retained_results,
        authorization_events=authorization_events,
        interventions=interventions,
    )


def _approved_check_specs(uow: UnitOfWork, approved: PlanRevision) -> tuple[CheckSpec, ...]:
    required_ids = tuple(
        dict.fromkeys(check_id for node in approved.nodes for check_id in node.required_check_ids)
    )
    specs_by_id = {
        spec.check_id: spec for spec in uow.states.list_check_specs(approved.plan_revision_id)
    }
    missing = tuple(check_id for check_id in required_ids if check_id not in specs_by_id)
    if missing:
        raise ProcessReviewContextError(
            "Original approved PlanRevision is missing CheckSpecs: " + ", ".join(missing)
        )
    checks = tuple(specs_by_id[check_id] for check_id in required_ids)
    if any(not check.required for check in checks):
        raise ProcessReviewContextError(
            "Original approved PlanRevision references an optional Check"
        )
    return checks


def _retained_results(
    uow: UnitOfWork,
    run: Run,
    previous: ProcessRevision,
    candidate: PlanRevision,
) -> tuple[RetainedResultEvidence, ...]:
    previous_node_ids = {node.plan_node_id for node in previous.graph.nodes}
    reusable_completed = tuple(
        node
        for node in candidate.nodes
        if node.plan_node_id in previous_node_ids and node.status is PlanNodeStatus.COMPLETED
    )
    if not reusable_completed:
        return ()

    attempts = tuple(
        sorted(
            (
                attempt
                for attempt in uow.states.list_attempts(run.run_id)
                if attempt.run_id == run.run_id
            ),
            key=lambda item: item.sequence,
        )
    )
    artifacts = uow.states.list_artifacts_for_run(run.run_id)
    check_runs = uow.states.list_check_runs(run.run_id)
    results: list[RetainedResultEvidence] = []
    for node in reusable_completed:
        succeeded = tuple(
            attempt
            for attempt in attempts
            if attempt.plan_node_id == node.plan_node_id
            and attempt.status is AttemptStatus.SUCCEEDED
        )
        attempt = succeeded[-1] if succeeded else None
        if attempt is None:
            results.append(RetainedResultEvidence(node, None, (), ()))
            continue
        node_artifacts = tuple(
            artifact
            for artifact in artifacts
            if artifact.run_id == run.run_id
            and artifact.plan_node_id == node.plan_node_id
            and artifact.attempt_id == attempt.attempt_id
        )
        node_check_runs = tuple(
            check_run
            for check_run in check_runs
            if check_run.run_id == run.run_id
            and check_run.plan_node_id == node.plan_node_id
            and check_run.attempt_id == attempt.attempt_id
        )
        results.append(RetainedResultEvidence(node, attempt, node_artifacts, node_check_runs))
    return tuple(results)


def _authorization_events(uow: UnitOfWork, run: Run) -> tuple[StoredEvent, ...]:
    """Return explicit RunStarted authorization/config facts; never infer defaults."""

    return tuple(
        stored
        for stored in uow.events.list_events()
        if stored.event.run_id == run.run_id
        and stored.event.type is EventType.RUN_STARTED
        and _contains_authorization_fact(stored)
    )


def _contains_authorization_fact(stored: StoredEvent) -> bool:
    payload = stored.event.payload
    return any(
        key in payload
        for key in {
            "execution_config",
            "execution_config_fingerprint",
            "authorization",
            "permissions",
        }
    )
