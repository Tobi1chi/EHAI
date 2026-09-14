"""Immutable host-validated facts for adopting results across Runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from ehai import (
    ID,
    JsonValue,
    format_utc_datetime,
    normalize_id,
    parse_utc_datetime,
)
from ehai.domain.checking import HumanCheckEvidence

_MAX_REASON_BYTES = 16_000
_EVIDENCE_FIELDS = frozenset({"artifact_id", "sha256"})
_ADOPTION_FIELDS = frozenset(
    {
        "adoption_id",
        "target_run_id",
        "target_plan_revision_id",
        "target_plan_node_id",
        "source_run_id",
        "source_plan_revision_id",
        "source_process_revision_id",
        "source_plan_node_id",
        "source_attempt_id",
        "evidence",
        "reason",
        "created_at",
    }
)


@dataclass(frozen=True, slots=True)
class ResultAdoption:
    """A host-validated, immutable result handoff between two Runs.

    This record describes provenance only.  It is not an Attempt, does not
    copy a Gate decision, and deliberately has no persistence or repository
    responsibilities.
    """

    adoption_id: ID
    target_run_id: ID
    target_plan_revision_id: ID
    target_plan_node_id: ID
    source_run_id: ID
    source_plan_revision_id: ID
    source_process_revision_id: ID
    source_plan_node_id: ID
    source_attempt_id: ID
    evidence: tuple[HumanCheckEvidence, ...]
    reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "adoption_id",
            "target_run_id",
            "target_plan_revision_id",
            "target_plan_node_id",
            "source_run_id",
            "source_plan_revision_id",
            "source_process_revision_id",
            "source_plan_node_id",
            "source_attempt_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_id(getattr(self, field_name), field_name),
            )

        if self.source_run_id == self.target_run_id:
            raise ValueError("ResultAdoption source_run_id and target_run_id must differ")

        evidence = tuple(self.evidence)
        if not evidence:
            raise ValueError("ResultAdoption evidence must not be empty")
        if any(not isinstance(item, HumanCheckEvidence) for item in evidence):
            raise TypeError("ResultAdoption evidence must contain HumanCheckEvidence values")
        if len({item.artifact_id for item in evidence}) != len(evidence):
            raise ValueError("ResultAdoption evidence must not contain duplicate artifact IDs")
        object.__setattr__(self, "evidence", evidence)

        reason = _reason(self.reason)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "created_at", _utc(self.created_at))

    def to_dict(self) -> dict[str, JsonValue]:
        """Return the canonical JSON-compatible adoption document."""
        return {
            "adoption_id": self.adoption_id,
            "target_run_id": self.target_run_id,
            "target_plan_revision_id": self.target_plan_revision_id,
            "target_plan_node_id": self.target_plan_node_id,
            "source_run_id": self.source_run_id,
            "source_plan_revision_id": self.source_plan_revision_id,
            "source_process_revision_id": self.source_process_revision_id,
            "source_plan_node_id": self.source_plan_node_id,
            "source_attempt_id": self.source_attempt_id,
            "evidence": [
                {"artifact_id": item.artifact_id, "sha256": item.sha256} for item in self.evidence
            ],
            "reason": self.reason,
            "created_at": format_utc_datetime(self.created_at),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, JsonValue]) -> Self:
        """Validate and reconstruct an adoption from a JSON object."""
        if not isinstance(value, Mapping):
            raise ValueError("ResultAdoption must be a JSON object")
        actual_fields = set(value)
        if not all(isinstance(field_name, str) for field_name in actual_fields):
            raise ValueError("ResultAdoption field names must be strings")
        if actual_fields != _ADOPTION_FIELDS:
            missing = sorted(_ADOPTION_FIELDS - actual_fields)
            extra = sorted(actual_fields - _ADOPTION_FIELDS)
            raise ValueError(f"invalid ResultAdoption fields; missing={missing}, extra={extra}")

        id_values: dict[str, ID] = {}
        for field_name in (
            "adoption_id",
            "target_run_id",
            "target_plan_revision_id",
            "target_plan_node_id",
            "source_run_id",
            "source_plan_revision_id",
            "source_process_revision_id",
            "source_plan_node_id",
            "source_attempt_id",
        ):
            raw_value = value[field_name]
            if not isinstance(raw_value, str):
                raise ValueError(f"ResultAdoption {field_name} must be a UUID string")
            id_values[field_name] = _normalize_id(raw_value, field_name)

        raw_evidence = value["evidence"]
        if not isinstance(raw_evidence, list):
            raise ValueError("ResultAdoption evidence must be a JSON array")
        evidence = tuple(
            _evidence_from_mapping(item, index=index) for index, item in enumerate(raw_evidence)
        )

        reason = value["reason"]
        if not isinstance(reason, str):
            raise ValueError("ResultAdoption reason must be text")
        created_at = value["created_at"]
        if not isinstance(created_at, str):
            raise ValueError("ResultAdoption created_at must be an RFC 3339 string")

        return cls(
            adoption_id=id_values["adoption_id"],
            target_run_id=id_values["target_run_id"],
            target_plan_revision_id=id_values["target_plan_revision_id"],
            target_plan_node_id=id_values["target_plan_node_id"],
            source_run_id=id_values["source_run_id"],
            source_plan_revision_id=id_values["source_plan_revision_id"],
            source_process_revision_id=id_values["source_process_revision_id"],
            source_plan_node_id=id_values["source_plan_node_id"],
            source_attempt_id=id_values["source_attempt_id"],
            evidence=evidence,
            reason=reason,
            created_at=parse_utc_datetime(created_at),
        )


def _evidence_from_mapping(value: object, *, index: int) -> HumanCheckEvidence:
    if not isinstance(value, Mapping):
        raise ValueError(f"ResultAdoption evidence[{index}] must be a JSON object")
    actual_fields = set(value)
    if not all(isinstance(field_name, str) for field_name in actual_fields):
        raise ValueError(f"ResultAdoption evidence[{index}] field names must be strings")
    if actual_fields != _EVIDENCE_FIELDS:
        missing = sorted(_EVIDENCE_FIELDS - actual_fields)
        extra = sorted(actual_fields - _EVIDENCE_FIELDS)
        raise ValueError(
            f"invalid ResultAdoption evidence[{index}] fields; missing={missing}, extra={extra}"
        )
    artifact_id = value["artifact_id"]
    digest = value["sha256"]
    if not isinstance(artifact_id, str):
        raise ValueError(f"ResultAdoption evidence[{index}].artifact_id must be a UUID string")
    if not isinstance(digest, str):
        raise ValueError(f"ResultAdoption evidence[{index}].sha256 must be text")
    try:
        return HumanCheckEvidence(
            artifact_id=_normalize_id(artifact_id, f"evidence[{index}].artifact_id"),
            sha256=digest,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid ResultAdoption evidence[{index}]") from error


def _normalize_id(value: str, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"ResultAdoption {field_name} must be a valid UUID") from error


def _reason(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("ResultAdoption reason must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError("ResultAdoption reason must not be empty")
    if len(normalized.encode("utf-8")) > _MAX_REASON_BYTES:
        raise ValueError(f"ResultAdoption reason exceeds {_MAX_REASON_BYTES} UTF-8 bytes")
    return normalized


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ResultAdoption created_at must be timezone-aware")
    return value.astimezone(UTC)
