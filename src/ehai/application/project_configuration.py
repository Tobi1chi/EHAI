"""Versioned project rules bound to one explicitly configured execution host."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from ehai import ID, JsonValue, format_utc_datetime, json_dumps, normalize_id, utc_now
from ehai.application.ports import EventReader
from ehai.domain.events import EventType


class ProjectConfigurationError(ValueError):
    """A project configuration is invalid for the selected host."""


class ProjectConfigurationConflict(ProjectConfigurationError):
    """A write uses an old version or a different idempotent request."""


class ProjectConfigurationNotFound(ProjectConfigurationError):
    """The requested project or run does not exist."""


class ProjectConfigurationStore(Protocol):
    """Durable versions and immutable execution facts."""

    def versions(self, project_id: ID) -> list[dict[str, JsonValue]]: ...

    def put(
        self,
        project_id: ID,
        expected_version: int,
        idempotency_key: str,
        request: dict[str, JsonValue],
        snapshot: dict[str, JsonValue],
    ) -> dict[str, JsonValue]: ...

    def run_snapshot(self, run_id: ID) -> dict[str, JsonValue] | None: ...


def _fingerprint(document: dict[str, JsonValue]) -> str:
    return hashlib.sha256(json_dumps(document).encode()).hexdigest()


def run_configuration(events: EventReader, run_id: ID) -> dict[str, JsonValue] | None:
    """Return only the original execution snapshot; never consult latest project rules."""
    for stored in events.list_events():
        event = stored.event
        if event.run_id == run_id and event.type in {
            EventType.RUN_STARTED,
            EventType.RUN_CONFIGURATION_CAPTURED,
        }:
            snapshot = event.payload.get("project_configuration")
            if isinstance(snapshot, dict):
                return snapshot
    return None


class ProjectConfigurationService:
    """Apply static rules without changing host permissions or private model settings."""

    def __init__(
        self,
        store: ProjectConfigurationStore,
        execution_config: dict[str, JsonValue] | None = None,
        *,
        workspace: Path | str | None = None,
        role_configuration: dict[str, JsonValue] | None = None,
    ) -> None:
        self._store = store
        self._execution_config = execution_config
        self._workspace = None if workspace is None else str(Path(workspace).resolve(strict=True))
        self._role_configuration = role_configuration

    def host(self) -> dict[str, JsonValue]:
        """Describe reusable host role settings using no Pi paths or secret contents."""
        config = self._execution_config
        if config is None and (self._workspace is None or self._role_configuration is None):
            raise ProjectConfigurationError(
                "An execution host workspace and role configuration are required"
            )
        role = (
            self._role_configuration
            if config is None
            else {key: config.get(key) for key in ("worker_kind", "model", "reasoning_effort")}
        )
        assert role is not None
        role = {key: role.get(key) for key in ("worker_kind", "model", "reasoning_effort")}
        return {
            "workspace": self._workspace if config is None else config.get("workspace"),
            "execution_config_fingerprint": None if config is None else _fingerprint(config),
            "role_configuration_ref": "sha256:" + _fingerprint(role),
            "role_configuration": role,
        }

    def versions(self, project_id: ID) -> dict[str, JsonValue]:
        versions = self._store.versions(normalize_id(project_id))
        return {"project_id": normalize_id(project_id), "versions": list(versions)}

    def get(self, project_id: ID) -> dict[str, JsonValue]:
        versions = self._store.versions(normalize_id(project_id))
        return {
            "project_id": normalize_id(project_id),
            "configuration": versions[-1] if versions else None,
        }

    def update(
        self,
        project_id: ID,
        *,
        expected_version: int,
        idempotency_key: str,
        workspace: str,
        execution_config_fingerprint: str | None,
        role_configuration_ref: str,
        static_rules: list[str],
    ) -> dict[str, JsonValue]:
        """Replace all rules; expected_version=0 creates the initial immutable version."""
        project_id = normalize_id(project_id)
        if type(expected_version) is not int or expected_version < 0:
            raise ProjectConfigurationError("expected_version must be a nonnegative integer")
        if not idempotency_key.strip():
            raise ProjectConfigurationError("idempotency_key must be nonblank")
        if len(static_rules) > 100 or any(
            not isinstance(rule, str) or not rule.strip() or len(rule) > 8000
            for rule in static_rules
        ):
            raise ProjectConfigurationError(
                "static_rules requires at most 100 nonblank rules of 8000 characters"
            )
        try:
            resolved_workspace = str(Path(workspace).expanduser().resolve(strict=True))
        except OSError as error:
            raise ProjectConfigurationError(
                "workspace must be an existing host directory"
            ) from error
        binding: dict[str, JsonValue] = {
            "workspace": resolved_workspace,
            "execution_config_fingerprint": execution_config_fingerprint,
            "role_configuration_ref": role_configuration_ref,
        }
        self._validate_binding(binding)
        request: dict[str, JsonValue] = {
            "project_id": project_id,
            "expected_version": expected_version,
            **binding,
            "static_rules": list(static_rules),
        }
        snapshot: dict[str, JsonValue] = {
            "project_id": project_id,
            "version": expected_version + 1,
            **binding,
            "static_rules": list(static_rules),
            "created_at": format_utc_datetime(utc_now()),
        }
        return self._store.put(project_id, expected_version, idempotency_key, request, snapshot)

    def _validate_binding(self, binding: dict[str, JsonValue]) -> None:
        host = self.host()
        for field in ("workspace", "execution_config_fingerprint", "role_configuration_ref"):
            if binding.get(field) != host[field]:
                raise ProjectConfigurationConflict(
                    f"Project {field} does not match the selected execution host"
                )

    def resolve_for_run(
        self,
        project_id: ID,
        execution_config: dict[str, JsonValue] | None,
    ) -> dict[str, JsonValue]:
        """Capture latest rules under the caller's StartRun write lock."""
        versions = self._store.versions(project_id)
        configuration = versions[-1] if versions else None
        if configuration is not None:
            self._validate_binding(configuration)
            # The runtime validates a per-Run process policy against its Planner separately.
            # Match the same stable host binding here; RUN_STARTED retains the exact
            # authorized document and fingerprint, including that process policy.
            host_config = None if execution_config is None else dict(execution_config)
            if host_config is not None and self._execution_config is not None:
                if "process_adjustment" in self._execution_config:
                    host_config["process_adjustment"] = self._execution_config["process_adjustment"]
                else:
                    host_config.pop("process_adjustment", None)
            fingerprint = None if host_config is None else _fingerprint(host_config)
            if fingerprint != configuration.get("execution_config_fingerprint"):
                raise ProjectConfigurationConflict(
                    "Project configuration requires matching explicit execution authorization"
                )
            return configuration
        return {
            "project_id": project_id,
            "version": 0,
            "static_rules": [],
            "workspace": self._workspace
            if execution_config is None
            else execution_config.get("workspace"),
            "execution_config_fingerprint": None
            if execution_config is None
            else _fingerprint(execution_config),
            "role_configuration_ref": None,
            "created_at": format_utc_datetime(utc_now()),
        }

    def planner_context(self, project_id: ID, run_id: ID | None = None) -> dict[str, JsonValue]:
        """Planning uses current rules; process work uses the original Run snapshot."""
        if run_id is not None:
            snapshot = self._store.run_snapshot(run_id)
            if snapshot is not None and snapshot.get("project_id") != project_id:
                raise ProjectConfigurationConflict(
                    "Run configuration belongs to a different project"
                )
            return {} if snapshot is None else snapshot
        versions = self._store.versions(project_id)
        if not versions:
            return {}
        self._validate_binding(versions[-1])
        return versions[-1]

    def get_run(self, run_id: ID) -> dict[str, JsonValue]:
        """Legacy Runs return null; no historical configuration is invented."""
        return {
            "run_id": normalize_id(run_id),
            "configuration": self._store.run_snapshot(normalize_id(run_id)),
        }
