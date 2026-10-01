"""Opt-in, read-only dual-feedback experiments; not the production Workflow engine."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from ehai import JsonValue
from ehai.application.connector_models import ConnectorModel

RoutingText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
RoutingKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
RoutingTarget = Literal["life.tasks.list", "inbox.list"]
RoutingProbability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class RoutingValidationCase(ConnectorModel):
    request_id: str
    expected_target: Literal["life.tasks.list", "inbox.list", "escalate"]


class ConfigureRoutingFallbackRequest(ConnectorModel):
    idempotency_key: RoutingKey
    mode: Literal["external", "pi"]
    validation_cases: list[RoutingValidationCase] = Field(max_length=19)


class RoutingProjectBinding(ConnectorModel):
    api_url: str
    project_id: str
    workspace: str


class ConfigureRoutingProjectRequest(ConnectorModel):
    idempotency_key: RoutingKey
    api_url: RoutingKey | None
    project_id: str | None

    @model_validator(mode="after")
    def paired_target(self) -> ConfigureRoutingProjectRequest:
        if (self.api_url is None) != (self.project_id is None):
            raise ValueError("api_url and project_id must both be provided or both be null")
        return self


class RoutingRecipeInput(ConnectorModel):
    name: RoutingKey
    applicability: RoutingText
    target: RoutingTarget


class CreateRoutingLabRequest(ConnectorModel):
    idempotency_key: RoutingKey
    name: RoutingKey
    connector_id: str
    mode: Literal["shadow", "read_only"] = "shadow"
    confidence_threshold: RoutingProbability = 0.85
    probability_threshold: RoutingProbability = 0.8
    max_pending: int = Field(default=32, ge=1, le=100, strict=True)
    actor: RoutingKey
    approved_recipes: list[RoutingRecipeInput] = Field(min_length=1, max_length=20)
    fallback_mode: Literal["external", "pi"] = "external"


class RoutingLab(ConnectorModel):
    lab_id: str
    project_id: str
    name: str
    connector_id: str
    mode: Literal["shadow", "read_only"]
    confidence_threshold: float
    probability_threshold: float
    max_pending: int
    catalog_version: int
    created_at: AwareDatetime
    fallback_mode: Literal["external", "pi"] = "external"
    fallback_validation_cases: list[RoutingValidationCase] = Field(default_factory=list)
    change_project: RoutingProjectBinding | None = None


class RoutingRecipe(ConnectorModel):
    recipe_id: str
    lab_id: str
    project_id: str
    name: str
    applicability: str
    target: RoutingTarget
    status: Literal["candidate", "active", "paused"]
    source_request_ids: list[str]
    approved_by: str | None
    approval_evidence: str | None
    paused_by: str | None = None
    pause_reason: str | None = None
    created_at: AwareDatetime


class SubmitRoutingRequest(ConnectorModel):
    idempotency_key: RoutingKey
    message: RoutingText
    explicit_recipe_id: str | None = None
    force_slow: bool = Field(default=False, strict=True)
    case_role: Literal["learning", "validation"] = "learning"


class JevRoutingInput(ConnectorModel):
    message: RoutingText
    criteria: dict[str, RoutingText]
    template_version: Literal["routing-v1"] = "routing-v1"

    @model_validator(mode="after")
    def options(self) -> JevRoutingInput:
        if "escalate" not in self.criteria or not 2 <= len(self.criteria) <= 21:
            raise ValueError("Routing requires escalate and 1..20 approved choices")
        return self


class JevRoutingJudgement(ConnectorModel):
    model: str
    choice: str
    confidence: RoutingProbability
    probabilities: dict[str, RoutingProbability]
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    evidence_source: Literal["typesafe", "protocol_trial"]
    template_version: Literal["routing-v1"] = "routing-v1"

    @model_validator(mode="after")
    def distribution(self) -> JevRoutingJudgement:
        if self.choice not in self.probabilities:
            raise ValueError("Selected choice is missing from probabilities")
        if abs(sum(self.probabilities.values()) - 1) > 0.001:
            raise ValueError("Probabilities must sum to one")
        if self.probabilities[self.choice] + 0.000001 < max(self.probabilities.values()):
            raise ValueError("Choice must have the highest probability")
        return self


class RoutingFeedbackRequest(ConnectorModel):
    idempotency_key: RoutingKey
    actor: RoutingKey
    outcome: Literal["correct", "misroute", "execution_failed"]
    explanation: RoutingText


class RoutingFeedback(ConnectorModel):
    actor: str
    outcome: Literal["correct", "misroute", "execution_failed"]
    explanation: str
    recorded_at: AwareDatetime


class ResolveRoutingRequest(ConnectorModel):
    idempotency_key: RoutingKey
    actor: RoutingKey
    response: RoutingText
    evidence: RoutingText


class RoutingSlowAnswer(ConnectorModel):
    response: RoutingText
    evidence: RoutingText
    needs_human: bool = Field(strict=True)
    candidate: RoutingRecipeInput | None
    change_reason: RoutingText | None
    project_change: RoutingText | None = None

    @model_validator(mode="after")
    def candidate_reason(self) -> RoutingSlowAnswer:
        if (self.candidate is None) != (self.change_reason is None):
            raise ValueError("Candidate and change_reason must both be null or both be provided")
        if self.needs_human and self.candidate is not None:
            raise ValueError("Resolve the current request before proposing a reusable candidate")
        if self.project_change is not None and (self.candidate is not None or not self.needs_human):
            raise ValueError("Project changes require needs_human=true and no routing candidate")
        return self


class RoutingSlowSubmission(ConnectorModel):
    """Flat, required/nullable fields supported by Pi's strict tool schema contract."""

    response: RoutingText
    evidence: RoutingText
    needs_human: bool = Field(
        strict=True,
        description=(
            "True only when the CURRENT request cannot be answered/completed with the available "
            "read-only facts. Listing pending human decisions answers a query: those decisions "
            "remaining pending do not themselves require this flag. If true all candidate fields "
            "and change_reason must be null."
        ),
    )
    candidate_name: RoutingKey | None
    candidate_applicability: RoutingText | None
    candidate_target: RoutingTarget | None
    change_reason: RoutingText | None
    project_change: RoutingText | None = Field(
        description=(
            "Concrete code or workflow-branch change to request in the bound target Project; "
            "null means no engineering task. Requires needs_human=true and all candidate fields "
            "null. The target's normal plan approval and execution authorization remain required."
        )
    )

    def answer(self) -> RoutingSlowAnswer:
        candidate = None
        if any(
            v is not None
            for v in (self.candidate_name, self.candidate_applicability, self.candidate_target)
        ):
            if (
                self.candidate_name is None
                or self.candidate_applicability is None
                or self.candidate_target is None
            ):
                raise ValueError("Provide every candidate field, or set all of them to null")
            candidate = RoutingRecipeInput(
                name=self.candidate_name,
                applicability=self.candidate_applicability,
                target=self.candidate_target,
            )
        return RoutingSlowAnswer(
            response=self.response,
            evidence=self.evidence,
            needs_human=self.needs_human,
            candidate=candidate,
            change_reason=self.change_reason,
            project_change=self.project_change,
        )


class RoutingReplayCase(ConnectorModel):
    request_id: str
    expected_choice: str


class RoutingFallbackExecution(ConnectorModel):
    attempt_id: str
    session_id: str
    model: str
    configuration_hash: str
    status: Literal["running", "completed", "failed", "interrupted"]
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    error: str | None = None
    candidate_id: str | None = None
    change_reason: str | None = None
    replay_id: str | None = None
    replay_error: str | None = None
    replay_cases: list[RoutingReplayCase] | None = None
    project_change: RoutingProjectChange | None = None
    change_project_binding: RoutingProjectBinding | None = None


class RoutingProjectChange(ConnectorModel):
    target: RoutingProjectBinding
    objective: str
    status: Literal[
        "pending", "dispatching", "waiting_approval", "needs_input", "failed", "unknown"
    ]
    goal_id: str | None = None
    conversation_id: str | None = None
    plan_revision_id: str | None = None
    error: str | None = None


class RoutingRequest(ConnectorModel):
    request_id: str
    lab_id: str
    project_id: str
    message: str
    case_role: Literal["learning", "validation"]
    explicit_recipe_id: str | None
    force_slow: bool
    catalog_version: int
    catalog_snapshot: list[RoutingRecipe]
    status: Literal["queued", "routing", "escalated", "completed", "shadow"]
    route_source: Literal["none", "rule", "jev", "system2"]
    reason: str | None
    connector_call_id: str | None
    judgement: JevRoutingJudgement | None
    selected_recipe_id: str | None
    result: dict[str, JsonValue] | None
    feedback: RoutingFeedback | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    fallback: RoutingFallbackExecution | None = None


class ProposeRoutingRecipeRequest(ConnectorModel):
    idempotency_key: RoutingKey
    recipe: RoutingRecipeInput
    source_request_ids: list[str] = Field(min_length=1, max_length=20)


class StartRoutingReplayRequest(ConnectorModel):
    idempotency_key: RoutingKey
    candidate_id: str
    cases: list[RoutingReplayCase] = Field(min_length=2, max_length=20)


class RoutingReplayEntry(ConnectorModel):
    request_id: str
    message: str
    case_role: Literal["learning", "validation"]
    expected_choice: str
    connector_call_id: str | None
    judgement: JevRoutingJudgement | None
    passed: bool | None
    reason: str | None


class RoutingReplay(ConnectorModel):
    replay_id: str
    lab_id: str
    project_id: str
    candidate_id: str
    catalog_version: int
    catalog_snapshot: list[RoutingRecipe]
    status: Literal["pending", "passed", "failed"]
    entries: list[RoutingReplayEntry]
    created_at: AwareDatetime


class PublishRoutingRecipeRequest(ConnectorModel):
    idempotency_key: RoutingKey
    actor: RoutingKey
    replay_id: str
    allow_protocol_trial: bool = Field(default=False, strict=True)


class PauseRoutingRecipeRequest(ConnectorModel):
    idempotency_key: RoutingKey
    actor: RoutingKey
    reason: RoutingText


class RoutingLabView(ConnectorModel):
    lab: RoutingLab
    recipes: list[RoutingRecipe]


class RoutingLabMetrics(ConnectorModel):
    request_count: int
    routing_count: int
    escalated_count: int
    system2_resolved_count: int
    fast_completed_count: int
    shadow_count: int
    reviewed_count: int
    misroute_count: int
    provider_judgements: int
    protocol_trial_judgements: int
    by_recipe: dict[str, dict[str, int]]
