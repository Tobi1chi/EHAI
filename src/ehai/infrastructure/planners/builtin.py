"""Responses-backed Built-in Planner adapter without Worker execution state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from openai.types.shared import ReasoningEffort

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.builtin_agent import (
    AgentBudget,
    BuiltinSession,
    BuiltinSessionStore,
    CancellationToken,
    ModelClient,
    RecoverableToolError,
    ToolCall,
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
    COMMAND_EXIT_ZERO_CRITERION,
    ExplorationBudget,
    PlanningReply,
    PlanProposal,
    PlanTemplate,
    ReplanContext,
    build_plan_proposal,
    replan_from_template,
    require_p1_criteria,
    require_replan_context,
)
from ehai.application.ports import ArtifactStore
from ehai.application.sanitization import redact_sensitive_text
from ehai.domain.goal import Goal, GoalStatus
from ehai.domain.planning import PlanRevision
from ehai.domain.workers import WorkerCapability, WorkerKind, WorkerProfile
from ehai.infrastructure.builtin_tools import BuiltinToolRuntime
from ehai.infrastructure.openai_responses import (
    OpenAIResponsesModelClient,
    ResponsesEndpointCapabilities,
)
from ehai.infrastructure.planners.codex_protocol import build_codex_planner_input
from ehai.infrastructure.planners.plan_graph_tools import (
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
        "Investigate the supplied workspace with the read-only tools when available.",
        "Use set_plan_design to record requirements, scope, exclusions, assumptions,",
        "implementation design, and how requirements will be verified within authorized checks.",
        "Keep the design and graph consistent; describe task inputs, outputs and file boundaries.",
        "Every Worker node instruction must be implementation-ready for a smaller model: name the",
        "files or modules to inspect/change, exact interfaces or symbols, concrete ordered steps,",
        "dependencies and upstream inputs, expected outputs, and the local choices the Worker",
        "may make.",
        "Use the user's discussion history and current plan, including external review feedback.",
        "In discussion mode, ask_user ends this turn with a clarification or review response",
        "without creating or approving a plan. Do not force a graph before requirements are clear.",
        "Reference nodes and branches with stable local keys;",
        "EHAI assigns persistent IDs after finish_plan.",
        "Declare ordering with add_plan_edge dependency edges;",
        "node required dependencies are derived from them deterministically,",
        "so never maintain a second dependency list.",
        "Choose a linear dependency chain when the Goal is certain,",
        "or use a general DAG when work can be genuinely parallel: omit artificial edges between",
        "independent cooperating tasks and converge them at one integration node that depends",
        "on all.",
        "Use a bounded exploration structure (fork, two or more mutually alternative branches,",
        "evaluator, merge) only when approaches must be compared; state evaluator selection",
        "criteria.",
        "For coding alternatives, candidate Workers must implement real code in their isolated",
        "worktrees, not merely return competing design suggestions unless the user asks for that.",
        "The host, not Workers, launches tasks, schedules dependencies, prunes branches, and",
        "prepares merged upstream code. Do not instruct a fork or merge Worker to dispatch Agents.",
        "Preserve exact user-supplied literals, signatures, punctuation and whitespace in both",
        "the design and final Gate; do not normalize them or invent different assumptions.",
        "The graph must have one final work or merge integration sink and every task must feed it.",
        "The final sink owns the required completion checks; intermediate Workers do not need",
        "fake Gates.",
        "When command:exit-zero is selected, call set_final_gate with the exact behavioral argv",
        "sequence.",
        "If host_check_configuration contains an explicit command argv, use that exact argv.",
        "Do not invent a generic lint or unit-test checklist as the final behavioral gate.",
        "Every accepted or rejected graph mutation counts toward the plan",
        "operation budget; inspect_plan and finish_plan are free.",
        "finish_plan runs full validation and returns issues with local-key",
        "locations; repair them with the same tools, then call finish_plan again.",
        "Checks and Gates outside this process retain all completion authority:",
        "executable checks are host-selected and cannot be changed or weakened by tools.",
        "You may explain missing verification and recommend additions for the user to configure.",
        "The host Check Runner runs configured checks after candidate submission.",
        "Do not create Worker nodes solely to rerun these host checks.",
        "Host check argv configuration does not grant a Worker permission to execute that command.",
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
        workspace: Path | None = None,
        artifact_store: ArtifactStore | None = None,
        check_configuration: Mapping[str, JsonValue] | None = None,
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
        if (workspace is None) != (artifact_store is None):
            raise ValueError("Planner workspace and artifact store must be configured together")
        self._workspace = None if workspace is None else workspace.resolve(strict=True)
        if self._workspace is not None and not self._workspace.is_dir():
            raise ValueError("Planner workspace must be a directory")
        self._artifact_store = artifact_store
        self._check_configuration = dict(check_configuration or {})
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
        template = asyncio.run(
            self._build_template(
                input_document,
                require_final_gate=COMMAND_EXIT_ZERO_CRITERION in normalized_criteria,
            )
        )
        return build_plan_proposal(
            goal,
            normalized_criteria,
            template,
            id_factory=self._id_factory,
            clock=self._clock,
        )

    def discuss(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        message: str,
        history: tuple[dict[str, JsonValue], ...],
        base: PlanRevision | None,
    ) -> PlanningReply:
        self._require_goal(goal, allow_completion_contract=True)
        normalized = require_p1_criteria(criteria, "Planning discussion")
        document = build_codex_planner_input(goal, normalized, self._budget, base)
        document["discussion_history"] = [dict(item) for item in history]
        document["user_message"] = message
        document["current_design_document"] = None if base is None else base.design_document
        template, answer, session_id = asyncio.run(
            self._run_planner(
                document,
                discussion=True,
                require_final_gate=COMMAND_EXIT_ZERO_CRITERION in normalized,
            )
        )
        proposal = (
            None
            if template is None
            else build_plan_proposal(
                goal, normalized, template, id_factory=self._id_factory, clock=self._clock
            )
        )
        return PlanningReply(answer, session_id, proposal)

    async def _build_template(
        self,
        input_document: Mapping[str, JsonValue],
        *,
        require_final_gate: bool,
    ) -> PlanTemplate:
        template, _, _ = await self._run_planner(
            input_document,
            discussion=False,
            require_final_gate=require_final_gate,
        )
        if template is None:
            raise BuiltinPlannerError("Proposal finished without a plan")
        return template

    async def _run_planner(
        self,
        input_document: Mapping[str, JsonValue],
        *,
        discussion: bool,
        require_final_gate: bool,
    ) -> tuple[PlanTemplate | None, str, ID]:
        graph = PlanGraphToolRuntime(
            self._budget,
            planner_event_types=("planner.responses.completed",),
            require_final_gate=require_final_gate,
        )
        design: dict[str, str] = {}
        answer: dict[str, str] = {}
        workspace_tools = (
            None
            if self._workspace is None or self._artifact_store is None
            else BuiltinToolRuntime(
                workspace=self._workspace,
                artifact_store=self._artifact_store,
                allow_workspace_write=False,
            )
        )
        registry, names = _planner_registry(graph, design, answer, workspace_tools, discussion)
        config = BuiltinRoleConfig(
            role=BuiltinRole.PLANNER,
            system_prompt=_PLANNER_SYSTEM_PROMPT,
            tool_profile="planner-discussion-v1" if discussion else "planner-design-v1",
            tool_names=names,
            finish_tool="finish_plan",
            permissions=frozenset(
                {"plan.read", "plan.write"}
                | ({"workspace.read"} if workspace_tools is not None else set())
            ),
            tool_choice="required",
            final_tool_requires_only=False,
        )
        session = self._agent_runtime.create_session()
        self._last_session = session
        try:
            await self._agent_runtime.run(
                config=config,
                registry=registry,
                model_client=self._model_client_factory(self._profile),
                session=session,
                execution=BuiltinRoleExecution(session.agent_session_ref_id),
                instruction=(
                    "Discuss the user's request. Ask for clarification or respond to review using "
                    "ask_user when appropriate; otherwise prepare a detailed design and graph, "
                    "then finish_plan."
                    if discussion
                    else "Prepare a detailed design with set_plan_design, build the graph, "
                    "then finish_plan."
                ),
                context={
                    "planner_input": dict(input_document),
                    "host_check_configuration": self._check_configuration,
                },
            )
        finally:
            if workspace_tools is not None:
                await workspace_tools.aclose()
        if "message" in answer:
            return None, answer["message"], session.agent_session_ref_id
        if not graph.finished:
            raise BuiltinPlannerError(
                "Built-in Planner response contained no ToolCalls; the plan must be built "
                "with the graph tools and submitted through finish_plan"
            )
        document = "\n\n".join(f"## {name}\n\n{text}" for name, text in design.items())
        return (
            replace(graph.build_template(), design_document=document),
            "Draft ready. Review its design and checks; explicit approval is required.",
            session.agent_session_ref_id,
        )

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


def _planner_registry(
    graph: PlanGraphToolRuntime,
    design: dict[str, str],
    answer: dict[str, str],
    workspace_tools: BuiltinToolRuntime | None,
    discussion: bool,
) -> tuple[ToolRegistry, tuple[str, ...]]:
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
            if tool_name == "finish_plan" and not design:
                return {"accepted": False, "error": "Call set_plan_design before finish_plan"}
            result = graph.execute(tool_name, arguments)
            if graph.validation_budget_exhausted:
                raise BuiltinPlannerError(
                    "Built-in Planner validation budget exhausted: finish_plan failed "
                    f"full validation {MAX_VALIDATION_RETRIES + 1} times"
                )
            return result

        handlers[name] = execute
    sections = (
        "requirements",
        "scope",
        "out_of_scope",
        "assumptions",
        "implementation",
        "acceptance_and_permissions",
    )
    definitions.append(
        ToolDefinition(
            "set_plan_design",
            "Record the reviewable design for this exact plan; use 'none' for empty exclusions",
            {
                "type": "object",
                "properties": {
                    name: {"type": "string", "minLength": 1, "maxLength": 10_000}
                    for name in sections
                },
                "required": list(sections),
                "additionalProperties": False,
            },
        )
    )

    async def set_design(
        arguments: dict[str, JsonValue], cancellation: CancellationToken
    ) -> JsonValue:
        cancellation.raise_if_cancelled()
        values: dict[str, str] = {}
        for name in sections:
            value = arguments.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > 10_000:
                raise RecoverableToolError("invalid_design", f"Invalid design section {name}")
            values[name] = redact_sensitive_text(value.strip())
        design.clear()
        design.update(values)
        return {"accepted": True}

    handlers["set_plan_design"] = set_design
    if discussion:
        definitions.append(
            ToolDefinition(
                "ask_user",
                "End this discussion turn with a question or review response, without a plan",
                {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "minLength": 1, "maxLength": 8000}
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
                ends_turn=True,
            )
        )

        async def ask_user(
            arguments: dict[str, JsonValue], cancellation: CancellationToken
        ) -> JsonValue:
            cancellation.raise_if_cancelled()
            value = arguments.get("message")
            if not isinstance(value, str) or not value.strip() or len(value) > 8000:
                raise RecoverableToolError("invalid_message", "A question or reply is required")
            answer["message"] = redact_sensitive_text(value.strip())
            return {"accepted": True, "message": answer["message"]}

        handlers["ask_user"] = ask_user
    if workspace_tools is not None:
        read_tools: BuiltinToolRuntime = workspace_tools
        for definition in workspace_tools.tool_set.definitions:
            if definition.name not in {"workspace_list", "workspace_read", "workspace_search"}:
                continue
            definitions.append(definition)

            async def read_workspace(
                arguments: dict[str, JsonValue],
                cancellation: CancellationToken,
                *,
                name: str = definition.name,
                tools: BuiltinToolRuntime = read_tools,
            ) -> JsonValue:
                return await tools.executor.execute(
                    ToolCall(str(new_id()), name, arguments), cancellation
                )

            handlers[definition.name] = read_workspace
    return ToolRegistry(tuple(definitions), handlers), tuple(item.name for item in definitions)
