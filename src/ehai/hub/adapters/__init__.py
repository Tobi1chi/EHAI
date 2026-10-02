"""Compatibility-layer interface between the Hub service and one kind of Agent harness.

A compatibility layer launches an unmodified harness through its public CLI/SDK and
configuration, translates its native output into Hub events, and forwards host responses.
It owns no EHAI business rules and never executes EHAI tools itself.
"""

from __future__ import annotations

from typing import Protocol

from ehai import JsonValue
from ehai.hub.protocol import StartSession, ToolResult


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

    async def steer(self, messages: list[str]) -> None: ...

    async def prompt(self, message: str) -> None: ...

    async def tool_result(self, result: ToolResult) -> None: ...

    async def cancel(self) -> None: ...

    async def close(self, *, abort: bool) -> None: ...


class HarnessAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def launch(self, request: StartSession) -> HarnessSession:
        """Start and verify a session; raise HarnessRequestError or HarnessFailure."""
        ...
