"""Explicit approval and provenance for moving a Goal to a successor Run."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from hashlib import sha256

from ehai import ID, JsonValue, normalize_id
from ehai.application.interventions import list_interventions
from ehai.application.ports import StateConflictError, UnitOfWork
from ehai.domain.adoptions import ResultAdoption
from ehai.domain.checking import CheckRunStatus, HumanCheckEvidence
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, Run, RunStatus
from ehai.domain.planning import PlanRevision
from ehai.domain.process import ProcessRevision


def successor_source(
    uow: UnitOfWork, plan: PlanRevision, predecessor_id: ID
) -> tuple[Run, ProcessRevision]:
    source = uow.states.get_run(predecessor_id)
    process = uow.states.get_active_process_revision(predecessor_id)
    if (
        source is None
        or process is None
        or source.goal_id != plan.goal_id
        or source.status is not RunStatus.PAUSED
        or plan.supersedes_plan_revision_id != source.plan_revision_id
    ):
        raise StateConflictError(
            "Successor requires a drained predecessor and its direct revised plan"
        )
    if any(
        a.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
        for a in uow.states.list_attempts(predecessor_id)
    ) or any(
        c.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING} and c.human_request is None
        for c in uow.states.list_check_runs(predecessor_id)
    ):
        raise StateConflictError(
            "Drain predecessor Attempts and automatic Checks before succession"
        )
    if any(r.predecessor_run_id == predecessor_id for r in uow.states.list_runs(plan.goal_id)):
        raise StateConflictError("The predecessor already has a successor Run")
    if any(
        r.run_id != predecessor_id and r.status in {RunStatus.PENDING, RunStatus.RUNNING}
        for r in uow.states.list_runs(plan.goal_id)
    ):
        raise StateConflictError("Another Run of this Goal is active")
    return source, process


def pending_successor_requests(uow: UnitOfWork, source_id: ID) -> list[JsonValue]:
    """Keep pending questions and their exact tokens; never translate them into Gate passes."""
    pending: list[JsonValue] = []
    for notice in list_interventions(uow.events, source_id):
        if notice["status"] == "open":
            pending.append(
                {
                    "kind": "intervention",
                    "request_id": notice["intervention_id"],
                    "request_token": notice["request_token"],
                    "source": notice,
                }
            )
    for check in uow.states.list_check_runs(source_id):
        request = check.human_request
        if request is not None and check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}:
            pending.append(
                {
                    "kind": "human_check",
                    "request_id": check.check_run_id,
                    "request_token": request.request_token,
                    "source": {
                        "question": request.question,
                        "plan_node_id": check.plan_node_id,
                        "evidence": [
                            {"artifact_id": e.artifact_id, "sha256": e.sha256}
                            for e in request.evidence
                        ],
                    },
                }
            )
    return pending


def approve_succession(
    uow: UnitOfWork, plan: PlanRevision, document: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Validate the user's explicit dispositions before changing the approved contract."""
    _keys(
        document,
        {"predecessor_run_id", "expected_process_revision_id", "actor", "reason", "resolutions"},
    )
    source, process = successor_source(
        uow, plan, normalize_id(_text(document, "predecessor_run_id"))
    )
    if process.process_revision_id != normalize_id(_text(document, "expected_process_revision_id")):
        raise StateConflictError("Predecessor process changed before approval")
    _text(document, "actor")
    _text(document, "reason")
    pending = pending_successor_requests(uow, source.run_id)
    expected = {(p["kind"], p["request_id"]): p for p in pending if isinstance(p, dict)}
    resolutions = document["resolutions"]
    if not isinstance(resolutions, list):
        raise ValueError("resolutions must be an array")
    seen: set[tuple[str, str]] = set()
    for item in resolutions:
        if not isinstance(item, dict):
            raise ValueError("Every resolution must be an object")
        _keys(item, {"kind", "request_id", "request_token", "disposition", "reason"})
        key = (_text(item, "kind"), str(normalize_id(_text(item, "request_id"))))
        request = expected.get(key)
        if key in seen or request is None or item["request_token"] != request["request_token"]:
            raise StateConflictError("Resolution does not match an exact pending request")
        seen.add(key)
        _text(item, "reason")
        if item["disposition"] not in {"resolved", "superseded"}:
            raise ValueError("Resolution disposition must be resolved or superseded")
        notice = request["source"]
        if (
            isinstance(notice, dict)
            and notice.get("kind") == "external_effects"
            and item["disposition"] != "resolved"
        ):
            raise StateConflictError(
                "Unknown external effects require explicit resolution, not supersession"
            )
    if seen != set(expected):
        raise StateConflictError(
            "Every pending human Check and intervention requires a disposition"
        )
    return {
        **document,
        "predecessor_run_id": source.run_id,
        "expected_process_revision_id": process.process_revision_id,
        "actor": _text(document, "actor"),
        "reason": _text(document, "reason"),
        "pending_requests": pending,
    }


def approved_succession(uow: UnitOfWork, plan_id: ID) -> dict[str, JsonValue] | None:
    for stored in reversed(uow.events.list_events()):
        if (
            stored.event.type is EventType.PLAN_REVISION_APPROVED
            and stored.event.correlation_id == plan_id
        ):
            value = stored.event.payload.get("succession")
            return value if isinstance(value, dict) else None
    return None


def prepare_succession(
    uow: UnitOfWork, plan: PlanRevision, predecessor_id: ID | None
) -> tuple[Run | None, ProcessRevision | None, dict[str, JsonValue] | None]:
    approved = approved_succession(uow, plan.plan_revision_id)
    if predecessor_id is None:
        if approved is not None:
            raise StateConflictError("This approval requires an explicit predecessor Run")
        return None, None, None
    source, process = successor_source(uow, plan, predecessor_id)
    pending = pending_successor_requests(uow, predecessor_id)
    if approved is None:
        if pending:
            raise StateConflictError("Pending predecessor requests were not covered by approval")
        return (
            source,
            process,
            {
                "predecessor_run_id": predecessor_id,
                "expected_process_revision_id": process.process_revision_id,
                "pending_requests": [],
            },
        )
    if (
        approved["predecessor_run_id"] != predecessor_id
        or approved["expected_process_revision_id"] != process.process_revision_id
        or approved["pending_requests"] != pending
    ):
        raise StateConflictError("Predecessor context no longer matches the explicit approval")
    return source, process, approved


def adopt_successor_results(
    uow: UnitOfWork,
    *,
    source: Run,
    process: ProcessRevision,
    target: Run,
    selections: list[JsonValue],
    artifact_reader: Callable[[ID], bytes],
    id_factory: Callable[[], ID],
    clock: Callable[[], datetime],
) -> tuple[ResultAdoption, ...]:
    """Derive immutable evidence from the source, never accept user-forged producer hashes."""
    seen_sources: set[ID] = set()
    seen_targets: set[ID] = set()
    records: list[ResultAdoption] = []
    attempts = uow.states.list_attempts(source.run_id)
    for selection in selections:
        if not isinstance(selection, dict):
            raise ValueError("Result adoptions must be objects")
        _keys(selection, {"source_plan_node_id", "target_plan_node_id", "reason"})
        source_id = normalize_id(_text(selection, "source_plan_node_id"))
        target_id = normalize_id(_text(selection, "target_plan_node_id"))
        if source_id in seen_sources or target_id in seen_targets:
            raise ValueError("Result adoption must map distinct source and target blocks")
        seen_sources.add(source_id)
        seen_targets.add(target_id)
        matches = [a for a in attempts if a.plan_node_id == source_id]
        attempt = max(matches, key=lambda a: a.sequence) if matches else None
        if attempt is None or attempt.status is not AttemptStatus.SUCCEEDED:
            raise StateConflictError("Selected block has no latest successful producer")
        evidence = []
        for artifact_id in attempt.artifact_ids:
            artifact = uow.states.get_artifact(artifact_id)
            if artifact is None:
                raise StateConflictError("Selected result Artifact is missing")
            content = artifact_reader(artifact_id)
            if (
                len(content) != artifact.size_bytes
                or sha256(content).hexdigest() != artifact.sha256
            ):
                raise StateConflictError("Selected result Artifact bytes changed")
            evidence.append(HumanCheckEvidence(artifact_id, artifact.sha256))
        record = ResultAdoption(
            adoption_id=id_factory(),
            target_run_id=target.run_id,
            target_plan_revision_id=target.plan_revision_id,
            target_plan_node_id=target_id,
            source_run_id=source.run_id,
            source_plan_revision_id=source.plan_revision_id,
            source_process_revision_id=process.process_revision_id,
            source_plan_node_id=source_id,
            source_attempt_id=attempt.attempt_id,
            evidence=tuple(evidence),
            reason=_text(selection, "reason"),
            created_at=clock(),
        )
        uow.states.put_result_adoption(record)
        records.append(record)
    return tuple(records)


def _text(document: Mapping[str, JsonValue], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 16000:
        raise ValueError(f"{key} must be nonblank text of at most 16000 bytes")
    return value.strip()


def _keys(document: Mapping[str, JsonValue], expected: set[str]) -> None:
    if set(document) != expected:
        raise ValueError(
            "Invalid succession fields: " + ", ".join(sorted(set(document) ^ expected))
        )
