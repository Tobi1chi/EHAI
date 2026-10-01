"""Durable, single-attempt Pi fallback using the routing lab's existing records."""

from ehai import JsonValue, new_id, utc_now
from ehai.application.ports import StateConflictError
from ehai.application.routing_lab import RoutingLabService
from ehai.application.routing_models import (
    RoutingFallbackExecution,
    RoutingLab,
    RoutingProjectChange,
    RoutingRecipe,
    RoutingReplayCase,
    RoutingRequest,
    RoutingSlowAnswer,
    StartRoutingReplayRequest,
)
from ehai.application.sanitization import redact_sensitive_text
from ehai.domain.events import EventType


class RoutingFallbackService:
    def __init__(self, labs: RoutingLabService) -> None:
        self.labs = labs

    def recover(self) -> None:
        # One owner process per core database, as with the other local runtimes.
        with self.labs.store.transaction(write=True) as tx:
            for row in tx.list("request"):
                record = RoutingRequest.model_validate(row)
                fallback = record.fallback
                if fallback is not None and fallback.status == "running":
                    fallback = fallback.model_copy(
                        update={
                            "status": "interrupted",
                            "completed_at": utc_now(),
                            "error": "host_stopped_before_confirmed_completion",
                        }
                    )
                    self.labs._save_request(tx, record.model_copy(update={"fallback": fallback}))
                if (
                    fallback is not None
                    and fallback.project_change is not None
                    and fallback.project_change.status == "dispatching"
                ):
                    change = fallback.project_change.model_copy(
                        update={
                            "status": "unknown",
                            "error": "host_stopped_during_project_handoff",
                        }
                    )
                    self.labs._save_request(
                        tx,
                        record.model_copy(
                            update={
                                "fallback": fallback.model_copy(update={"project_change": change}),
                            }
                        ),
                    )

    def claim(self, request_id: str, model: str, configuration_hash: str) -> RoutingRequest | None:
        with self.labs.store.transaction(write=True) as tx:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            lab = RoutingLab.model_validate(tx.get("lab", record.lab_id))
            if (
                lab.fallback_mode != "pi"
                or record.status != "escalated"
                or record.case_role != "learning"
                or record.fallback is not None
            ):
                return None
            record = record.model_copy(
                update={
                    "fallback": RoutingFallbackExecution(
                        attempt_id=str(new_id()),
                        session_id=str(new_id()),
                        model=model,
                        configuration_hash=configuration_hash,
                        status="running",
                        started_at=utc_now(),
                        change_project_binding=lab.change_project,
                    ),
                    "updated_at": utc_now(),
                }
            )
            self.labs._save_request(tx, record)
            return record

    def complete(
        self,
        request_id: str,
        attempt_id: str,
        answer: RoutingSlowAnswer,
        facts: dict[str, JsonValue],
    ) -> None:
        # Apply the answer/candidate only after Pi's finish tool AND native settled event.
        with self.labs.store.transaction(write=True) as tx:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            fallback = record.fallback
            if (
                fallback is None
                or fallback.attempt_id != attempt_id
                or fallback.status != "running"
            ):
                raise StateConflictError("Fallback no longer owns this request")
            candidate_id = None
            project_change = None
            if answer.project_change is not None:
                if fallback.change_project_binding is None:
                    raise StateConflictError("This request has no authorized change Project")
                project_change = RoutingProjectChange(
                    target=fallback.change_project_binding,
                    objective=redact_sensitive_text(answer.project_change),
                    status="pending",
                )
            if answer.candidate is not None:
                recipe = answer.candidate
                candidate = RoutingRecipe(
                    recipe_id=str(new_id()),
                    lab_id=record.lab_id,
                    project_id=record.project_id,
                    name=redact_sensitive_text(recipe.name),
                    applicability=redact_sensitive_text(recipe.applicability),
                    target=recipe.target,
                    status="candidate",
                    source_request_ids=[record.request_id],
                    approved_by=None,
                    approval_evidence=None,
                    created_at=utc_now(),
                )
                candidate_id = candidate.recipe_id
                tx.save("recipe", candidate_id, candidate)
                self.labs._event(tx, EventType.ROUTING_CANDIDATE_CREATED, candidate_id, candidate)
            fallback = fallback.model_copy(
                update={
                    "status": "completed",
                    "completed_at": utc_now(),
                    "candidate_id": candidate_id,
                    "project_change": project_change,
                    "change_reason": None
                    if answer.change_reason is None
                    else redact_sensitive_text(answer.change_reason),
                }
            )
            result: dict[str, JsonValue] = {
                "evidence_kind": "pi_agent_report",
                "model": fallback.model,
                "response": redact_sensitive_text(answer.response),
                "evidence": redact_sensitive_text(answer.evidence),
                "needs_human": answer.needs_human,
                "facts": facts,
                "previous_result": record.result,
                "project_change_pending": project_change is not None,
            }
            self.labs._save_request(
                tx,
                record.model_copy(
                    update={
                        "status": "escalated" if answer.needs_human else "completed",
                        "route_source": "system2",
                        "result": result,
                        "fallback": fallback,
                        "updated_at": utc_now(),
                    }
                ),
            )

    def fail(self, request_id: str, *, interrupted: bool, error: str) -> None:
        with self.labs.store.transaction(write=True) as tx:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            fallback = record.fallback
            if fallback is not None and fallback.status == "running":
                fallback = fallback.model_copy(
                    update={
                        "status": "interrupted" if interrupted else "failed",
                        "error": error,
                        "completed_at": utc_now(),
                    }
                )
                self.labs._save_request(
                    tx,
                    record.model_copy(
                        update={
                            "fallback": fallback,
                            "updated_at": utc_now(),
                        }
                    ),
                )

    def replay(self, record: RoutingRequest) -> None:
        fallback = record.fallback
        if (
            fallback is None
            or fallback.status != "completed"
            or fallback.candidate_id is None
            or fallback.replay_id is not None
        ):
            return
        if fallback.replay_cases is not None:
            self._start_replay(record, fallback)
            return
        view = self.labs.lab(record.lab_id)
        candidate = next(r for r in view.recipes if r.recipe_id == fallback.candidate_id)
        validation = view.lab.fallback_validation_cases
        if not any(c.expected_target == candidate.target for c in validation):
            self._replay_link(record.request_id, None, "awaiting_independent_candidate_positive")
            return
        cases = [
            RoutingReplayCase(request_id=record.request_id, expected_choice=candidate.recipe_id)
        ]
        for case in validation:
            if case.expected_target == "escalate":
                choice = "escalate"
            elif case.expected_target == candidate.target:
                choice = candidate.recipe_id
            else:
                matches = [
                    r.recipe_id
                    for r in view.recipes
                    if r.status == "active" and r.target == case.expected_target
                ]
                if len(matches) != 1:
                    self._replay_link(record.request_id, None, "validation_target_is_ambiguous")
                    return
                choice = matches[0]
            cases.append(RoutingReplayCase(request_id=case.request_id, expected_choice=choice))
        with self.labs.store.transaction(write=True) as tx:
            current = RoutingRequest.model_validate(tx.get("request", record.request_id))
            assert current.fallback is not None
            fallback = current.fallback
            if fallback.replay_cases is None:
                fallback = fallback.model_copy(update={"replay_cases": cases})
                self.labs._save_request(tx, current.model_copy(update={"fallback": fallback}))
        self._start_replay(record, fallback)

    def _start_replay(self, record: RoutingRequest, fallback: RoutingFallbackExecution) -> None:
        assert fallback.candidate_id is not None and fallback.replay_cases is not None
        try:
            replay = self.labs.start_replay(
                record.lab_id,
                StartRoutingReplayRequest(
                    idempotency_key="fallback:" + fallback.attempt_id,
                    candidate_id=fallback.candidate_id,
                    cases=fallback.replay_cases,
                ),
            )
        except (StateConflictError, ValueError):
            self._replay_link(record.request_id, None, "replay_not_ready_or_cases_invalid")
            return
        self._replay_link(record.request_id, replay.replay_id, None)

    def _replay_link(self, request_id: str, replay_id: str | None, error: str | None) -> None:
        with self.labs.store.transaction(write=True) as tx:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            fallback = record.fallback
            if fallback is None or fallback.replay_id is not None:
                return
            if fallback.replay_error == error and replay_id is None:
                return
            self.labs._save_request(
                tx,
                record.model_copy(
                    update={
                        "fallback": fallback.model_copy(
                            update={"replay_id": replay_id, "replay_error": error}
                        ),
                        "updated_at": utc_now(),
                    }
                ),
            )
