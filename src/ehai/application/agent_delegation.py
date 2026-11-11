"""Port for a single non-Git, read-only workflow role invocation."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from ehai import JsonValue
from ehai.application.connector_models import ConnectorAction
from ehai.application.custom_workflow_models import AgentStep

# The third argument is a schema-refusal reason, or None for a validated request.
# Both paths consume the query budget and are audited; refusals never queue a call.
AgentQuery = Callable[[str, dict[str, JsonValue], str | None], Awaitable[JsonValue]]


class AgentDelegation(Protocol):
    @property
    def harness_kind(self) -> str: ...
    @property
    def model(self) -> str: ...

    async def run(
        self,
        *,
        run_id: str,
        invocation_id: str,
        attempt: int,
        step: AgentStep,
        instruction: str,
        actions: dict[str, ConnectorAction],
        query: AgentQuery,
    ) -> dict[str, JsonValue]: ...
