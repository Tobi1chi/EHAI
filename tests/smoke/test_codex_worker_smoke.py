from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ehai import new_id
from ehai.application.workers import WorkerRequest
from ehai.domain.execution import Attempt, Run
from ehai.domain.goal import CompletionContract
from ehai.domain.planning import PlanNode
from ehai.infrastructure.workers import CodexWorkerAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("EHAI_RUN_CODEX_SMOKE") != "1",
    reason="set EHAI_RUN_CODEX_SMOKE=1 to use the existing local Codex authentication",
)


def test_real_codex_worker_returns_structured_candidate() -> None:
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("codex executable is not installed")
    now = datetime.now(UTC)
    goal_id = new_id()
    run = Run(goal_id, new_id(), run_id=new_id(), created_at=now).start(at=now)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal_id,
        ("artifact:non-empty",),
        (check_id,),
        created_at=now,
    ).confirm(confirmed_at=now)
    node = (
        PlanNode(
            new_id(),
            "produce smoke candidate",
            "Return one text candidate containing exactly EHAI_CODEX_SMOKE_OK.",
            required_check_ids=(check_id,),
        )
        .mark_ready()
        .start()
    )
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=now).start(at=now)
    request = WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        context={},
    )

    auth_environment = {
        name: os.environ[name]
        for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "AZURE_OPENAI_API_KEY")
        if name in os.environ
    }
    result = CodexWorkerAdapter(
        workspace=Path(__file__).resolve().parents[2],
        executable=executable,
        sandbox="read-only",
        timeout_seconds=120,
        env_overrides=auth_environment,
    ).execute(request)

    assert any(b"EHAI_CODEX_SMOKE_OK" in artifact.content for artifact in result.artifacts)
