"""Responses-backed Built-in Planner adapter without Worker execution state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime

from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.builtin_agent import (
    AgentBudget,
    BuiltinSession,
    BuiltinSessionStore,
    CancellationToken,
    ModelClient,
    ToolDefinition,
    ToolHandler,
)
from ehai.application.builtin_runtime import (
    BuiltinAgentRuntime,
    BuiltinRole,
    BuiltinRoleConfig,
    BuiltinRoleExecution,
    MemoryBuiltinSessionStore,
    ToolRegistry,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.planner import (
    ExplorationBudget,
    PlanProposal,
    PlanTemplate,
    ReplanContext,
    build_plan_proposal,
    replan_from_template,
    require_p1_criteria,
    require_replan_context,
)
from ehai.domain.goal import Goal, GoalStatus
from ehai.domain.planning import PlanRevision
from ehai.domain.workers import WorkerCapability, WorkerKind, WorkerProfile
from ehai.infrastructure.openai_responses import (
    OpenAIResponsesModelClient,
    ResponsesEndpointCapabilities,
)
from ehai.infrastructure.planners.codex_protocol import build_codex_planner_input
from ehai.infrastructure.planners.plan_graph_tools import (
    MAX_PLAN_OPERATIONS,
    MAX_VALIDATION_RETRIES,
    PlanGraphToolRuntime,
)

_DEFAULT_PLANNER_BUDGET = ExplorationBudget(max_attempts=24, max_width=3, max_depth=4)

_PLANNER_AGENT_BUDGET = AgentBudget(256, 512, None, 8 * 1024 * 1024)

_PLANNER_SYSTEM_PROMPT = "\n".join(
    (
        "You are the EHAI Built-in Planner. Propose a plan only.",
        "Do not execute work, create Worker Attempts, create Sessions,",
        "or claim the Goal is complete.",
        "Construct the plan graph by calling the provided graph tools directly.",
        "Reference nodes and branches with stable local keys;",
        "EHAI assigns persistent IDs after finish_plan.",
        "Declare ordering with add_plan_edge dependency edges;",
        "node required dependencies are derived from them deterministically,",
        "so never maintain a second dependency list.",
        "Choose a linear dependency chain when the Goal is certain,",
        "or a bounded exploration structure (fork, two or more branches,",
        "evaluator, merge) when approaches must be compared.",
        "Every accepted or rejected graph mutation counts toward the plan",
        "operation budget; inspect_plan and finish_plan are free.",
        "finish_plan runs full validation and returns issues with local-key",
        "locations; repair them with the same tools, then call finish_plan again.",
        "Checks and Gates outside this process retain all completion authority:",
        "you cannot create, remove, or weaken completion criteria.",
    )
)


class BuiltinPlannerError(RuntimeError):
    """Raised when the Built-in Responses Planner cannot produce a valid proposal."""


PlannerModelClientFactory = Callable[[WorkerProfile], ModelClient]


class BuiltinPlannerAdapter:
    """Propose a bounded PlanTemplate using the Built-in Responses ModelClient seam."""

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: ReasoningEffort = None,
        budget: ExplorationBudget | None = None,
        model_client_factory: PlannerModelClientFactory | None = None,
        endpoint_capabilities: ResponsesEndpointCapabilities | None = None,
        agent_runtime: BuiltinAgentRuntime | None = None,
        session_store: BuiltinSessionStore | None = None,
        id_factory: Callable[[], ID] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("BuiltinPlannerAdapter model must not be blank")
        self._budget = budget or _DEFAULT_PLANNER_BUDGET
        if not isinstance(self._budget, ExplorationBudget):
            raise TypeError("budget must be an ExplorationBudget")
        self._profile = WorkerProfile(
            "builtin-planner",
            WorkerKind.BUILTIN,
            model,
            frozenset({WorkerCapability("planner.builtin")}),
            credential_ref=OPENAI_CREDENTIAL_REF,
        )
        self._reasoning_effort = reasoning_effort
        self._endpoint_capabilities = endpoint_capabilities or ResponsesEndpointCapabilities()
        if not isinstance(self._endpoint_capabilities, ResponsesEndpointCapabilities):
            raise TypeError("endpoint_capabilities must be ResponsesEndpointCapabilities")
        self._model_client_factory = model_client_factory or self._create_model_client
        if agent_runtime is not None and session_store is not None:
            raise ValueError("agent_runtime and session_store are mutually exclusive")
        self._agent_runtime = agent_runtime or BuiltinAgentRuntime(
            session_store or MemoryBuiltinSessionStore(),
            budget=_PLANNER_AGENT_BUDGET,
        )
        self._last_session: BuiltinSession | None = None
        self._id_factory = id_factory
        self._clock = clock

    @property
    def last_session(self) -> BuiltinSession | None:
        return self._last_session

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        """Ask Responses for a provider-specific plan and assemble a draft proposal."""
        self._require_goal(goal, allow_completion_contract=False)
        return self._propose_template(goal, criteria)

    def replan(
        self,
        goal: Goal,
        base: PlanRevision,
        criteria: tuple[str, ...],
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        """Create a versioned replacement through the application GraphPatch boundary."""
        self._require_goal(goal, allow_completion_contract=True)
        require_replan_context(goal, base)
        template = self._propose_template(goal, criteria, base=base, context=context)
        return replan_from_template(goal, base, template)

    def _propose_template(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        *,
        base: PlanRevision | None = None,
        context: ReplanContext | None = None,
    ) -> PlanProposal:
        normalized_criteria = require_p1_criteria(criteria, "BuiltinPlannerAdapter")
        input_document = build_codex_planner_input(
            goal,
            normalized_criteria,
            self._budget,
            base,
            context,
        )
        template = asyncio.run(self._build_template(input_document))
        return build_plan_proposal(
            goal,
            normalized_criteria,
            template,
            id_factory=self._id_factory,
            clock=self._clock,
        )

    async def _build_template(self, input_document: Mapping[str, JsonValue]) -> PlanTemplate:
        """Build a Plan through the shared Built-in Agent Runtime."""
        graph = PlanGraphToolRuntime(
            self._budget,
            planner_event_types=("planner.responses.completed",),
        )
        registry = _planner_registry(graph)
        config = BuiltinRoleConfig(
            role=BuiltinRole.PLANNER,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            tool_profile="planner-graph-v1",
            tool_names=tuple(definition.name for definition in graph.tool_definitions()),
            finish_tool="finish_plan",
            permissions=frozenset({"plan.read", "plan.write"}),
            tool_choice="required",
            final_tool_requires_only=False,
        )
        session = self._agent_runtime.create_session()
        self._last_session = session
        await self._agent_runtime.run(
            config=config,
            registry=registry,
            model_client=self._model_client_factory(self._profile),
            session=session,
            execution=BuiltinRoleExecution(session.agent_session_ref_id),
            instruction=(
                "EHAI BUILT-IN PLANNER PROTOCOL v2. Build the plan graph with the graph "
                "tools, then call finish_plan. "
                f"Mutation budget: {MAX_PLAN_OPERATIONS}; validation repairs: "
                f"{MAX_VALIDATION_RETRIES}."
            ),
            context={"planner_input": dict(input_document)},
        )
        if not graph.finished:
            raise BuiltinPlannerError(
                "Built-in Planner response contained no ToolCalls; the plan must be built "
                "with the graph tools and submitted through finish_plan"
            )
        return graph.build_template()

    def _create_model_client(self, profile: WorkerProfile) -> ModelClient:
        return OpenAIResponsesModelClient(
            profile,
            reasoning_effort=self._reasoning_effort,
            background=True,
            endpoint_capabilities=self._endpoint_capabilities,
        )

    @staticmethod
    def _require_goal(goal: Goal, *, allow_completion_contract: bool) -> None:
        if not isinstance(goal, Goal):
            raise TypeError("goal must be a Goal")
        if goal.status is not GoalStatus.OPEN:
            raise ValueError(f"Goal {goal.goal_id} must be open before planning")
        if not allow_completion_contract and goal.completion_contract is not None:
            raise ValueError(f"Goal {goal.goal_id} already has a CompletionContract")


def _planner_registry(graph: PlanGraphToolRuntime) -> ToolRegistry:
    definitions: list[ToolDefinition] = []
    handlers: dict[str, ToolHandler] = {}
    for graph_definition in graph.tool_definitions():
        name = graph_definition.name
        definitions.append(
            ToolDefinition(
                name,
                graph_definition.description,
                graph_definition.input_schema,
                ends_turn=name == "finish_plan",
            )
        )

        async def execute(
            arguments: dict[str, JsonValue],
            cancellation: CancellationToken,
            *,
            tool_name: str = name,
        ) -> JsonValue:
            cancellation.raise_if_cancelled()
            result = graph.execute(tool_name, arguments)
            if graph.validation_budget_exhausted:
                raise BuiltinPlannerError(
                    "Built-in Planner validation budget exhausted: finish_plan failed "
                    f"full validation {MAX_VALIDATION_RETRIES + 1} times"
                )
            return result

        handlers[name] = execute
    return ToolRegistry(tuple(definitions), handlers)
