from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ehai import new_id
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorStartRequest,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.workers import WorkerRequest
from ehai.domain.checking import CheckKind, CheckSpec
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode
from ehai.infrastructure.workers import CodexAppServerConnector

pytestmark = pytest.mark.skipif(
    os.environ.get("EHAI_RUN_CODEX_APP_SERVER_SMOKE") != "1",
    reason="set EHAI_RUN_CODEX_APP_SERVER_SMOKE=1 to use local Codex authentication",
)


def test_real_app_server_runs_two_isolated_sessions_and_interrupts_one(
    tmp_path: Path,
) -> None:
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if executable is None:
        pytest.skip("codex executable is not installed")

    async def scenario() -> None:
        connector = CodexAppServerConnector(
            workspace=tmp_path,
            executable=executable,
            model=os.environ.get("EHAI_CODEX_APP_SERVER_MODEL", "gpt-5.6-terra"),
            approval_policy="never",
            sandbox="read-only",
            request_timeout_seconds=60,
        )
        try:
            completed, cancellable = await asyncio.gather(
                connector.start(
                    ConnectorStartRequest(
                        _request(
                            "completed",
                            "Return a text candidate containing EHAI_APP_SESSION_ONE. "
                            "Do not use tools.",
                        )
                    )
                ),
                connector.start(
                    ConnectorStartRequest(
                        _request(
                            "cancelled",
                            "Use PowerShell to execute Start-Sleep -Seconds 120, then return "
                            "a text candidate containing EHAI_APP_SESSION_TWO.",
                        )
                    )
                ),
            )
            assert completed.provider_session_id != cancellable.provider_session_id
            first_events = asyncio.create_task(_collect(connector, completed))
            second_events = asyncio.create_task(_collect(connector, cancellable))
            await asyncio.sleep(2)
            await connector.cancel(cancellable)
            observed_first, observed_second = await asyncio.wait_for(
                asyncio.gather(first_events, second_events),
                timeout=180,
            )
            assert observed_first[-1].type is WorkerEventType.COMPLETED
            assert any(
                event.type is WorkerEventType.CANDIDATE
                and event.result is not None
                and any(
                    b"EHAI_APP_SESSION_ONE" in artifact.content
                    for artifact in event.result.artifacts
                )
                for event in observed_first
            )
            assert observed_second[-1].type is WorkerEventType.FAILED
            assert "interrupted" in (observed_second[-1].reason or "").lower()
        finally:
            await connector.close()

    asyncio.run(scenario())


async def _collect(
    connector: CodexAppServerConnector,
    execution: ConnectorExecution,
) -> list[WorkerEvent]:
    return [event async for event in connector.events(execution)]


def _request(title: str, instruction: str) -> WorkerRequest:
    now = datetime.now(UTC)
    goal_id = new_id()
    run = Run(goal_id, new_id(), created_at=now).start(at=now)
    check_id = new_id()
    check_spec = CheckSpec(
        "completion-artifact",
        CheckKind.ARTIFACT,
        "artifact:non-empty",
        check_id=check_id,
    )
    contract = CompletionContract.draft(
        goal_id,
        ("artifact:non-empty",),
        (check_id,),
        created_at=now,
    ).confirm(confirmed_at=now)
    node = (
        PlanNode(
            new_id(),
            title,
            instruction,
            required_check_ids=(check_id,),
        )
        .mark_ready()
        .start()
    )
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=now).start(at=now)
    return WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        required_check_specs=(check_spec,),
        context={},
    )
