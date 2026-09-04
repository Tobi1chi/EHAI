"""Read-only Built-in Agent Role for PlanRevision visualization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import cast

from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.builtin_agent import (
    CancellationToken,
    ModelClient,
    RecoverableToolError,
    ToolDefinition,
    ToolHandler,
)
from ehai.application.builtin_runtime import (
    BuiltinAgentRuntime,
    BuiltinRole,
    BuiltinRoleConfig,
    BuiltinRoleExecution,
    ToolRegistry,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.ports import ArtifactStore
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.domain.planning import PlanRevision
from ehai.domain.workers import WorkerCapability, WorkerKind, WorkerProfile
from ehai.infrastructure.openai_responses import OpenAIResponsesModelClient

_SYSTEM_PROMPT = """You are the EHAI Plan Visualizer. Consume only the validated
PlanRevision supplied in context. Produce a faithful Mermaid flowchart and finish by calling
submit_visualization. Do not modify the plan, invent nodes or edges, execute work, create
Attempts, or claim completion."""

VisualizerModelClientFactory = Callable[[WorkerProfile], ModelClient]


class BuiltinPlanVisualizer:
    """Render a validated PlanRevision through the shared Built-in Agent Runtime."""

    def __init__(
        self,
        *,
        runtime: BuiltinAgentRuntime,
        artifact_store: ArtifactStore,
        model: str,
        reasoning_effort: ReasoningEffort = None,
        model_client_factory: VisualizerModelClientFactory | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("BuiltinPlanVisualizer model must not be blank")
        self._runtime = runtime
        self._artifact_store = artifact_store
        self._profile = WorkerProfile(
            "builtin-visualizer",
            WorkerKind.BUILTIN,
            model,
            frozenset({WorkerCapability("visualizer.builtin")}),
            credential_ref=OPENAI_CREDENTIAL_REF,
        )
        self._reasoning_effort = reasoning_effort
        self._model_client_factory = model_client_factory or self._create_model_client
        self.last_session_id: ID | None = None

    def visualize(self, plan_revision: PlanRevision) -> Artifact:
        if not isinstance(plan_revision, PlanRevision):
            raise TypeError("plan_revision must be a PlanRevision")
        plan_revision.validate()
        submitted: dict[str, str] = {}

        async def submit(
            arguments: dict[str, JsonValue], cancellation: CancellationToken
        ) -> JsonValue:
            cancellation.raise_if_cancelled()
            name = _required_text(arguments, "name")
            content = _required_text(arguments, "content")
            if not content.lstrip().startswith(("flowchart ", "graph ")):
                raise RecoverableToolError(
                    "invalid_visualization",
                    "content must be a Mermaid flowchart beginning with 'flowchart ' or 'graph '",
                )
            missing_node_ids = [
                str(node.plan_node_id)
                for node in plan_revision.nodes
                if str(node.plan_node_id) not in content
            ]
            if missing_node_ids:
                raise RecoverableToolError(
                    "incomplete_visualization",
                    "Mermaid content must include every PlanNode ID; missing: "
                    + ", ".join(missing_node_ids),
                )
            submitted.update(name=name, content=content)
            return {"accepted": True, "name": name, "format": "mermaid"}

        definition = ToolDefinition(
            "submit_visualization",
            "Submit one Mermaid visualization of the supplied PlanRevision",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "content": {"type": "string", "minLength": 1, "maxLength": 200_000},
                },
                "required": ["name", "content"],
                "additionalProperties": False,
            },
            ends_turn=True,
        )
        registry = ToolRegistry((definition,), {"submit_visualization": cast(ToolHandler, submit)})
        config = BuiltinRoleConfig(
            role=BuiltinRole.VISUALIZER,
            system_prompt=_SYSTEM_PROMPT,
            tool_profile="plan-visualizer-v1",
            tool_names=("submit_visualization",),
            finish_tool="submit_visualization",
            permissions=frozenset({"plan.read"}),
        )
        session = self._runtime.create_session()
        self.last_session_id = session.agent_session_ref_id
        asyncio.run(
            self._runtime.run(
                config=config,
                registry=registry,
                model_client=self._model_client_factory(self._profile),
                session=session,
                execution=BuiltinRoleExecution(session.agent_session_ref_id),
                instruction="Render the exact PlanRevision as a Mermaid flowchart.",
                context={"plan_revision": _plan_document(plan_revision)},
            )
        )
        content = submitted["content"].encode("utf-8")
        artifact_id = new_id()
        artifact = Artifact(
            artifact_id=artifact_id,
            kind=ArtifactKind.EVIDENCE,
            name=submitted["name"],
            media_type="text/vnd.mermaid",
            size_bytes=len(content),
            sha256=sha256(content).hexdigest(),
            relative_path=f"objects/{artifact_id[:2]}/{artifact_id}.blob",
            created_at=utc_now(),
        )
        self._artifact_store.put(artifact, content)
        return artifact

    def _create_model_client(self, profile: WorkerProfile) -> ModelClient:
        return OpenAIResponsesModelClient(profile, reasoning_effort=self._reasoning_effort)


def _plan_document(plan: PlanRevision) -> dict[str, JsonValue]:
    return {
        "plan_revision_id": plan.plan_revision_id,
        "goal_id": plan.goal_id,
        "version": plan.version,
        "completion_contract_id": plan.completion_contract_id,
        "completion_contract_version": plan.completion_contract_version,
        "status": plan.status.value,
        "nodes": cast(
            list[JsonValue],
            [
                {
                    "plan_node_id": node.plan_node_id,
                    "title": node.title,
                    "kind": node.kind.value,
                    "required_dependency_ids": list(node.required_dependency_ids),
                }
                for node in plan.nodes
            ],
        ),
        "edges": cast(
            list[JsonValue],
            [
                {
                    "edge_id": edge.edge_id,
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "edge_type": edge.edge_type.value,
                    "branch_id": edge.branch_id,
                    "condition": edge.condition,
                }
                for edge in plan.edges
            ],
        ),
        "branches": cast(
            list[JsonValue],
            [
                {
                    "branch_id": branch.branch_id,
                    "label": branch.label,
                    "fork_node_id": branch.fork_node_id,
                    "node_ids": list(branch.node_ids),
                    "merge_node_id": branch.merge_node_id,
                    "status": branch.status.value,
                }
                for branch in plan.branches
            ],
        ),
    }


def _required_text(document: Mapping[str, JsonValue], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RecoverableToolError("invalid_arguments", f"{key} must be non-blank text")
    return value.strip()
