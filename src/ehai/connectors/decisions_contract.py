"""Strict batch classification contracts, independent of provider transport."""

from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from ehai import JsonValue
from ehai.application.connector_models import ConnectorModel

ChoiceValue = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}$")]


class DecisionChoice(ConnectorModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value: ChoiceValue
    description: str = Field(min_length=1, max_length=300)


class ClassifyInput(ConnectorModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    items: list[dict[str, JsonValue]] = Field(min_length=1, max_length=200)
    id_field: str
    text_fields: list[str] = Field(min_length=1, max_length=5)
    question: str = Field(min_length=1, max_length=1000)
    choices: list[DecisionChoice] = Field(min_length=2, max_length=10)
    keep: list[ChoiceValue] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def choice_membership(self) -> "ClassifyInput":
        values = [choice.value for choice in self.choices]
        if len(set(values)) != len(values) or not set(self.keep) <= set(values):
            raise ValueError("Choices must be distinct and keep must name choices")
        return self


class Probability(ConnectorModel):
    value: ChoiceValue
    probability: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)


class DecisionResult(ConnectorModel):
    id: JsonValue
    choice: ChoiceValue
    confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    probabilities: list[Probability]


class DecisionFailure(ConnectorModel):
    id: JsonValue
    reason: str


class DecisionCounts(ConnectorModel):
    total: int
    kept: int
    failed: int


class DecisionUsage(ConnectorModel):
    input_tokens: int = Field(strict=True, ge=0)
    output_tokens: int = Field(strict=True, ge=0)


class ClassifyOutput(ConnectorModel):
    model: str
    results: list[DecisionResult]
    kept: list[dict[str, JsonValue]]
    failed: list[DecisionFailure]
    counts: DecisionCounts
    usage: DecisionUsage
