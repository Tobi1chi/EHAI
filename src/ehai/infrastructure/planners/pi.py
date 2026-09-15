"""Pi-backed Planner harness without Worker execution state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ehai import ID, JsonValue, new_id, utc_now
from ehai.application.agent_contracts import (
    CancellationToken,
    RecoverableToolError,
    ToolCall,
    ToolDefinition,
    ToolHandler,
)
from ehai.application.agent_roles import (
    AgentRole,
    AgentRoleConfig,
    AgentRoleExecution,
    MemoryAgentTraceStore,
    ToolRegistry,
)
from ehai.application.agent_trace import (
    AgentTrace,
    AgentTraceStore,
)
from ehai.application.plan_graph_tools import (
    MAX_VALIDATION_RETRIES,
    PlanGraphToolRuntime,
)
from ehai.application.planner import (
    COMMAND_EXIT_ZERO_CRITERION,
    HUMAN_CRITERION_PREFIX,
    ExplorationBudget,
    PlanningReply,
    PlanProposal,
    PlanTemplate,
    ReplanContext,
    build_plan_proposal,
    discussion_proposal_criteria,
    normalize_discussion_criteria,
    replan_from_template,
    require_p1_criteria,
    require_replan_context,
)
from ehai.application.ports import ArtifactStore
from ehai.application.process_planner import build_process_proposal
from ehai.application.process_review import ProcessBoundaryReport
from ehai.application.process_review_context import ProcessReviewContext
from ehai.application.sanitization import redact_sensitive_text
from ehai.domain.checking import CheckSpec
from ehai.domain.goal import Goal, GoalStatus
from ehai.domain.planning import PlanRevision
from ehai.domain.process import (
    ProcessRevision,
    process_approval_identity,
    process_graph_definition,
)
from ehai.domain.workers import WorkerCapability, WorkerKind, WorkerProfile
from ehai.infrastructure.host_tools import HostToolRuntime
from ehai.infrastructure.pi_config import PiBackendConfig
from ehai.infrastructure.pi_runtime import PiRoleRunner
from ehai.infrastructure.planners.codex_protocol import build_codex_planner_input

_DEFAULT_PLANNER_BUDGET = ExplorationBudget(max_attempts=24, max_width=3, max_depth=4)


def _requires_final_gate(criteria: tuple[str, ...]) -> bool:
    return any(
        criterion == COMMAND_EXIT_ZERO_CRITERION or criterion.startswith(HUMAN_CRITERION_PREFIX)
        for criterion in criteria
    )


_PLANNER_SYSTEM_PROMPT = "\n".join(
    (
        "You are the EHAI Pi Planner. Propose a plan only.",
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
        "may make. State what is outside this node's deliverable and which missing prerequisite",
        "or scope change requires report_blocked rather than expanding the task. Permit normal",
        "investigation and debugging within the objective; do not prescribe every tool call or",
        "turn incidental issues into new requirements. Independent node work stays separate.",
        "Declare required_capabilities and session_policy when the task needs a",
        "specific Worker preset; omitted values default to no required capabilities and a new",
        "physical Agent Session. Capability labels are routing requirements only, not Tool grants.",
        "A non-new Session policy must match an explicitly supporting preset and",
        "does not by itself establish shared Phase Session behavior.",
        "Distinguish physical Agent Sessions from the host-owned logical Phase Session:",
        "Pi phase members share durable phase discussion and context while keeping their",
        "own physical Agent Sessions and isolated writable workspaces. A session_policy of new",
        "does not disable that logical phase continuity. Do not describe same-phase work as",
        "having no shared phase context, or promise concurrent use of one physical conversation.",
        "Process-version changes alone do not clear an unchanged logical Phase Session.",
        "Use the user's discussion history and current plan, including external review feedback.",
        "For replan_context, retain intervention kind and source task/Attempt/process identity.",
        "Replies are continuation facts, not approval of a changed boundary. Alternative routes",
        "do not resolve unknown external effects. Summaries are bounded: if intervention_count",
        "exceeds the supplied list, do not treat omitted questions or replies as absent/resolved.",
        "In discussion mode, ask_user ends this turn with a clarification or review response",
        "without creating or approving a plan. Do not force a graph before requirements are clear.",
        "Reference nodes and branches with stable local keys;",
        "EHAI assigns persistent IDs after finish_plan.",
        "Declare ordering with add_plan_edge dependency edges;",
        "node required dependencies are derived from them deterministically,",
        "so never maintain a second dependency list.",
        "Prefer independent work to run in parallel when dependencies, workspace isolation, and",
        "capacity allow; use a linear chain only when the Goal requires it, and otherwise use",
        "a DAG",
        "with no artificial edges between",
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
        "The graph must have one final work, merge, or reviewer sink and every task must feed it.",
        "Use set_plan_phase to partition every node into ordered execution phases. Each phase",
        "must end at one reviewer node that is also its Gate node; every phase node must feed",
        "that reviewer. Declare the in-phase work or merge nodes that receive rework when the",
        "Reviewer Gate fails; never choose the Reviewer itself. A final reviewer may be the graph",
        "sink and owns the final Gate.",
        "The final sink owns the final completion contract check. For a meaningful intermediate",
        "phase boundary, add a reviewer node after every phase deliverable and attach the phase",
        "Gate to that reviewer node. For a branch boundary without a review, use set_node_gate on",
        "an existing work or merge node. Do not",
        "add a Gate for every small task, and never attach one to a fork or evaluator",
        "node.",
        "A reviewer node inspects all dependency Artifacts and prepared code without modifying",
        "them, returns evidence plus a pass-or-revise recommendation, and never decides its Gate.",
        "A node or final Gate may require a command argv, a human_question, or both. A",
        "human_question states the exact judgment the user must make; the Planner and Workers",
        "must not decide it automatically or encode human judgment as a command.",
        "When command:exit-zero is selected, call set_final_gate with the exact behavioral argv",
        "sequence. If host_check_configuration already contains an explicit command argv, preserve",
        "that existing condition; a final_gate_argv adds the command:exit-zero condition rather",
        "than replacing an existing artifact or other condition.",
        "In particular, an existing artifact condition such as artifact:non-empty is retained",
        "when final_gate_argv causes command:exit-zero to be added.",
        "When a human-only final condition is required, call set_final_gate with argv=[] and a",
        "clear human_question. When both are present, both conditions are required.",
        "An explicit human:<question> completion criterion is a user-required human Check; keep",
        "it and do not replace it with an automatic artifact or command Check.",
        "If the discussion input has no explicit completion criteria, propose a non-empty final",
        "Gate using an exact command or human_question; never add artifact:non-empty as a",
        "placeholder for an unspecified condition.",
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


_DISCUSSION_SYSTEM_PROMPT = "\n".join(
    (
        _PLANNER_SYSTEM_PROMPT,
        "When planner_input.base_checks is present, it is the exact CheckSpec snapshot "
        "associated with planner_input.base_plan_revision. The base may be an unapproved "
        "draft, so it is historical context, not approval or authorization.",
        "Use base_checks to map each existing condition to its original node owner while "
        "discussing the revision. A newly compiled PlanRevision may receive new Check IDs; "
        "do not assume IDs will be reused. When the user asks to retain a condition, carry "
        "its command argv, human description, and semantic terms; an explicit request to "
        "change a condition is a proposed new condition requiring approval. Never treat "
        "command_argv as Tool permission.",
        "base_checks and host_check_configuration are separate facts: do not replace the "
        "stored base snapshot with host defaults.",
        "When replan_context accompanies a discussion, it describes a stopped source Run. "
        "Its approved_design_document and approved_checks are the original approval; "
        "base_plan_revision/base_checks may instead describe the source's current execution "
        "graph or the latest unapproved draft. A current execution graph is not a replacement "
        "approval. Keep these distinct. The source Run owns its original contract and results.",
        "Use source failures, pending questions and prior replies to discuss a proposed "
        "revision. Do not declare interventions resolved, transfer results, reuse old Gate "
        "verdicts, or claim a successor Run was started. Drafting does none of those things. "
        "Explain any changed approved boundary; an internal implementation adjustment alone "
        "belongs to the existing process-adjustment path, not a new approval boundary.",
    )
)


class PiPlannerError(RuntimeError):
    """Raised when the Pi Planner cannot produce a valid proposal."""


_PROCESS_PLANNER_SYSTEM_PROMPT = "\n".join(
    (
        "You are the EHAI process Planner. Draft an adjustment, never apply or approve it.",
        "The supplied original approved plan fixes requirements, interfaces, Gate conditions",
        "and permissions. Keep them intact; the current process is not a replacement approval.",
        "The graph tools are seeded with the current process. Inspect it first. Existing UUID",
        "keys identify reusable tasks, branches and phases; use new local keys for new tasks.",
        "A seeded task key is a draft editing reference, NOT a promise to retain that task's",
        "execution ID. After finish_plan the host gives a fresh ID to a changed task and to",
        "downstream tasks whose input identities change, even if their instructions do not.",
        "Only unchanged definitions and input scopes may retain task IDs and completion.",
        "Recompiled edges and Phase membership use the resulting IDs. Original logical Gate",
        "identity and frozen Checks remain fixed, while gate_owners maps to the compiled owner.",
        "Describe remaining work by task title/role and dependency relationships. Label source",
        "UUIDs as historical/draft keys when needed; never claim changed tasks, affected owners",
        "or edges will preserve their execution IDs, and never guess the host's new UUIDs.",
        "Edit tasks, dependencies and implementation routes only within the original boundary.",
        "Use the supplied intervention history, including user replies and originating Attempt",
        "identities. Replies provide continuation facts, not new permission or changed standards.",
        "Do not silently discard open questions, declare unknown external effects resolved, or",
        "use another route to bypass a required human decision. Carry relevant replies into",
        "replacement task instructions so the new Worker receives the needed context.",
        "For every change explain which original obligation it implements, its inputs/outputs,",
        "permission scope, evidence and Gate coverage in set_plan_design. Keep unchanged work.",
        "Use move_process_gate only to move a complete original Gate to its new owner; never",
        "change, drop, split or replace frozen Checks. The final Gate must remain at the sink.",
        "Reviewer nodes inspect prepared dependency code without modifying it. Phases retain",
        "their original Reviewer Gate boundaries; rework targets identify implementation work.",
        "Branch alternatives are real candidate work; Evaluators compare them, the host",
        "selects and prepares code inputs. Do not ask Workers to dispatch Agents themselves.",
        "Use the supplied read-only workspace tools to investigate; never execute work.",
        "Task instructions must identify files/modules, concrete steps, interfaces, required",
        "capabilities, session policy, outputs, dependency inputs and authorized local choices.",
        "Call set_plan_design with the complete updated design before finish_plan. Explain",
        "any missing evidence or boundary gap honestly; your draft is not evidence of safety.",
        "No task or branch state supplied by you can establish completion. The host decides",
        "identity and result reuse, then independently reviews the proposal before applying it.",
    )
)


class PiPlannerAdapter:
    """Role harness producing a validated PlanTemplate through native Pi."""

    def __init__(
        self,
        *,
        model: str,
        backend: PiBackendConfig,
        reasoning_effort: str | None = None,
        budget: ExplorationBudget | None = None,
        session_store: AgentTraceStore | None = None,
        workspace: Path | None = None,
        artifact_store: ArtifactStore | None = None,
        check_configuration: Mapping[str, JsonValue] | None = None,
        id_factory: Callable[[], ID] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("PiPlannerAdapter model must not be blank")
        self._budget = budget or _DEFAULT_PLANNER_BUDGET
        if not isinstance(self._budget, ExplorationBudget):
            raise TypeError("budget must be an ExplorationBudget")
        self._profile = WorkerProfile(
            "pi-planner",
            WorkerKind.PI,
            model,
            frozenset({WorkerCapability("planner.pi")}),
        )
        self._reasoning_effort = reasoning_effort
        self._agent_runtime = PiRoleRunner(
            session_store or MemoryAgentTraceStore(),
            backend=backend,
            state_root=backend.agent_dir / "ehai-sessions",
        )
        self._last_session: AgentTrace | None = None
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
    def last_session(self) -> AgentTrace | None:
        return self._last_session

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        """Ask Pi to propose a plan and validate it through the host builders."""
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
        normalized_criteria = require_p1_criteria(criteria, "PiPlannerAdapter")
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
                require_final_gate=_requires_final_gate(normalized_criteria),
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
        *,
        checks: tuple[CheckSpec, ...] = (),
        source_context: ReplanContext | None = None,
        session_ref_id: ID | None = None,
    ) -> PlanningReply:
        self._require_goal(goal, allow_completion_contract=True)
        normalized = normalize_discussion_criteria(criteria, "Planning discussion")
        document = build_codex_planner_input(
            goal,
            normalized,
            self._budget,
            base,
            discussion=True,
            checks=checks,
            context=source_context,
        )
        document["discussion_history"] = [dict(item) for item in history]
        document["user_message"] = message
        document["current_design_document"] = None if base is None else base.design_document
        template, answer, session_id = asyncio.run(
            self._run_planner(
                document,
                discussion=True,
                require_final_gate=(True if not normalized else _requires_final_gate(normalized)),
                session_ref_id=session_ref_id,
            )
        )
        proposal_criteria = (
            normalized if template is None else discussion_proposal_criteria(normalized, template)
        )
        proposal = (
            None
            if template is None
            else build_plan_proposal(
                goal, proposal_criteria, template, id_factory=self._id_factory, clock=self._clock
            )
        )
        return PlanningReply(answer, session_id, proposal)

    def propose_process(
        self,
        goal: Goal,
        approved: PlanRevision,
        previous: ProcessRevision,
        current: PlanRevision,
        checks: tuple[CheckSpec, ...],
        reason: str,
        *,
        session_ref_id: ID | None = None,
        intervention_context: tuple[dict[str, JsonValue], ...] = (),
    ) -> ProcessRevision:
        """Run the async process draft entry point from a synchronous caller."""
        return asyncio.run(
            self.propose_process_async(
                goal,
                approved,
                previous,
                current,
                checks,
                reason,
                session_ref_id=session_ref_id,
                intervention_context=intervention_context,
            )
        )

    async def propose_process_async(
        self,
        goal: Goal,
        approved: PlanRevision,
        previous: ProcessRevision,
        current: PlanRevision,
        checks: tuple[CheckSpec, ...],
        reason: str,
        *,
        session_ref_id: ID | None = None,
        intervention_context: tuple[dict[str, JsonValue], ...] = (),
    ) -> ProcessRevision:
        """Draft only; the caller still owns durable review, authorization and publication."""
        self._require_goal(goal, allow_completion_contract=True)
        if (
            approved.goal_id != goal.goal_id
            or process_approval_identity(approved) != process_approval_identity(previous.graph)
            or process_approval_identity(current) != process_approval_identity(previous.graph)
            or process_graph_definition(current) != process_graph_definition(previous.graph)
        ):
            raise ValueError("Process drafting requires the original approval and active graph")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason.encode("utf-8")) > 16_000
        ):
            raise ValueError("Process adjustment requires a reason within 16000 UTF-8 bytes")
        required_ids = {check_id for node in approved.nodes for check_id in node.required_check_ids}
        if {check.check_id for check in checks} != required_ids or len(checks) != len(required_ids):
            raise ValueError("Process drafting requires every frozen Check exactly once")
        contract = goal.completion_contract
        if contract is None or (
            contract.completion_contract_id,
            contract.version,
        ) != (approved.completion_contract_id, approved.completion_contract_version):
            raise ValueError("Process drafting requires the original completion contract")
        document = build_codex_planner_input(goal, contract.criteria, self._budget, approved)
        document["operation"] = "propose_process"
        document["original_approved_plan"] = document.pop("base_plan_revision")
        document["current_process_plan"] = build_codex_planner_input(
            goal, contract.criteria, self._budget, current
        )["base_plan_revision"]
        document["parent_process_revision_id"] = previous.process_revision_id
        document["reason"] = reason.strip()
        document["interventions"] = [dict(item) for item in intervention_context]
        document["frozen_checks"] = [
            {
                "check_id": check.check_id,
                "name": check.name,
                "kind": check.kind.value,
                "description": check.description,
                "required": check.required,
                "command_argv": list(check.command_argv),
                "semantic_required_terms": list(check.semantic_required_terms),
            }
            for check in checks
        ]
        graph = PlanGraphToolRuntime.from_process(
            previous, current, self._budget, planner_event_types=("planner.pi.completed",)
        )
        template, _, _ = await self._run_planner(
            document,
            discussion=False,
            require_final_gate=True,
            process_graph=graph,
            session_ref_id=session_ref_id,
        )
        if template is None:
            raise PiPlannerError("Process proposal finished without a graph")
        return build_process_proposal(
            previous, current, template, reason, id_factory=self._id_factory, clock=self._clock
        )

    def review_process(
        self, context: ProcessReviewContext, *, session_ref_id: ID
    ) -> ProcessBoundaryReport:
        """Run the async process review entry point from a synchronous caller."""
        return asyncio.run(self.review_process_async(context, session_ref_id=session_ref_id))

    async def review_process_async(
        self, context: ProcessReviewContext, *, session_ref_id: ID
    ) -> ProcessBoundaryReport:
        """Use a fresh Reviewer session, with no Planner mutation tools or proposal transcript."""
        from ehai.infrastructure.planners.process_review import run_process_boundary_review

        if self._artifact_store is None:
            raise PiPlannerError("Process review requires the retained Artifact store")
        return await run_process_boundary_review(
            context,
            runtime=self._agent_runtime,
            model=self._profile.model,
            reasoning_effort=self._reasoning_effort,
            session_ref_id=session_ref_id,
            artifact_store=self._artifact_store,
            workspace=self._workspace,
        )

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
            raise PiPlannerError("Proposal finished without a plan")
        return template

    async def _run_planner(
        self,
        input_document: Mapping[str, JsonValue],
        *,
        discussion: bool,
        require_final_gate: bool,
        process_graph: PlanGraphToolRuntime | None = None,
        session_ref_id: ID | None = None,
    ) -> tuple[PlanTemplate | None, str, ID]:
        graph = process_graph or PlanGraphToolRuntime(
            self._budget,
            planner_event_types=("planner.pi.completed",),
            require_final_gate=require_final_gate,
        )
        design: dict[str, str] = {}
        answer: dict[str, str] = {}
        workspace_tools = (
            None
            if self._workspace is None or self._artifact_store is None
            else HostToolRuntime(
                workspace=self._workspace,
                artifact_store=self._artifact_store,
                allow_workspace_write=False,
            )
        )
        registry, names = _planner_registry(graph, design, answer, workspace_tools, discussion)
        config = AgentRoleConfig(
            role=AgentRole.PLANNER,
            system_prompt=(
                _PROCESS_PLANNER_SYSTEM_PROMPT
                if process_graph is not None
                else _DISCUSSION_SYSTEM_PROMPT
                if discussion
                else _PLANNER_SYSTEM_PROMPT
            ),
            tool_profile=(
                "planner-process-v1"
                if process_graph is not None
                else "planner-discussion-v1"
                if discussion
                else "planner-design-v1"
            ),
            tool_names=names,
            finish_tool="finish_plan",
            permissions=frozenset(
                {"plan.read", "plan.write"}
                | ({"workspace.read"} if workspace_tools is not None else set())
            ),
            final_tool_requires_only=False,
        )
        session = self._agent_runtime.create_session(session_ref_id)
        self._last_session = session
        try:
            await self._agent_runtime.run(
                config=config,
                registry=registry,
                model=self._profile.model,
                reasoning_effort=self._reasoning_effort,
                session=session,
                workspace=self._workspace or Path.cwd(),
                execution=AgentRoleExecution(session.agent_session_ref_id),
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
                    "host_check_configuration": (
                        self._check_configuration if process_graph is None else {}
                    ),
                },
            )
        finally:
            if workspace_tools is not None:
                await workspace_tools.aclose()
        if "message" in answer:
            return None, answer["message"], session.agent_session_ref_id
        if not graph.finished:
            raise PiPlannerError(
                "Pi Planner response contained no ToolCalls; the plan must be built "
                "with the graph tools and submitted through finish_plan"
            )
        document = "\n\n".join(f"## {name}\n\n{text}" for name, text in design.items())
        return (
            replace(graph.build_template(), design_document=document),
            (
                "Draft ready. Review its design and checks; explicit approval is required."
                if process_graph is None
                else "Process draft ready; boundary/evidence review is required before applying."
            ),
            session.agent_session_ref_id,
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
    workspace_tools: HostToolRuntime | None,
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
                raise PiPlannerError(
                    "Pi Planner validation budget exhausted: finish_plan failed "
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
        read_tools: HostToolRuntime = workspace_tools
        for definition in workspace_tools.tool_set.definitions:
            if definition.name not in {"workspace_list", "workspace_read", "workspace_search"}:
                continue
            definitions.append(definition)

            async def read_workspace(
                arguments: dict[str, JsonValue],
                cancellation: CancellationToken,
                *,
                name: str = definition.name,
                tools: HostToolRuntime = read_tools,
            ) -> JsonValue:
                return await tools.executor.execute(
                    ToolCall(str(new_id()), name, arguments), cancellation
                )

            handlers[definition.name] = read_workspace
    return ToolRegistry(tuple(definitions), handlers), tuple(item.name for item in definitions)
