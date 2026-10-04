"""Bounded, integrity-checked reads of roster-listed Artifacts for planning roles."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from hashlib import sha256
from typing import Protocol

from ehai import ID, JsonValue, normalize_id
from ehai.application.agent_contracts import RecoverableToolError
from ehai.application.ports import ArtifactStore
from ehai.infrastructure.artifacts import ArtifactIntegrityError

MAX_ARTIFACT_PAGE_BYTES = 64 * 1024


class RosterArtifact(Protocol):
    """Retained metadata the host verifies before returning Artifact bytes."""

    @property
    def artifact_id(self) -> ID: ...
    @property
    def name(self) -> str: ...
    @property
    def media_type(self) -> str: ...
    @property
    def size_bytes(self) -> int: ...
    @property
    def sha256(self) -> str: ...


def artifact_page_parameters() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ARTIFACT_PAGE_BYTES},
        },
        "required": ["artifact_id", "offset", "limit"],
        "additionalProperties": False,
    }


def read_artifact_page(
    arguments: Mapping[str, JsonValue],
    roster: Mapping[ID, RosterArtifact],
    artifact_store: ArtifactStore,
    *,
    missing_code: str,
    missing_message: str,
) -> dict[str, JsonValue]:
    """Return one page of a roster Artifact after checking its retained size and SHA-256."""
    artifact_id = _tool_artifact_id(arguments)
    artifact = roster.get(artifact_id)
    if artifact is None:
        raise RecoverableToolError(missing_code, missing_message)
    offset = _tool_integer(arguments, "offset", minimum=0)
    limit = _tool_integer(arguments, "limit", minimum=1, maximum=MAX_ARTIFACT_PAGE_BYTES)
    try:
        content = artifact_store.read(artifact_id)
    except ArtifactIntegrityError as error:
        raise RecoverableToolError(
            "artifact_integrity",
            f"Artifact {artifact_id} failed storage integrity validation; "
            "do not cite it as reviewed evidence and assess result reuse as uncertain",
        ) from error
    except (FileNotFoundError, OSError) as error:
        raise RecoverableToolError(
            "evidence_unavailable",
            f"Artifact {artifact_id} bytes are unavailable; assess result reuse as uncertain",
        ) from error
    if len(content) != artifact.size_bytes:
        raise RecoverableToolError(
            "artifact_integrity",
            f"Artifact {artifact_id} size does not match retained metadata",
        )
    if sha256(content).hexdigest() != artifact.sha256:
        raise RecoverableToolError(
            "artifact_integrity",
            f"Artifact {artifact_id} SHA-256 does not match retained metadata",
        )
    if offset > len(content):
        raise RecoverableToolError(
            "invalid_offset",
            f"offset must be no greater than the Artifact size ({len(content)})",
        )
    if content and offset == len(content):
        raise RecoverableToolError(
            "invalid_offset",
            "offset at non-empty Artifact EOF would return no evidence bytes",
        )
    end = min(offset + limit, len(content))
    page = content[offset:end]
    encoding, encoded = _encode_page(page)
    has_more = end < len(content)
    return {
        "artifact_id": artifact.artifact_id,
        "name": artifact.name,
        "media_type": artifact.media_type,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "offset": offset,
        "limit": limit,
        "returned_bytes": len(page),
        "next_offset": end if has_more else None,
        "has_more": has_more,
        "complete": offset == 0 and end == len(content),
        "encoding": encoding,
        "content": encoded,
    }


def _tool_artifact_id(arguments: Mapping[str, JsonValue]) -> ID:
    value = arguments.get("artifact_id")
    if not isinstance(value, str):
        raise RecoverableToolError("invalid_artifact_id", "artifact_id must be a valid UUID")
    try:
        return normalize_id(value)
    except ValueError as error:
        raise RecoverableToolError(
            "invalid_artifact_id", "artifact_id must be a valid UUID"
        ) from error


def _tool_integer(
    arguments: Mapping[str, JsonValue],
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = arguments.get(name)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = f" between {minimum} and {maximum}" if maximum is not None else f" >= {minimum}"
        raise RecoverableToolError("invalid_arguments", f"{name} must be an integer{bound}")
    return value


def _encode_page(page: bytes) -> tuple[str, str]:
    try:
        return "utf-8", page.decode("utf-8")
    except UnicodeDecodeError:
        return "base64", base64.b64encode(page).decode("ascii")
