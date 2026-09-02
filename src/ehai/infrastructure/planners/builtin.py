"""Responses-backed Built-in Planner adapter without Worker execution state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime

from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, json_dumps, json_loads, new_id, utc_now
from ehai.application.builtin_agent import (
    ModelClient,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ToolDefinition,
)
from ehai.application.execution_contracts import OPENAI_CREDENTIAL_REF
from ehai.application.planner import (
    ExplorationBudget,
    ExplorationUsage,
    PlanProposal,
    build_plan_proposal,
    replan_from_template,
    require_p1_criteria,
    require_replan_context,
    two_branch_plan_template,
)
from ehai.domain.goal import Goal, GoalStatus
from ehai.domain.planning import PlanRevision
from ehai.domain.workers import WorkerCapability, WorkerKind, WorkerProfile
from ehai.infrastructure.openai_responses import OpenAIResponsesModelClient
from ehai.infrastructure.planners.codex_protocol import (
    CodexPlannerProtocolError,
    ParsedCodexPlan,
    build_codex_planner_input,
    codex_planner_output_schema_json,
    parse_codex_planner_result,
)

_P1_USAGE = ExplorationUsage(width=2, depth=1, attempts=5)


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
        id_factory: Callable[[], ID] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("BuiltinPlannerAdapter model must not be blank")
        self._budget = budget or ExplorationBudget(max_attempts=5)
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
    ) -> PlanProposal:
        """Create a versioned replacement through the application GraphPatch boundary."""
        self._require_goal(goal, allow_completion_contract=True)
        require_replan_context(goal, base)
        template = self._propose_template(goal, criteria, base=base)
        return replan_from_template(goal, base, template)

    def _propose_template(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        *,
        base: PlanRevision | None = None,
    ) -> PlanProposal:
        normalized_criteria = require_p1_criteria(criteria, "BuiltinPlannerAdapter")
        _P1_USAGE.require_within(self._budget)
        input_document = build_codex_planner_input(
            goal,
            normalized_criteria,
            self._budget,
            base,
        )
        response = asyncio.run(self._complete(input_document))
        parsed = self._parse_response(goal.goal_id, response)
        return build_plan_proposal(
            goal,
            normalized_criteria,
            two_branch_plan_template(
                fork_title=parsed.fork.title,
                fork_instruction=parsed.fork.instruction,
                first_label=parsed.branches[0].label,
                first_title=parsed.branches[0].title,
                first_instruction=parsed.branches[0].instruction,
                second_label=parsed.branches[1].label,
                second_title=parsed.branches[1].title,
                second_instruction=parsed.branches[1].instruction,
                evaluator_title=parsed.evaluator.title,
                evaluator_instruction=parsed.evaluator.instruction,
                merge_title=parsed.merge.title,
                merge_instruction=parsed.merge.instruction,
                budget=self._budget,
                usage=_P1_USAGE,
                planner_event_types=("planner.responses.completed",),
            ),
            id_factory=self._id_factory,
            clock=self._clock,
        )

    async def _complete(self, input_document: Mapping[str, JsonValue]) -> ModelResponse:
        client = self._model_client_factory(self._profile)
        try:
            return await client.complete(
                ModelRequest(
                    (
                        ModelMessage(
                            ModelRole.SYSTEM,
                            "You are the EHAI Built-in Planner. Propose a plan only. "
                            "Do not execute work, create Worker Attempts, create Sessions, "
                            "or claim the Goal is complete.",
                        ),
                        ModelMessage(
                            ModelRole.USER,
                            "\n".join(
                                (
                                    "EHAI BUILT-IN PLANNER PROTOCOL v1",
                                    "The graph shape is fixed: fork, two independent "
                                    "branches, evaluator, then merge.",
                                    "Checks and Gates outside this process retain all "
                                    "completion authority.",
                                    "Return the plan by calling submit_plan, or as exactly "
                                    "one JSON object matching that tool schema.",
                                    "--- PLANNER_INPUT_JSON ---",
                                    json_dumps(dict(input_document)),
                                )
                            ),
                        ),
                    ),
                    (_submit_plan_tool(),),
                )
            )
        finally:
            await client.aclose()

    def _parse_response(self, goal_id: ID, response: ModelResponse) -> ParsedCodexPlan:
        try:
            if response.tool_calls:
                if len(response.tool_calls) != 1 or response.tool_calls[0].name != "submit_plan":
                    raise CodexPlannerProtocolError("Planner must call only submit_plan")
                return parse_codex_planner_result(json_dumps(response.tool_calls[0].arguments))
            document = response.final_text or response.content
            return parse_codex_planner_result(document)
        except (ValueError, CodexPlannerProtocolError) as error:
            raise BuiltinPlannerError(
                f"Goal {goal_id} Built-in Planner returned an invalid structured result: {error}"
            ) from error

    def _create_model_client(self, profile: WorkerProfile) -> ModelClient:
        return OpenAIResponsesModelClient(
            profile,
            reasoning_effort=self._reasoning_effort,
        )

    @staticmethod
    def _require_goal(goal: Goal, *, allow_completion_contract: bool) -> None:
        if not isinstance(goal, Goal):
            raise TypeError("goal must be a Goal")
        if goal.status is not GoalStatus.OPEN:
            raise ValueError(f"Goal {goal.goal_id} must be open before planning")
        if not allow_completion_contract and goal.completion_contract is not None:
            raise ValueError(f"Goal {goal.goal_id} already has a CompletionContract")


def _submit_plan_tool() -> ToolDefinition:
    schema = json_loads(codex_planner_output_schema_json())
    if not isinstance(schema, dict):
        raise RuntimeError("planner output schema must be a JSON object")
    return ToolDefinition(
        "submit_plan",
        "Submit one bounded EHAI exploration PlanTemplate.",
        schema,
    )
