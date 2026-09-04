"""Codex-backed Planner adapter with an independent structured protocol."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from ehai import ID, new_id, utc_now
from ehai.application.planner import (
    ExplorationBudget,
    ExplorationUsage,
    PlanProposal,
    ReplanContext,
    build_plan_proposal,
    replan_from_template,
    require_p1_criteria,
    require_replan_context,
    two_branch_plan_template,
)
from ehai.domain.goal import Goal, GoalStatus
from ehai.domain.planning import PlanRevision
from ehai.infrastructure.codex_transport import (
    CodexCancellation,
    CodexProcessOutcome,
    CodexProcessTransport,
    bounded_codex_text,
    read_codex_final,
    redact_codex_text,
    sanitize_codex_output,
)
from ehai.infrastructure.planners.codex_protocol import (
    CodexPlannerProtocolError,
    ParsedCodexPlan,
    build_codex_planner_input,
    build_codex_planner_prompt,
    codex_planner_output_schema_json,
    map_codex_planner_event_types,
    parse_codex_planner_result,
)

_P1_USAGE = ExplorationUsage(width=2, depth=1, attempts=5)


class CodexPlannerError(RuntimeError):
    """Raised when Codex cannot return a valid Planner proposal."""


class CodexPlannerTimedOutError(CodexPlannerError):
    """Raised when a Codex Planner invocation exceeds its own deadline."""


class CodexPlannerAdapter(CodexProcessTransport):
    """Propose a bounded graph without acquiring Worker responsibilities."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        executable: str | Path | Sequence[str] = "codex",
        sandbox: str = "read-only",
        timeout_seconds: float = 120.0,
        cancel_grace_seconds: float = 2.0,
        max_output_bytes: int = 1_048_576,
        env_overrides: Mapping[str, str] | None = None,
        budget: ExplorationBudget | None = None,
        id_factory: Callable[[], ID] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(
            workspace=workspace,
            executable=executable,
            sandbox=sandbox,
            timeout_seconds=timeout_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
            max_output_bytes=max_output_bytes,
            env_overrides=env_overrides,
        )
        self._budget = budget or ExplorationBudget(max_attempts=5)
        if not isinstance(self._budget, ExplorationBudget):
            raise TypeError("budget must be an ExplorationBudget")
        self._id_factory = id_factory
        self._clock = clock

    def propose(self, goal: Goal, criteria: tuple[str, ...]) -> PlanProposal:
        """Ask a fresh Codex process for one unapproved exploration proposal."""
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
        normalized_criteria = self._criteria(criteria)
        _P1_USAGE.require_within(self._budget)
        input_document = build_codex_planner_input(
            goal,
            normalized_criteria,
            self._budget,
            base,
            context,
        )
        prompt = build_codex_planner_prompt(input_document).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="ehai-codex-planner-") as temporary:
            temporary_path = Path(temporary)
            schema_path = temporary_path / "output-schema.json"
            last_message_path = temporary_path / "last-message.json"
            schema_path.write_text(codex_planner_output_schema_json(), encoding="utf-8")
            command = self._command(schema_path, last_message_path)
            try:
                outcome = asyncio.run(self._execute_async(command, prompt, CodexCancellation()))
            except OSError as error:
                detail = redact_codex_text(str(error), self._secret_values)
                raise CodexPlannerError(
                    f"Goal {goal.goal_id} Codex Planner process could not start: "
                    f"{type(error).__name__}: {detail}"
                ) from error
            parsed, diagnostics = self._interpret(goal.goal_id, last_message_path, outcome)
        provider_event_types = tuple(
            bounded_codex_text(redact_codex_text(item, self._secret_values), 100)
            for item in outcome.event_types
        )
        event_types = map_codex_planner_event_types(provider_event_types)
        return self._build_proposal(
            goal,
            normalized_criteria,
            parsed,
            diagnostics,
            event_types,
        )

    def _interpret(
        self,
        goal_id: ID,
        last_message_path: Path,
        outcome: CodexProcessOutcome,
    ) -> tuple[ParsedCodexPlan, tuple[str, ...]]:
        stdout = sanitize_codex_output(outcome.stdout, self._max_output_bytes, self._secret_values)
        stderr = sanitize_codex_output(outcome.stderr, self._max_output_bytes, self._secret_values)
        diagnostics = tuple(
            bounded_codex_text(redact_codex_text(item, self._secret_values), 500)
            for item in outcome.diagnostics
        )
        if outcome.timed_out:
            raise CodexPlannerTimedOutError(
                f"Goal {goal_id} Codex Planner timed out after {self._timeout_seconds:g} seconds"
            )
        if outcome.cancelled:
            raise CodexPlannerError(f"Goal {goal_id} Codex Planner was cancelled")
        if outcome.communication_error is not None:
            raise CodexPlannerError(f"Goal {goal_id}: {outcome.communication_error}")
        if outcome.return_code != 0:
            detail = bounded_codex_text(stderr.decode("utf-8", errors="replace").strip(), 300)
            suffix = f": {detail}" if detail else ""
            raise CodexPlannerError(
                f"Goal {goal_id} Codex Planner exited with code {outcome.return_code}{suffix}"
            )
        try:
            final_text = read_codex_final(last_message_path, self._max_output_bytes)
            parsed = parse_codex_planner_result(final_text)
        except (OSError, UnicodeError, ValueError, CodexPlannerProtocolError) as error:
            detail = redact_codex_text(str(error), self._secret_values)
            raise CodexPlannerError(
                f"Goal {goal_id} Codex Planner returned an invalid structured result: {detail}"
            ) from error
        if stderr:
            diagnostics += (
                "codex planner stderr: "
                + bounded_codex_text(stderr.decode("utf-8", errors="replace").strip(), 300),
            )
        if stdout and not outcome.event_types:
            diagnostics += ("codex planner produced stdout without recognized JSONL events",)
        return parsed, diagnostics

    def _build_proposal(
        self,
        goal: Goal,
        criteria: tuple[str, ...],
        parsed: ParsedCodexPlan,
        diagnostics: tuple[str, ...],
        event_types: tuple[str, ...],
    ) -> PlanProposal:
        return build_plan_proposal(
            goal,
            criteria,
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
                planner_diagnostics=diagnostics,
                planner_event_types=event_types,
            ),
            id_factory=self._id_factory,
            clock=self._clock,
        )

    @staticmethod
    def _require_goal(goal: Goal, *, allow_completion_contract: bool) -> None:
        if not isinstance(goal, Goal):
            raise TypeError("goal must be a Goal")
        if goal.status is not GoalStatus.OPEN:
            raise ValueError(f"Goal {goal.goal_id} must be open before planning")
        if not allow_completion_contract and goal.completion_contract is not None:
            raise ValueError(f"Goal {goal.goal_id} already has a CompletionContract")

    @staticmethod
    def _criteria(criteria: tuple[str, ...]) -> tuple[str, ...]:
        return require_p1_criteria(criteria, "CodexPlannerAdapter")
