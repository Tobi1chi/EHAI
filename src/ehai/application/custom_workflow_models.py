"""Contracts for project-defined workflows: a finite, acyclic, single-path step graph.

A definition is saved as an immutable revision. Runs pin the revision they started from;
saving a new revision never changes a started Run or a Routine bound to an older one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from ehai import JsonValue
from ehai.application.workflow_models import LifeText, WorkflowKey, WorkflowModel

StepId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
# ``input.<field>[.<key>...]`` or ``steps.<step id>[.<key>...]``; numeric keys index lists.
REF_PATTERN = r"^(input|steps\.[a-z][a-z0-9_]{0,63})(\.[A-Za-z0-9_-]{1,100}){0,20}$"
RefPath = Annotated[str, StringConstraints(pattern=REF_PATTERN)]
# Names under ``life.`` belong to the fixed workflows and are refused when saving.
DefinitionName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]


class RefValue(WorkflowModel):
    """The value at a path in the Run inputs or in an earlier step's output."""

    ref: RefPath


class LiteralValue(WorkflowModel):
    value: JsonValue


class TemplateValue(WorkflowModel):
    """Text with ``{path}`` placeholders, each a reference rendered as text."""

    template: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


Value = RefValue | LiteralValue | TemplateValue


class CompareCondition(WorkflowModel):
    """Equality works on any JSON; ordering needs two numbers or two strings."""

    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    left: Value
    right: Value


class ExistsCondition(WorkflowModel):
    """True when the path resolves to a value other than null."""

    op: Literal["exists"]
    ref: RefPath


class AllCondition(WorkflowModel):
    op: Literal["all"]
    conditions: list[Condition] = Field(min_length=1, max_length=20)


class AnyCondition(WorkflowModel):
    op: Literal["any"]
    conditions: list[Condition] = Field(min_length=1, max_length=20)


class NotCondition(WorkflowModel):
    op: Literal["not"]
    condition: Condition


Condition = Annotated[
    CompareCondition | ExistsCondition | AllCondition | AnyCondition | NotCondition,
    Field(discriminator="op"),
]


class ActionStep(WorkflowModel):
    """Call a registered connector action and continue with its structured output.

    A write action needs exactly one authorization: ``approval_step`` names a human step
    on every path to this one whose approval leads here, or ``standing_write`` records the
    definer's standing permission. Read-only actions take neither. A failed or unknown
    result never continues on a business branch.
    """

    type: Literal["action"]
    id: StepId
    connector_id: str
    action: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")]
    action_version: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    inputs: dict[str, Value] = Field(default_factory=dict, max_length=100)
    approval_step: StepId | None = None
    standing_write: bool = Field(default=False, strict=True)
    next: StepId


class AgentTool(WorkflowModel):
    connector_id: str
    action: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")]
    action_version: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class AgentStep(WorkflowModel):
    """Delegate one bounded, read-only query task outside a Git workspace."""

    type: Literal["agent"]
    id: StepId
    instruction: Value
    tools: list[AgentTool] = Field(min_length=1, max_length=10)
    output_schema: dict[str, JsonValue]
    max_tool_calls: int = Field(default=30, ge=1, le=100, strict=True)
    timeout_seconds: int = Field(default=900, ge=60, le=3600, strict=True)
    next: StepId

    @model_validator(mode="after")
    def unique_tools(self) -> AgentStep:
        keys = [(tool.connector_id, tool.action) for tool in self.tools]
        if len(keys) != len(set(keys)):
            raise ValueError("Agent tools must be unique by connector_id and action")
        if isinstance(self.instruction, LiteralValue) and not isinstance(
            self.instruction.value, str
        ):
            raise ValueError("Agent instruction literal must be a string")
        return self


class ConditionCase(WorkflowModel):
    when: Condition
    next: StepId


class ConditionStep(WorkflowModel):
    """Take the first matching case, otherwise ``default``; the choice is recorded once."""

    type: Literal["condition"]
    id: StepId
    cases: list[ConditionCase] = Field(min_length=1, max_length=20)
    default: StepId


class GrantLimit(WorkflowModel):
    kind: Literal["amount", "count"]
    value: Value
    unit: str | None = None


class HumanGrant(WorkflowModel):
    limit: GrantLimit | None = None
    valid_seconds: int | None = Field(default=None, ge=60, le=31536000, strict=True)


class HumanStep(WorkflowModel):
    """Wait for an approve/reject decision on the subject as resolved when the step starts.

    ``timeout_seconds`` and ``on_timeout`` go together; without them the step waits until
    decided.
    """

    type: Literal["human"]
    id: StepId
    question: Annotated[LifeText, Field(max_length=500)]
    subject: dict[str, Value] = Field(default_factory=dict, max_length=50)
    grant: HumanGrant | None = None
    on_approve: StepId
    on_reject: StepId
    timeout_seconds: int | None = Field(default=None, ge=60, le=31536000, strict=True)
    on_timeout: StepId | None = None

    @model_validator(mode="after")
    def timeout_pair(self) -> HumanStep:
        if (self.timeout_seconds is None) != (self.on_timeout is None):
            raise ValueError("timeout_seconds and on_timeout must be given together")
        return self


class WaitStep(WorkflowModel):
    """Wait until a time with an offset; ``until`` must resolve to an ISO time with offset."""

    type: Literal["wait"]
    id: StepId
    until: Value
    offset_seconds: int = Field(default=0, ge=-31536000, le=31536000, strict=True)
    next: StepId


class EndStep(WorkflowModel):
    """Finish with a business outcome, such as ``booked`` or ``no_suitable_train``."""

    type: Literal["end"]
    id: StepId
    outcome: StepId
    result: dict[str, Value] = Field(default_factory=dict, max_length=50)


Step = Annotated[
    ActionStep | AgentStep | ConditionStep | HumanStep | WaitStep | EndStep,
    Field(discriminator="type"),
]


class WorkflowDefinitionBody(WorkflowModel):
    name: DefinitionName
    description: Annotated[LifeText, Field(max_length=2000)]
    inputs_schema: dict[str, JsonValue]
    start: StepId
    steps: list[Step] = Field(min_length=1, max_length=100)


class SaveWorkflowDefinitionRequest(WorkflowDefinitionBody):
    """Save the next immutable revision of ``name`` in this project.

    ``expected_revision`` is null for a new name, otherwise the latest saved revision.
    """

    idempotency_key: WorkflowKey
    expected_revision: int | None = Field(ge=1, strict=True)


class WorkflowDefinitionRevision(WorkflowModel):
    definition_id: str
    project_id: str
    name: str
    revision: int
    digest: str
    definition: WorkflowDefinitionBody
    created_at: datetime


class StartCustomWorkflowRequest(WorkflowModel):
    idempotency_key: WorkflowKey
    inputs: dict[str, JsonValue]


for _model in (AllCondition, AnyCondition, NotCondition, ConditionCase, ConditionStep):
    _model.model_rebuild()
