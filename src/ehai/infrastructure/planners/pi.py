"""Pi adapter for the Planner role: run PlannerRole through the Hub with a Pi backend."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.agent_roles import MemoryAgentTraceStore
from ehai.application.agent_trace import AgentTraceStore
from ehai.application.notes import NoteService
from ehai.application.planner import ExplorationBudget
from ehai.application.ports import ArtifactStore
from ehai.hub.adapters.pi.config import PiBackendConfig
from ehai.infrastructure.hub_runtime import HubRoleRunner
from ehai.infrastructure.planners.role import PlannerError, PlannerRole

# Kept for importers and stored event history; the role itself is harness-neutral.
PiPlannerError = PlannerError
PI_PLANNER_COMPLETED = "planner.pi.completed"


class PiPlannerAdapter(PlannerRole):
    """PlannerRole whose role runner launches Pi through the Hub."""

    def __init__(
        self,
        *,
        model: str,
        backend: PiBackendConfig,
        reasoning_effort: str | None = None,
        timeout_seconds: float = 120.0,
        budget: ExplorationBudget | None = None,
        session_store: AgentTraceStore | None = None,
        workspace: Path | None = None,
        artifact_store: ArtifactStore | None = None,
        check_configuration: Mapping[str, JsonValue] | None = None,
        worker_capabilities: Mapping[str, JsonValue] | None = None,
        note_service: NoteService | None = None,
        project_context_resolver: Callable[[ID, ID | None], dict[str, JsonValue]] | None = None,
        id_factory: Callable[[], ID] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(
            runner=HubRoleRunner(
                session_store or MemoryAgentTraceStore(),
                backend=backend,
                state_root=backend.agent_dir / "ehai-sessions",
            ),
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            budget=budget,
            workspace=workspace,
            artifact_store=artifact_store,
            check_configuration=check_configuration,
            worker_capabilities=worker_capabilities,
            note_service=note_service,
            project_context_resolver=project_context_resolver,
            id_factory=id_factory,
            clock=clock,
            completion_event_type=PI_PLANNER_COMPLETED,
        )
