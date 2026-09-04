"""Responses-backed Built-in Planner adapter without Worker execution state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime

from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, json_dumps, new_id, utc_now
from ehai.application.builtin_agent import (
    ModelClient,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ToolCall,
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

_MAX_PLANNER_STEPS = 256
"""Safety valve well above the 128-operation budget; not a functional limit.

The Planner loop terminates through the operation budget, the validation repair
budget, accepted finish_plan, or provider terminal states. This bound only stops
a pathological client that never converges, and never limits a valid flow that
respects the 128-mutation budget.
"""

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
        self._id_factory = id_factory
        self._clock = clock

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
        """Drive the graph Tool loop until finish_plan accepts the draft graph.

        The loop has no default wall-clock deadline: termination comes from the
        128-mutation operation budget, the 4 validation repair attempts, an
        accepted finish_plan, or provider terminal states.
        """
        client = self._model_client_factory(self._profile)
        try:
            runtime = PlanGraphToolRuntime(
                self._budget,
                planner_event_types=("planner.responses.completed",),
            )
            history: list[ModelMessage] = [
                ModelMessage(ModelRole.SYSTEM, _PLANNER_SYSTEM_PROMPT),
                ModelMessage(
                    ModelRole.USER,
                    "\n".join(
                        (
                            "EHAI BUILT-IN PLANNER PROTOCOL v2",
                            "Build the plan graph with the graph tools,",
                            "then call finish_plan.",
                            f"Graph mutation budget: {MAX_PLAN_OPERATIONS} operations;",
                            f"validation repair budget: {MAX_VALIDATION_RETRIES} attempts",
                            "after the first failed finish_plan.",
                            "--- PLANNER_INPUT_JSON ---",
                            json_dumps(dict(input_document)),
                        )
                    ),
                ),
            ]
            pending: list[ModelMessage] = []
            previous_response_id: str | None = None
            steps = 0
            while True:
                steps += 1
                if steps > _MAX_PLANNER_STEPS:
                    raise BuiltinPlannerError(
                        "Built-in Planner tool loop did not converge before the model step "
                        f"safety valve ({_MAX_PLANNER_STEPS} steps)"
                    )
                request = ModelRequest(
                    tuple(history),
                    runtime.tool_definitions(),
                    input_messages=tuple(pending) if previous_response_id else tuple(history),
                    previous_response_id=previous_response_id,
                    tool_choice="required",
                )
                response = await client.complete(request)
                assistant = ModelMessage(
                    ModelRole.ASSISTANT,
                    response.content,
                    tool_calls=response.tool_calls,
                )
                history.append(assistant)
                if response.provider_response_id is not None:
                    previous_response_id = response.provider_response_id
                    pending = [assistant]
                else:
                    pending.append(assistant)
                if not response.tool_calls:
                    raise BuiltinPlannerError(
                        "Built-in Planner response contained no ToolCalls; the plan must be built "
                        "with the graph tools and submitted through finish_plan"
                    )
                for index, call in enumerate(response.tool_calls):
                    if call.name == "finish_plan" and index != len(response.tool_calls) - 1:
                        result = _finish_ordering_diagnostic(index, response.tool_calls)
                    else:
                        result = runtime.execute(call.name, call.arguments)
                    tool_message = ModelMessage(
                        ModelRole.TOOL,
                        json_dumps(result),
                        call_id=call.call_id,
                    )
                    history.append(tool_message)
                    pending.append(tool_message)
                    if runtime.validation_budget_exhausted:
                        raise BuiltinPlannerError(
                            "Built-in Planner validation budget exhausted: finish_plan failed "
                            f"full validation {MAX_VALIDATION_RETRIES + 1} times"
                        )
                if runtime.finished:
                    return runtime.build_template()
        finally:
            await client.aclose()

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


def _finish_ordering_diagnostic(
    index: int,
    calls: tuple[ToolCall, ...],
) -> dict[str, JsonValue]:
    following_names: list[JsonValue] = [call.name for call in calls[index + 1 :]]
    return {
        "accepted": False,
        "issues": [
            {
                "code": "FINISH_TOOL_NOT_LAST",
                "location": f"response.tool_calls[{index}]",
                "message": (
                    "finish_plan must be the final Tool Call in its model Response; "
                    "review the following Tool results, then call finish_plan again "
                    "as the last Tool"
                ),
                "related_keys": following_names,
            }
        ],
    }
