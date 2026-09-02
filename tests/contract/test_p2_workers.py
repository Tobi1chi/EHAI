from collections.abc import AsyncIterator

import pytest

from ehai.application import (
    OPENAI_CREDENTIAL_REF,
    AttemptActivity,
    AttemptExecutionKind,
    Connector,
    SessionPolicy,
    normalize_capabilities,
    supports_capabilities,
)
from ehai.domain import AttemptStatus


class _ContractConnector:
    async def start(self, request: str) -> str:
        return request

    async def events(
        self,
        execution: str,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[str]:
        del execution, after_cursor
        if False:
            yield ""

    async def inspect(self, execution: str) -> str:
        return execution

    async def cancel(self, execution: str) -> None:
        del execution

    async def recover(self, request: str) -> str:
        return request


def test_p2_execution_contract_is_fixed_and_unknown_capabilities_fail_closed() -> None:
    offered = normalize_capabilities(("workspace.read", "workspace.write"))

    assert supports_capabilities(offered=offered, required=("workspace.read",))
    assert not supports_capabilities(offered=offered, required=("provider.unknown",))
    with pytest.raises(ValueError, match="capabilities"):
        normalize_capabilities(("Workspace Read",))

    assert tuple(policy.value for policy in SessionPolicy) == ("new", "reuse", "fork")
    with pytest.raises(ValueError):
        SessionPolicy("unknown")

    assert tuple(kind.value for kind in AttemptExecutionKind) == (
        "builtin_turn",
        "external_execution",
    )
    assert tuple(activity.value for activity in AttemptActivity) == (
        "queued",
        "running",
        "waiting",
        "stalled",
    )
    assert AttemptActivity.RUNNING.value == AttemptStatus.RUNNING.value
    assert "queued" not in {status.value for status in AttemptStatus}
    assert isinstance(_ContractConnector(), Connector)
    assert OPENAI_CREDENTIAL_REF == "env:OPENAI_API_KEY"
