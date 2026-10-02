"""A bounded System-1/System-2 experiment using approved read-only recipes.

Jev uses the independent Connector queue. System 2 is an external agent using these
public operations; this service never embeds a second Planner or grants write authority.
"""

from collections.abc import Callable
from typing import TypeVar

from ehai import JsonValue, new_id, normalize_id, utc_now
from ehai.application.connector_models import ConnectorModel, InvokeConnectorRequest
from ehai.application.connectors import ConnectorService
from ehai.application.idempotency import content_fingerprint, record_result, recorded_result
from ehai.application.ports import CommandReceipt, StateConflictError
from ehai.application.routing_models import (
    ConfigureRoutingFallbackRequest,
    ConfigureRoutingProjectRequest,
    CreateRoutingLabRequest,
    JevRoutingInput,
    JevRoutingJudgement,
    PauseRoutingRecipeRequest,
    ProposeRoutingRecipeRequest,
    PublishRoutingRecipeRequest,
    ResolveRoutingRequest,
    RoutingFeedback,
    RoutingFeedbackRequest,
    RoutingLab,
    RoutingLabMetrics,
    RoutingLabView,
    RoutingProjectBinding,
    RoutingRecipe,
    RoutingReplay,
    RoutingReplayEntry,
    RoutingRequest,
    RoutingTarget,
    StartRoutingReplayRequest,
    SubmitRoutingRequest,
)
from ehai.application.routing_store import RoutingDocumentKind, RoutingStore, RoutingTransaction
from ehai.application.sanitization import redact_sensitive_text, sanitize_json_object
from ehai.domain.events import Event, EventType

T = TypeVar("T", bound=ConnectorModel)
_ESCALATE = (
    "No approved recipe fits, or this request needs writing, planning, "
    "clarification or human judgement."
)


def _receipt_conflict(receipt: CommandReceipt) -> Exception:
    del receipt
    return StateConflictError("Routing idempotency key belongs to different content")


class RoutingLabService:
    def __init__(
        self,
        store: RoutingStore,
        connectors: ConnectorService,
        read_query: Callable[[str, RoutingTarget], dict[str, JsonValue]],
        *,
        fallback_available: bool = False,
        project_resolver: Callable[[str, str], RoutingProjectBinding] | None = None,
    ) -> None:
        self.store = store
        self.connectors = connectors
        self.read_query = read_query
        self.fallback_available = fallback_available
        self.project_resolver = project_resolver

    def configure_project(
        self, lab_id: str, request: ConfigureRoutingProjectRequest
    ) -> RoutingLabView:
        binding = None
        if request.api_url is not None and request.project_id is not None:
            if self.project_resolver is None:
                raise StateConflictError("Host cannot bind a change Project")
            binding = self.project_resolver(request.api_url, request.project_id)

        def action(tx: RoutingTransaction) -> RoutingLabView:
            lab = RoutingLab.model_validate(tx.get("lab", lab_id))
            lab = lab.model_copy(update={"change_project": binding})
            tx.save("lab", lab_id, lab)
            return RoutingLabView(lab=lab, recipes=self._recipes(tx, lab_id))

        return self._command(
            "change-project:" + lab_id, request, request.idempotency_key, RoutingLabView, action
        )

    def _command(
        self,
        scope: str,
        request: ConnectorModel,
        key: str,
        model: type[T],
        action: Callable[[RoutingTransaction], T],
    ) -> T:
        """``scope`` is "<operation>:<target>"; a key is unique within it."""
        operation = scope.partition(":")[0]
        fingerprint = content_fingerprint(request.model_dump(mode="json"))
        with self.store.transaction(write=True) as tx:
            old = recorded_result(
                tx.receipts,
                key,
                operation,
                fingerprint,
                conflict=_receipt_conflict,
                scope="routing:" + scope,
            )
            if old is not None:
                return model.model_validate(old)
            result = action(tx)
            record_result(
                tx.receipts,
                key,
                operation,
                fingerprint,
                result.model_dump(mode="json"),
                created_at=utc_now(),
                scope="routing:" + scope,
            )
            return result

    def get(self, kind: RoutingDocumentKind, entity_id: str, model: type[T]) -> T:
        with self.store.transaction(write=False) as tx:
            return model.model_validate(tx.get(kind, entity_id))

    def create(self, project_id: str, request: CreateRoutingLabRequest) -> RoutingLabView:
        if request.fallback_mode == "pi" and not self.fallback_available:
            raise StateConflictError("Host has no configured Pi routing fallback")
        if request.fallback_mode == "pi" and request.mode != "read_only":
            raise ValueError("Pi fallback requires read_only mode; shadow does not execute queries")
        connection = self.connectors.get(str(normalize_id(request.connector_id)))
        if connection.project_id != project_id or connection.manifest.connector_type != "jev":
            raise ValueError("Use a Jev connector registered in the same Project")
        if not any(
            a.name == "routing.choose" and a.version == "1" and a.read_only
            for a in connection.manifest.actions
        ):
            raise ValueError("Connector must expose routing.choose v1 as a read-only action")

        def action(tx: RoutingTransaction) -> RoutingLabView:
            lab = RoutingLab(
                lab_id=str(new_id()),
                project_id=project_id,
                name=request.name,
                connector_id=connection.connector_id,
                mode=request.mode,
                confidence_threshold=request.confidence_threshold,
                probability_threshold=request.probability_threshold,
                max_pending=request.max_pending,
                catalog_version=1,
                created_at=utc_now(),
                fallback_mode=request.fallback_mode,
            )
            tx.save("lab", lab.lab_id, lab)
            recipes = [
                RoutingRecipe(
                    recipe_id=str(new_id()),
                    lab_id=lab.lab_id,
                    project_id=project_id,
                    name=r.name,
                    applicability=redact_sensitive_text(r.applicability),
                    target=r.target,
                    status="active",
                    source_request_ids=[],
                    approved_by=request.actor,
                    approval_evidence="Explicit experiment seed approval",
                    created_at=utc_now(),
                )
                for r in request.approved_recipes
            ]
            for recipe in recipes:
                tx.save("recipe", recipe.recipe_id, recipe)
            self._event(tx, EventType.ROUTING_CATALOG_CHANGED, lab.lab_id, lab)
            return RoutingLabView(lab=lab, recipes=recipes)

        return self._command(
            "create:" + project_id, request, request.idempotency_key, RoutingLabView, action
        )

    def lab(self, lab_id: str) -> RoutingLabView:
        with self.store.transaction(write=False) as tx:
            lab = RoutingLab.model_validate(tx.get("lab", lab_id))
            return RoutingLabView(lab=lab, recipes=self._recipes(tx, lab_id))

    def automatic_labs(self) -> list[RoutingLab]:
        with self.store.transaction(write=False) as tx:
            return [
                lab
                for row in tx.list("lab")
                if (lab := RoutingLab.model_validate(row)).fallback_mode == "pi"
            ]

    def configure_fallback(
        self, lab_id: str, request: ConfigureRoutingFallbackRequest
    ) -> RoutingLabView:
        if request.mode == "pi" and not self.fallback_available:
            raise StateConflictError("Host has no configured Pi routing fallback")

        def action(tx: RoutingTransaction) -> RoutingLabView:
            lab = RoutingLab.model_validate(tx.get("lab", lab_id))
            if request.mode == "pi" and lab.mode != "read_only":
                raise ValueError("Pi fallback requires a read_only lab")
            cases = []
            seen: set[str] = set()
            for case in request.validation_cases:
                identity = str(normalize_id(case.request_id))
                row = RoutingRequest.model_validate(tx.get("request", identity))
                if row.lab_id != lab_id or row.case_role != "validation" or identity in seen:
                    raise ValueError("Use distinct validation requests from this lab")
                seen.add(identity)
                cases.append(case.model_copy(update={"request_id": identity}))
            lab = lab.model_copy(
                update={"fallback_mode": request.mode, "fallback_validation_cases": cases}
            )
            tx.save("lab", lab_id, lab)
            return RoutingLabView(lab=lab, recipes=self._recipes(tx, lab_id))

        return self._command(
            "configure-fallback:" + lab_id, request, request.idempotency_key, RoutingLabView, action
        )

    @staticmethod
    def _recipes(tx: RoutingTransaction, lab_id: str) -> list[RoutingRecipe]:
        return [RoutingRecipe.model_validate(r) for r in tx.list("recipe", lab_id)]

    def requests(self, lab_id: str) -> list[RoutingRequest]:
        with self.store.transaction(write=False) as tx:
            tx.get("lab", lab_id)
            return [RoutingRequest.model_validate(r) for r in tx.list("request", lab_id)]

    def submit(self, lab_id: str, request: SubmitRoutingRequest) -> RoutingRequest:
        def action(tx: RoutingTransaction) -> RoutingRequest:
            lab = RoutingLab.model_validate(tx.get("lab", lab_id))
            pending = sum(
                r["status"] in {"queued", "routing", "escalated"}
                for r in tx.list("request", lab_id)
            )
            if pending >= lab.max_pending:
                raise StateConflictError("Routing experiment pending-request capacity reached")
            now = utc_now()
            record = RoutingRequest(
                request_id=str(new_id()),
                lab_id=lab_id,
                project_id=lab.project_id,
                message=redact_sensitive_text(request.message),
                case_role=request.case_role,
                explicit_recipe_id=None
                if request.explicit_recipe_id is None
                else str(normalize_id(request.explicit_recipe_id)),
                force_slow=request.force_slow,
                catalog_version=lab.catalog_version,
                catalog_snapshot=[r for r in self._recipes(tx, lab_id) if r.status == "active"],
                status="queued",
                route_source="none",
                reason=None,
                connector_call_id=None,
                judgement=None,
                selected_recipe_id=None,
                result=None,
                feedback=None,
                created_at=now,
                updated_at=now,
            )
            self._save_request(tx, record)
            return record

        return self._command(
            "submit:" + lab_id, request, request.idempotency_key, RoutingRequest, action
        )

    @staticmethod
    def _criteria(recipes: list[RoutingRecipe]) -> dict[str, str]:
        return {**{r.recipe_id: r.applicability for r in recipes}, "escalate": _ESCALATE}

    def _queue(
        self, lab: RoutingLab, identity: str, message: str, recipes: list[RoutingRecipe]
    ) -> str:
        call = self.connectors.invoke(
            lab.connector_id,
            InvokeConnectorRequest(
                idempotency_key="routing:" + identity,
                action="routing.choose",
                action_version="1",
                inputs=JevRoutingInput(
                    message=message, criteria=self._criteria(recipes)
                ).model_dump(mode="json"),
            ),
        )
        return call.call_id

    def _judgement(self, call_id: str) -> tuple[bool, JevRoutingJudgement | None, str | None]:
        call = self.connectors.get_call(call_id)
        if call.status in {"queued", "claimed"}:
            return False, None, None
        if call.status != "completed" or call.output is None:
            return True, None, "jev_call_failed_or_unknown"
        try:
            return True, JevRoutingJudgement.model_validate(call.output), None
        except ValueError:
            return True, None, "invalid_jev_result"

    @staticmethod
    def _choice(
        lab: RoutingLab, recipes: list[RoutingRecipe], judgement: JevRoutingJudgement
    ) -> tuple[str | None, str]:
        allowed = {r.recipe_id for r in recipes} | {"escalate"}
        if set(judgement.probabilities) != allowed or judgement.choice not in allowed:
            return None, "invalid_choice_set"
        if judgement.choice == "escalate":
            return None, "jev_escalated"
        if (
            judgement.confidence < lab.confidence_threshold
            or judgement.probabilities[judgement.choice] < lab.probability_threshold
        ):
            return None, "uncertain_judgement"
        return judgement.choice, "approved_recipe_selected"

    def advance(self, lab_id: str) -> RoutingLabMetrics:
        """Explicit experiment tick; stable call keys recover intent-to-queue interruptions."""
        lab = self.lab(lab_id).lab
        for record in self.requests(lab_id):
            if record.status not in {"queued", "routing"}:
                continue
            if record.force_slow or not record.catalog_snapshot:
                self._finish(record.request_id, None, "none", "forced_or_empty_catalog", None)
                continue
            if record.explicit_recipe_id is not None:
                selected = next(
                    (
                        r.recipe_id
                        for r in record.catalog_snapshot
                        if r.recipe_id == record.explicit_recipe_id
                    ),
                    None,
                )
                self._finish(
                    record.request_id,
                    selected,
                    "rule",
                    "explicit_binding" if selected else "unapproved_binding",
                    None,
                )
                continue
            call_id = record.connector_call_id or self._queue(
                lab, record.request_id, record.message, record.catalog_snapshot
            )
            with self.store.transaction(write=True) as tx:
                current = RoutingRequest.model_validate(tx.get("request", record.request_id))
                if current.status not in {"queued", "routing"}:
                    continue
                current = current.model_copy(
                    update={
                        "connector_call_id": call_id,
                        "status": "routing",
                        "updated_at": utc_now(),
                    }
                )
                self._save_request(tx, current)
            ready, judgement, error = self._judgement(call_id)
            if not ready:
                continue
            selected, reason = (
                (None, error or "invalid_jev_result")
                if judgement is None
                else self._choice(lab, record.catalog_snapshot, judgement)
            )
            self._finish(record.request_id, selected, "jev", reason, judgement)
        self._advance_replays(lab)
        return self.metrics(lab_id)

    def _finish(
        self,
        request_id: str,
        selected: str | None,
        source: str,
        reason: str,
        judgement: JevRoutingJudgement | None,
    ) -> None:
        with self.store.transaction(write=True) as tx:
            request = RoutingRequest.model_validate(tx.get("request", request_id))
            if request.status not in {"queued", "routing"}:
                return
            lab = RoutingLab.model_validate(tx.get("lab", request.lab_id))
            recipe = next(
                (
                    r
                    for r in self._recipes(tx, request.lab_id)
                    if r.recipe_id == selected and r.status == "active"
                ),
                None,
            )
            updates: dict[str, object] = {
                "route_source": source,
                "reason": reason,
                "judgement": judgement,
                "selected_recipe_id": selected,
                "updated_at": utc_now(),
            }
            if recipe is None:
                updates["status"] = "escalated"
                if selected is not None:
                    updates["reason"] = "recipe_paused_before_execution"
            elif lab.mode == "shadow":
                updates["status"] = "shadow"
            else:
                try:
                    data, truncated = sanitize_json_object(
                        self.read_query(lab.project_id, recipe.target), max_bytes=64000
                    )
                    updates.update(
                        status="completed",
                        result={
                            "evidence_kind": "core_query",
                            "data": data,
                            "truncated": truncated,
                        },
                    )
                except Exception:
                    updates.update(status="escalated", reason="read_query_failed")
            self._save_request(tx, request.model_copy(update=updates))

    def resolve(self, request_id: str, request: ResolveRoutingRequest) -> RoutingRequest:
        def action(tx: RoutingTransaction) -> RoutingRequest:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            if record.status != "escalated":
                raise StateConflictError("Only an escalated request can be resolved by System 2")
            if record.fallback is not None and record.fallback.status == "running":
                raise StateConflictError("Pi fallback is still running")
            record = record.model_copy(
                update={
                    "status": "completed",
                    "route_source": "system2",
                    "result": {
                        "evidence_kind": "external_agent_report",
                        "actor": request.actor,
                        "response": redact_sensitive_text(request.response),
                        "evidence": redact_sensitive_text(request.evidence),
                        "previous_result": record.result,
                    },
                    "updated_at": utc_now(),
                }
            )
            self._save_request(tx, record)
            return record

        return self._command(
            "resolve:" + request_id, request, request.idempotency_key, RoutingRequest, action
        )

    def feedback(self, request_id: str, request: RoutingFeedbackRequest) -> RoutingRequest:
        def action(tx: RoutingTransaction) -> RoutingRequest:
            record = RoutingRequest.model_validate(tx.get("request", request_id))
            if (
                record.status in {"queued", "routing"}
                or record.feedback is not None
                or (record.fallback is not None and record.fallback.status == "running")
            ):
                raise StateConflictError(
                    "Feedback needs a routed request without an existing label"
                )
            feedback = RoutingFeedback(
                actor=request.actor,
                outcome=request.outcome,
                explanation=redact_sensitive_text(request.explanation),
                recorded_at=utc_now(),
            )
            record = record.model_copy(update={"feedback": feedback, "updated_at": utc_now()})
            if request.outcome != "correct":
                record = record.model_copy(
                    update={"status": "escalated", "reason": "feedback_" + request.outcome}
                )
            if request.outcome != "correct" and record.selected_recipe_id is not None:
                recipe = RoutingRecipe.model_validate(tx.get("recipe", record.selected_recipe_id))
                if recipe.status == "active":
                    tx.save(
                        "recipe",
                        recipe.recipe_id,
                        recipe.model_copy(
                            update={
                                "status": "paused",
                                "paused_by": request.actor,
                                "pause_reason": feedback.explanation,
                            }
                        ),
                    )
                    self._bump_catalog(tx, recipe.lab_id)
            self._save_request(tx, record)
            return record

        return self._command(
            "feedback:" + request_id, request, request.idempotency_key, RoutingRequest, action
        )

    def propose(self, lab_id: str, request: ProposeRoutingRecipeRequest) -> RoutingRecipe:
        def action(tx: RoutingTransaction) -> RoutingRecipe:
            lab = RoutingLab.model_validate(tx.get("lab", lab_id))
            source_ids = [str(normalize_id(i)) for i in request.source_request_ids]
            for request_id in source_ids:
                source = RoutingRequest.model_validate(tx.get("request", request_id))
                if source.lab_id != lab_id or source.case_role != "learning":
                    raise ValueError("Candidate sources must be learning cases in this experiment")
            recipe = RoutingRecipe(
                recipe_id=str(new_id()),
                lab_id=lab_id,
                project_id=lab.project_id,
                name=request.recipe.name,
                applicability=redact_sensitive_text(request.recipe.applicability),
                target=request.recipe.target,
                status="candidate",
                source_request_ids=source_ids,
                approved_by=None,
                approval_evidence=None,
                created_at=utc_now(),
            )
            tx.save("recipe", recipe.recipe_id, recipe)
            self._event(tx, EventType.ROUTING_CANDIDATE_CREATED, recipe.recipe_id, recipe)
            return recipe

        return self._command(
            "propose:" + lab_id, request, request.idempotency_key, RoutingRecipe, action
        )

    def start_replay(self, lab_id: str, request: StartRoutingReplayRequest) -> RoutingReplay:
        def action(tx: RoutingTransaction) -> RoutingReplay:
            lab = RoutingLab.model_validate(tx.get("lab", lab_id))
            candidate = RoutingRecipe.model_validate(
                tx.get("recipe", str(normalize_id(request.candidate_id)))
            )
            if candidate.lab_id != lab_id or candidate.status not in {"candidate", "paused"}:
                raise ValueError("Replay requires a candidate/paused recipe in this experiment")
            if any(r["status"] == "pending" for r in tx.list("replay", lab_id)):
                raise StateConflictError("Finish the pending replay before starting another")
            recipes = [r for r in self._recipes(tx, lab_id) if r.status == "active"] + [candidate]
            if len(recipes) > 20:
                raise ValueError("Experiment catalogue is limited to 20 active choices")
            entries = []
            seen: set[str] = set()
            learning_messages = {
                str(tx.get("request", i)["message"]) for i in candidate.source_request_ids
            }
            held_out = False
            for case in request.cases:
                source = RoutingRequest.model_validate(
                    tx.get("request", str(normalize_id(case.request_id)))
                )
                if source.lab_id != lab_id or source.message in seen:
                    raise ValueError("Replay cases must be distinct requests from this experiment")
                seen.add(source.message)
                if case.expected_choice not in self._criteria(recipes):
                    raise ValueError("Expected choice is outside the replay catalogue")
                held_out |= (
                    source.case_role == "validation"
                    and source.message not in learning_messages
                    and case.expected_choice == candidate.recipe_id
                )
                entries.append(
                    RoutingReplayEntry(
                        request_id=source.request_id,
                        message=source.message,
                        case_role=source.case_role,
                        expected_choice=case.expected_choice,
                        connector_call_id=None,
                        judgement=None,
                        passed=None,
                        reason=None,
                    )
                )
            if not held_out:
                raise ValueError(
                    "Replay needs a candidate-positive validation case not used to propose it"
                )
            replay = RoutingReplay(
                replay_id=str(new_id()),
                lab_id=lab_id,
                project_id=lab.project_id,
                candidate_id=candidate.recipe_id,
                catalog_version=lab.catalog_version,
                catalog_snapshot=recipes,
                status="pending",
                entries=entries,
                created_at=utc_now(),
            )
            tx.save("replay", replay.replay_id, replay)
            return replay

        return self._command(
            "replay:" + lab_id, request, request.idempotency_key, RoutingReplay, action
        )

    def _advance_replays(self, lab: RoutingLab) -> None:
        with self.store.transaction(write=False) as tx:
            pending = [
                RoutingReplay.model_validate(r)
                for r in tx.list("replay", lab.lab_id)
                if r["status"] == "pending"
            ]
        for replay in pending:
            entries = []
            for index, entry in enumerate(replay.entries):
                if entry.passed is not None:
                    entries.append(entry)
                    continue
                call_id = entry.connector_call_id or self._queue(
                    lab, f"{replay.replay_id}:{index}", entry.message, replay.catalog_snapshot
                )
                ready, judgement, error = self._judgement(call_id)
                updates: dict[str, object] = {"connector_call_id": call_id}
                if ready:
                    selected, reason = (
                        (None, error or "invalid_jev_result")
                        if judgement is None
                        else self._choice(lab, replay.catalog_snapshot, judgement)
                    )
                    updates.update(
                        judgement=judgement,
                        reason=reason,
                        passed=judgement is not None
                        and reason != "invalid_choice_set"
                        and (selected or "escalate") == entry.expected_choice,
                    )
                entries.append(entry.model_copy(update=updates))
            with self.store.transaction(write=True) as tx:
                current = RoutingReplay.model_validate(tx.get("replay", replay.replay_id))
                if current.status != "pending":
                    continue
                status = (
                    "pending"
                    if any(e.passed is None for e in entries)
                    else "passed"
                    if all(e.passed for e in entries)
                    else "failed"
                )
                updated = current.model_copy(update={"entries": entries, "status": status})
                tx.save("replay", replay.replay_id, updated)

    def publish(self, recipe_id: str, request: PublishRoutingRecipeRequest) -> RoutingRecipe:
        def action(tx: RoutingTransaction) -> RoutingRecipe:
            recipe = RoutingRecipe.model_validate(tx.get("recipe", recipe_id))
            lab = RoutingLab.model_validate(tx.get("lab", recipe.lab_id))
            replay = RoutingReplay.model_validate(
                tx.get("replay", str(normalize_id(request.replay_id)))
            )
            if (
                recipe.status not in {"candidate", "paused"}
                or replay.candidate_id != recipe_id
                or replay.status != "passed"
                or replay.catalog_version != lab.catalog_version
            ):
                raise StateConflictError(
                    "Publication requires a passed replay against the current catalogue"
                )
            if not request.allow_protocol_trial and any(
                e.judgement is None or e.judgement.evidence_source != "typesafe"
                for e in replay.entries
            ):
                raise StateConflictError(
                    "Protocol-trial publication must be explicitly acknowledged"
                )
            recipe = recipe.model_copy(
                update={
                    "status": "active",
                    "approved_by": request.actor,
                    "approval_evidence": replay.replay_id,
                    "paused_by": None,
                    "pause_reason": None,
                }
            )
            tx.save("recipe", recipe_id, recipe)
            self._bump_catalog(tx, recipe.lab_id)
            return recipe

        return self._command(
            "publish:" + recipe_id, request, request.idempotency_key, RoutingRecipe, action
        )

    def pause(self, recipe_id: str, request: PauseRoutingRecipeRequest) -> RoutingRecipe:
        def action(tx: RoutingTransaction) -> RoutingRecipe:
            recipe = RoutingRecipe.model_validate(tx.get("recipe", recipe_id))
            if recipe.status != "active":
                raise StateConflictError("Only an active recipe can be paused")
            recipe = recipe.model_copy(
                update={
                    "status": "paused",
                    "paused_by": request.actor,
                    "pause_reason": redact_sensitive_text(request.reason),
                }
            )
            tx.save("recipe", recipe_id, recipe)
            self._bump_catalog(tx, recipe.lab_id)
            return recipe

        return self._command(
            "pause:" + recipe_id, request, request.idempotency_key, RoutingRecipe, action
        )

    def metrics(self, lab_id: str) -> RoutingLabMetrics:
        rows = self.requests(lab_id)
        by_recipe: dict[str, dict[str, int]] = {}
        for row in rows:
            if row.selected_recipe_id is not None:
                counts = by_recipe.setdefault(
                    row.selected_recipe_id, {"selected": 0, "reviewed": 0, "misroutes": 0}
                )
                counts["selected"] += 1
                counts["reviewed"] += row.feedback is not None
                counts["misroutes"] += (
                    row.feedback is not None and row.feedback.outcome == "misroute"
                )
        return RoutingLabMetrics(
            request_count=len(rows),
            routing_count=sum(r.status in {"queued", "routing"} for r in rows),
            escalated_count=sum(r.status == "escalated" for r in rows),
            system2_resolved_count=sum(
                r.route_source == "system2" and r.status == "completed" for r in rows
            ),
            fast_completed_count=sum(
                r.status == "completed" and r.route_source in {"rule", "jev"} for r in rows
            ),
            shadow_count=sum(r.status == "shadow" for r in rows),
            reviewed_count=sum(r.feedback is not None for r in rows),
            misroute_count=sum(
                r.feedback is not None and r.feedback.outcome == "misroute" for r in rows
            ),
            provider_judgements=sum(
                r.judgement is not None and r.judgement.evidence_source == "typesafe" for r in rows
            ),
            protocol_trial_judgements=sum(
                r.judgement is not None and r.judgement.evidence_source == "protocol_trial"
                for r in rows
            ),
            by_recipe=by_recipe,
        )

    def _bump_catalog(self, tx: RoutingTransaction, lab_id: str) -> None:
        lab = RoutingLab.model_validate(tx.get("lab", lab_id))
        lab = lab.model_copy(update={"catalog_version": lab.catalog_version + 1})
        tx.save("lab", lab_id, lab)
        self._event(tx, EventType.ROUTING_CATALOG_CHANGED, lab_id, lab)

    def _save_request(self, tx: RoutingTransaction, record: RoutingRequest) -> None:
        tx.save("request", record.request_id, record)
        self._event(tx, EventType.ROUTING_REQUEST_CHANGED, record.request_id, record)

    @staticmethod
    def _event(
        tx: RoutingTransaction, kind: EventType, identity: str, value: ConnectorModel
    ) -> None:
        tx.emit(Event(type=kind, correlation_id=identity, payload=value.model_dump(mode="json")))
