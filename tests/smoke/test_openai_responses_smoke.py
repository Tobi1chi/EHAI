from __future__ import annotations

import asyncio
import os

import pytest

from ehai.application.builtin_agent import (
    ModelMessage,
    ModelRequest,
    ModelRole,
    ToolDefinition,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.domain.workers import WorkerKind, WorkerProfile
from ehai.infrastructure.openai_responses import OpenAIResponsesModelClient

pytestmark = pytest.mark.skipif(
    os.environ.get("EHAI_RUN_OPENAI_SMOKE") != "1",
    reason="set EHAI_RUN_OPENAI_SMOKE=1 for the explicit Responses smoke",
)


def test_real_openai_responses_returns_text_or_strict_function_call() -> None:
    if "OPENAI_API_KEY" not in os.environ:
        pytest.skip("OPENAI_API_KEY is not available")
    model = os.environ.get("EHAI_OPENAI_SMOKE_MODEL")
    if not model:
        pytest.skip("set EHAI_OPENAI_SMOKE_MODEL to an explicitly authorized model")
    profile = WorkerProfile(
        "responses-smoke",
        WorkerKind.BUILTIN,
        model,
        credential_ref=OPENAI_CREDENTIAL_REF,
    )
    tool = ToolDefinition(
        "submit_candidate",
        "Submit the final no-side-effect smoke candidate",
        {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
        ends_turn=True,
    )
    client = OpenAIResponsesModelClient(profile, max_output_tokens=256)

    async def invoke() -> None:
        try:
            response = await client.complete(
                ModelRequest(
                    (
                        ModelMessage(
                            ModelRole.SYSTEM,
                            "Return a concise candidate; do not request external side effects.",
                        ),
                        ModelMessage(
                            ModelRole.USER,
                            "Reply with EHAI_OPENAI_RESPONSES_SMOKE_OK or submit it as content.",
                        ),
                    ),
                    (tool,),
                )
            )
            assert response.final_text or response.tool_calls
        finally:
            await client.aclose()

    asyncio.run(invoke())
