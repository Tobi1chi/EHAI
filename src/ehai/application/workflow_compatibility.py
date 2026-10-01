"""Explicit contract adaptation hooks. Production keeps this module disabled.

No discovery, latest-version fallback, persisted state migration or implicit chaining.
An adapter transforms a detached document; applying it to a Run is not implemented.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ehai import JsonValue, json_dumps, json_loads
from ehai.application.ports import StateConflictError
from ehai.application.workflow_execution_models import (
    WorkflowCompatibilityStatus,
    WorkflowContractRef,
)


@dataclass(frozen=True, slots=True)
class WorkflowContractAdapter:
    source: WorkflowContractRef
    target: WorkflowContractRef
    transform: Callable[[dict[str, JsonValue]], dict[str, JsonValue]]
    validate_target: Callable[[dict[str, JsonValue]], None]


class WorkflowCompatibility:
    def __init__(
        self,
        *,
        enabled: bool = False,
        adapters: tuple[WorkflowContractAdapter, ...] = (),
    ) -> None:
        self._enabled = enabled
        self._adapters: dict[
            tuple[WorkflowContractRef, WorkflowContractRef], WorkflowContractAdapter
        ] = {}
        for adapter in adapters:
            if (
                adapter.source.kind != adapter.target.kind
                or adapter.source.name != adapter.target.name
            ):
                raise ValueError("An adapter must preserve the contract's identity")
            if adapter.source == adapter.target:
                raise ValueError("An adapter must describe an explicit contract change")
            if adapter.source.digest is None or adapter.target.digest is None:
                raise ValueError("Adapters require exact source and target contract digests")
            key = (adapter.source, adapter.target)
            if key in self._adapters:
                raise ValueError("Duplicate workflow contract adapter")
            self._adapters[key] = adapter

    def status(self) -> WorkflowCompatibilityStatus:
        return WorkflowCompatibilityStatus(
            enabled=self._enabled,
            registered_adapters=len(self._adapters),
        )

    def inspect(
        self,
        source: WorkflowContractRef,
        target: WorkflowContractRef,
    ) -> Literal["disabled", "exact", "adapter_available", "incompatible"]:
        if not self._enabled:
            return "disabled"
        if source == target and source.digest is not None:
            return "exact"
        return "adapter_available" if (source, target) in self._adapters else "incompatible"

    def adapt(
        self,
        source: WorkflowContractRef,
        target: WorkflowContractRef,
        document: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        outcome = self.inspect(source, target)
        if outcome == "disabled":
            raise StateConflictError("Workflow version compatibility is disabled")
        if outcome == "incompatible":
            raise StateConflictError("No adapter for these exact workflow contracts")
        value = _copy(document)
        if outcome == "exact":
            return value
        adapter = self._adapters[(source, target)]
        result = _copy(adapter.transform(value))
        adapter.validate_target(_copy(result))
        return result


def _copy(document: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = json_loads(json_dumps(document))
    if not isinstance(value, dict):
        raise ValueError("Contract adapters must return JSON objects")
    return value
