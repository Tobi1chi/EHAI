"""Immutable Artifact metadata for execution outputs and evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

from ehai import ID, JsonValue, format_utc_datetime, normalize_id, parse_utc_datetime

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


class ArtifactKind(StrEnum):
    """Artifact roles required by the P1 execution loop."""

    CANDIDATE = "candidate"
    EVIDENCE = "evidence"
    LOG = "log"
    PATCH = "patch"
    WORKER_OUTPUT = "worker_output"
    CHECK_OUTPUT = "check_output"


@dataclass(frozen=True, slots=True)
class Artifact:
    """Immutable metadata for bytes stored outside the execution database.

    Ownership is hierarchical: a node belongs to a Run, and an Attempt belongs
    to both a Run and a PlanNode. Project- and planning-stage Artifacts may have
    no execution owner at all.
    """

    artifact_id: ID
    kind: ArtifactKind
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    relative_path: str
    created_at: datetime
    run_id: ID | None = None
    plan_node_id: ID | None = None
    attempt_id: ID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _validated_id(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "kind", ArtifactKind(self.kind))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))

        if not self.name.strip() or "\x00" in self.name:
            raise ValueError(f"artifact {self.artifact_id}: name must not be blank or contain NUL")
        if (
            not self.media_type.strip()
            or "/" not in self.media_type
            or any(character in self.media_type for character in "\r\n\x00")
        ):
            raise ValueError(f"artifact {self.artifact_id}: invalid media_type")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError(f"artifact {self.artifact_id}: size_bytes must be non-negative")

        canonical_hash = self.sha256.lower()
        if _SHA256_PATTERN.fullmatch(canonical_hash) is None:
            raise ValueError(f"artifact {self.artifact_id}: sha256 must be 64 hexadecimal digits")
        object.__setattr__(self, "sha256", canonical_hash)
        object.__setattr__(
            self,
            "relative_path",
            _validated_relative_path(self.relative_path, artifact_id=self.artifact_id),
        )

        run_id = _validated_optional_id(self.run_id, "run_id")
        plan_node_id = _validated_optional_id(self.plan_node_id, "plan_node_id")
        attempt_id = _validated_optional_id(self.attempt_id, "attempt_id")
        if plan_node_id is not None and run_id is None:
            raise ValueError(f"artifact {self.artifact_id}: plan_node_id requires run_id")
        if attempt_id is not None and (run_id is None or plan_node_id is None):
            raise ValueError(
                f"artifact {self.artifact_id}: attempt_id requires run_id and plan_node_id"
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "plan_node_id", plan_node_id)
        object.__setattr__(self, "attempt_id", attempt_id)

    def to_dict(self) -> dict[str, JsonValue]:
        """Return deterministic JSON-compatible metadata without Artifact bytes."""
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind.value,
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "relative_path": self.relative_path,
            "created_at": format_utc_datetime(self.created_at),
            "run_id": self.run_id,
            "plan_node_id": self.plan_node_id,
            "attempt_id": self.attempt_id,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, object]) -> Self:
        """Strictly validate metadata loaded from a persistence boundary."""
        expected_keys = {
            "artifact_id",
            "kind",
            "name",
            "media_type",
            "size_bytes",
            "sha256",
            "relative_path",
            "created_at",
            "run_id",
            "plan_node_id",
            "attempt_id",
        }
        if not all(isinstance(key, str) for key in document):
            raise ValueError("artifact metadata keys must be strings")
        actual_keys = set(document)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise ValueError(f"invalid Artifact metadata keys; missing={missing}, extra={extra}")

        artifact_id = _required_string(document["artifact_id"], "artifact_id")
        kind = _required_string(document["kind"], "kind")
        name = _required_string(document["name"], "name")
        media_type = _required_string(document["media_type"], "media_type")
        sha256 = _required_string(document["sha256"], "sha256")
        relative_path = _required_string(document["relative_path"], "relative_path")
        created_at = _required_string(document["created_at"], "created_at")
        size_bytes = document["size_bytes"]
        if type(size_bytes) is not int:
            raise ValueError("Artifact size_bytes must be an integer")

        run_id = _optional_string(document["run_id"], "run_id")
        plan_node_id = _optional_string(document["plan_node_id"], "plan_node_id")
        attempt_id = _optional_string(document["attempt_id"], "attempt_id")
        return cls(
            artifact_id=normalize_id(artifact_id),
            kind=ArtifactKind(kind),
            name=name,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256,
            relative_path=relative_path,
            created_at=parse_utc_datetime(created_at),
            run_id=None if run_id is None else normalize_id(run_id),
            plan_node_id=None if plan_node_id is None else normalize_id(plan_node_id),
            attempt_id=None if attempt_id is None else normalize_id(attempt_id),
        )


def _validated_id(value: ID, field_name: str) -> ID:
    try:
        return normalize_id(value)
    except ValueError as error:
        raise ValueError(f"Artifact has invalid {field_name}: {error}") from error


def _validated_optional_id(value: ID | None, field_name: str) -> ID | None:
    return None if value is None else _validated_id(value, field_name)


def _validated_relative_path(value: str, *, artifact_id: ID) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"artifact {artifact_id}: relative_path is unsafe")
    raw_parts = value.split("/")
    if any(
        not part or part in {".", ".."} or _SAFE_PATH_SEGMENT.fullmatch(part) is None
        for part in raw_parts
    ):
        raise ValueError(f"artifact {artifact_id}: relative_path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError(f"artifact {artifact_id}: relative_path is unsafe")
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Artifact {field_name} must be a string")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Artifact {field_name} must be a string or null")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Artifact {field_name} must be timezone-aware")
    return value.astimezone(UTC)
