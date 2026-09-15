"""Explicit authorization for advisory-only, periodic Worker trajectory review."""

from collections.abc import Mapping
from dataclasses import dataclass

from ehai import JsonValue


@dataclass(frozen=True, slots=True)
class TrajectoryReviewPolicy:
    interval_seconds: int = 600
    step_count: int = 30
    model: str = "gpt-5.6-luna"

    def __post_init__(self) -> None:
        for name in ("interval_seconds", "step_count"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"trajectory_review.{name} must be a positive integer")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("trajectory_review.model must be non-empty text")
        object.__setattr__(self, "model", self.model.strip())

    @classmethod
    def from_document(cls, value: Mapping[str, JsonValue]) -> "TrajectoryReviewPolicy":
        if set(value) - {"interval_seconds", "step_count", "model"}:
            raise ValueError("Unknown trajectory_review configuration field")
        interval, steps, model = (
            value.get("interval_seconds", 600),
            value.get("step_count", 30),
            value.get("model", "gpt-5.6-luna"),
        )
        if type(interval) is not int or type(steps) is not int or not isinstance(model, str):
            raise ValueError("Invalid trajectory_review configuration")
        return cls(interval, steps, model)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "interval_seconds": self.interval_seconds,
            "step_count": self.step_count,
            "model": self.model,
        }
