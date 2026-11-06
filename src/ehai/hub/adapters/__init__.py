"""Compatibility-layer interface between the Hub service and one kind of Agent harness.

A compatibility layer launches an unmodified harness through its public CLI/SDK and
configuration, translates its native output into Hub events, and forwards host responses.
It owns no EHAI business rules and never executes EHAI tools itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from ehai import JsonValue
from ehai.hub.protocol import NativeDecision, ResolvedHarness, StartSession, ToolResult


@dataclass(frozen=True, slots=True)
class McpEndpoint:
    """This session's MCP endpoint; a layer whose harness speaks MCP points it here."""

    url: str
    token: str


@dataclass(frozen=True, slots=True)
class ToolBatch:
    """One MCP call matched to its model message: its own key and every call that message made.

    Keys are the harness's own call identities, in the order the model requested them, so
    the core can tell whether a finish tool came alone or last.
    """

    key: str
    batch: tuple[str, ...]


class HarnessRequestError(ValueError):
    """The request or harness settings were rejected before anything was launched."""


class HarnessFailure(RuntimeError):
    """The harness control channel failed; in-flight outcomes are unknown."""


class HarnessSession(Protocol):
    @property
    def native_session(self) -> JsonValue: ...

    async def next_event(self) -> dict[str, JsonValue]:
        """Return one normalized event body without ``seq``; raise HarnessFailure on loss."""
        ...

    async def await_tool_call(
        self, name: str, arguments: dict[str, JsonValue], meta: Mapping[str, JsonValue]
    ) -> ToolBatch:
        """Return once the model message that requested this MCP call has been emitted.

        Raise HarnessFailure when the call cannot be matched to that message.
        """
        ...

    async def steer(self, messages: list[str]) -> None: ...

    async def prompt(self, message: str) -> None: ...

    async def tool_result(self, result: ToolResult) -> None: ...

    async def native_decision(self, action_id: str, decision: NativeDecision) -> None: ...

    async def cancel(self) -> None: ...

    async def close(self, *, abort: bool) -> None: ...


class HarnessAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def capabilities(self) -> dict[str, JsonValue]:
        """The capability manifest (ADR 0007, section 5) the Hub reports in its health."""
        ...

    def resolve(self, settings: dict[str, JsonValue], pin_directory: str | None) -> ResolvedHarness:
        """Validate settings on this machine; raise HarnessRequestError when they are invalid."""
        ...

    async def inspect(self, settings: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Check the installation without credentials or model requests."""
        ...

    async def launch(self, request: StartSession, mcp: McpEndpoint) -> HarnessSession:
        """Start and verify a session; raise HarnessRequestError or HarnessFailure.

        ``mcp`` serves exactly this session's tools and already resolves while launch runs
        (listing works; calls are refused until the prompt). Layers whose harness has no MCP
        support (such as Pi) ignore it and use their own bridge with the same semantics.
        A layer that uses it must emit ``assistant_message`` for each model message before
        that message's tool calls run: MCP calls since the last one form one batch, which
        the core needs to check that a finish tool came alone.
        """
        ...
