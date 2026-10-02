"""Create Projects and Goals; propose, import, discuss, replan and approve plans."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ehai import ID, JsonValue, json_dumps, json_loads, normalize_id
from ehai.application.commands import (
    ApprovePlan,
    CreateGoal,
    CreateProject,
    DiscussPlan,
    ImportPlan,
    ProposePlan,
    ReplanPlan,
)
from ehai.application.execution_service.common import (
    ApplicationError,
    EntityNotFoundError,
    _build_replan_context,
    _capture_discussion_source,
    _DiscussionSource,
    _require_no_other_active_runs,
    _required_contract,
    _required_contract_for_plan,
    _required_execution_plan,
    _required_goal,
    _required_plan,
    _required_run,
    _result_id,
    _source_discussion_base,
    _validate_source_discussion_run,
)
from ehai.application.execution_service.host import ExecutionServiceHost
from ehai.application.interventions import list_interventions
from ehai.application.planner import (
    ConversationalPlanner,
    PlanProposal,
    build_plan_proposal,
    discussion_proposal_criteria,
    require_p1_criteria,
)
from ehai.application.planner_capacity import (
    planning_operation,
)
from ehai.application.planning_dialogue import PlanningConversationView, planning_conversation
from ehai.application.ports import UnitOfWork
from ehai.application.sanitization import bounded_redacted_text, redact_sensitive_text
from ehai.application.successions import (
    approve_succession,
    approved_succession,
)
from ehai.domain.checking import CheckRunStatus, CheckSpec
from ehai.domain.events import EventType
from ehai.domain.execution import AttemptStatus, RunStatus
from ehai.domain.goal import CompletionContract, Goal, GoalStatus, Project
from ehai.domain.planning import PlanRevision, PlanRevisionStatus


class PlanningCommandsMixin:
    """Create Projects and Goals; propose, import, discuss, replan and approve plans."""

    def create_project(self: ExecutionServiceHost, command: CreateProject) -> Project:
        """Create a Project and its immutable fact Event atomically."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_project(uow, _result_id(existing, "project_id"))

            project = Project.create(
                command.name,
                project_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_project(project)
            uow.events.append(
                self._event(
                    EventType.PROJECT_CREATED,
                    project.project_id,
                    {"project_id": project.project_id},
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"project_id": project.project_id},
            )
            uow.commit()
            return project

    def create_goal(self: ExecutionServiceHost, command: CreateGoal) -> Goal:
        """Create an open Goal without inventing completion criteria."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_goal(uow, _result_id(existing, "goal_id"))
            _required_project(uow, command.project_id)
            goal = Goal.create(
                command.project_id,
                command.objective,
                goal_id=self._id_factory(),
                created_at=self._clock(),
            )
            uow.states.put_goal(goal)
            uow.events.append(
                self._event(
                    EventType.GOAL_CREATED,
                    goal.goal_id,
                    {"goal_id": goal.goal_id, "project_id": goal.project_id},
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"goal_id": goal.goal_id},
            )
            uow.commit()
            return goal

    @planning_operation
    def propose_plan(self: ExecutionServiceHost, command: ProposePlan) -> PlanRevision:
        """Ask the Planner for an unapproved contract and graph proposal."""
        return self._create_plan_proposal(
            command, lambda goal: self._planner.propose(goal, command.criteria)
        )

    def import_plan(self: ExecutionServiceHost, command: ImportPlan) -> PlanRevision:
        """Import an initial draft for an unplanned Goal without invoking a model."""
        from ehai.application.plan_imports import import_plan_template

        def produce(goal: Goal) -> PlanProposal:
            if goal.status is not GoalStatus.OPEN or goal.completion_contract is not None:
                raise ApplicationError(
                    "Import requires an open, unplanned Goal; "
                    "use revision workflows for existing plans"
                )
            value = json_loads(command.plan_json)
            assert isinstance(value, dict)
            template = import_plan_template(value)
            return build_plan_proposal(
                goal,
                discussion_proposal_criteria((), template),
                template,
                id_factory=self._id_factory,
                clock=self._clock,
            )

        return self._create_plan_proposal(command, produce)

    def _create_plan_proposal(
        self: ExecutionServiceHost,
        command: ProposePlan | ImportPlan,
        produce: Callable[[Goal], PlanProposal],
    ) -> PlanRevision:
        """One idempotent, rechecked persistence path for both proposal sources."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            goal = _required_goal(uow, command.goal_id)
            _require_no_active_runs(uow, goal.goal_id)

        proposal = produce(goal)
        aligned_goal = goal.use_completion_contract(proposal.contract)
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            current_goal = _required_goal(uow, command.goal_id)
            _require_no_active_runs(uow, goal.goal_id)
            if current_goal != goal:
                raise ApplicationError(f"Goal {goal.goal_id} changed while planning")
            uow.states.put_completion_contract(proposal.contract)
            uow.states.put_goal(aligned_goal)
            uow.states.put_plan_revision(proposal.plan_revision)
            for check_spec in proposal.check_specs:
                uow.states.put_check_spec(proposal.plan_revision.plan_revision_id, check_spec)
            uow.events.append(
                self._event(
                    EventType.PLAN_REVISION_PROPOSED,
                    proposal.plan_revision.plan_revision_id,
                    {
                        "completion_contract_id": proposal.contract.completion_contract_id,
                        "goal_id": goal.goal_id,
                        "plan_revision_id": proposal.plan_revision.plan_revision_id,
                        "planner_diagnostics": list(proposal.planner_diagnostics),
                        "planner_event_types": list(proposal.planner_event_types),
                    },
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {
                    "completion_contract_id": proposal.contract.completion_contract_id,
                    "plan_revision_id": proposal.plan_revision.plan_revision_id,
                },
            )
            uow.commit()
            return proposal.plan_revision

    @planning_operation
    def discuss_plan(self: ExecutionServiceHost, command: DiscussPlan) -> PlanningConversationView:
        requested_criteria = (
            require_p1_criteria(command.criteria, "Planning discussion") if command.criteria else ()
        )
        if not isinstance(self._planner, ConversationalPlanner):
            raise ApplicationError("Configured Planner does not support discussion")
        with self._uow_factory() as uow:
            receipt = self._existing_result(
                uow, command.idempotency_key, type(command).__name__, command.fingerprint
            )
            if receipt is not None:
                conversation_id = _result_id(receipt, "conversation_id")
                view = _required_conversation(uow, conversation_id)
                turn_id = _result_id(receipt, "turn_id")
                turn = next(item for item in view.turns if item.turn_id == turn_id)
                if turn.status != "completed":
                    raise ApplicationError(
                        f"Planning turn {turn_id} is {turn.status}; inspect conversation "
                        f"{conversation_id}. This idempotency key will not repeat a model call."
                    )
                return view
            goal = _required_goal(uow, command.goal_id)
            if goal.status is not GoalStatus.OPEN:
                raise ApplicationError("Only an open Goal can be discussed")
            conversation_id = command.conversation_id or self._id_factory()
            previous = (
                None
                if command.conversation_id is None
                else _required_conversation(uow, conversation_id)
            )
            if previous is not None:
                if (
                    previous.goal_id != goal.goal_id
                    or previous.workspace != self._planning_workspace
                ):
                    raise ApplicationError("Planning conversation Goal or workspace does not match")
                if any(turn.status == "running" for turn in previous.turns):
                    raise ApplicationError("The previous planning turn has an unresolved outcome")
                if len(previous.turns) >= 24:
                    raise ApplicationError("Start a new discussion using the retained current plan")

            source_run_id = command.source_run_id
            if previous is not None and previous.source_run_id is not None:
                if source_run_id is not None and source_run_id != previous.source_run_id:
                    raise ApplicationError(
                        "A planning conversation cannot be rebound to another source Run"
                    )
                source_run_id = previous.source_run_id

            source: _DiscussionSource | None = None
            if source_run_id is None:
                _require_no_active_runs(uow, goal.goal_id)
                plans = uow.states.list_plan_revisions(goal.goal_id)
                base = plans[-1] if plans else None
            else:
                source_run = _required_run(uow, source_run_id)
                approved_source = _validate_source_discussion_run(uow, goal, source_run)
                _require_no_other_active_runs(uow, goal.goal_id, source_run.run_id)
                base = _source_discussion_base(uow, approved_source)
                _validate_source_discussion_base(uow, approved_source, base)
                source = _capture_discussion_source(
                    uow,
                    source_run=source_run,
                    approved_plan=approved_source,
                )
            planner_base = (
                source.execution_plan
                if source is not None and base == source.approved_plan
                else base
            )
            checks = () if planner_base is None else _base_check_specs(uow, planner_base)
            if source is not None and not requested_criteria:
                assert base is not None
                discussion_criteria = require_p1_criteria(
                    _required_contract_for_plan(uow, base).criteria, "Source planning discussion"
                )
            else:
                discussion_criteria = _resolve_discussion_criteria(
                    uow,
                    conversation_id=conversation_id,
                    requested=requested_criteria,
                    base=base,
                )
            history: tuple[dict[str, JsonValue], ...] = (
                ()
                if previous is None
                else tuple(
                    {"user": turn.message, "planner": turn.reply, "error": turn.error}
                    for turn in previous.turns
                )
            )
            if len(json_dumps(list(history)).encode("utf-8")) > 64_000:
                raise ApplicationError("Discussion context is full; start a new conversation")
            turn_id = self._id_factory()
            planner_session_id = self._id_factory()
            started_payload: dict[str, JsonValue] = {
                "turn_id": turn_id,
                "agent_session_ref_id": planner_session_id,
                "goal_id": goal.goal_id,
                "workspace": self._planning_workspace,
                "message": command.message,
                "criteria": list(command.criteria),
                "base_plan_revision_id": None if base is None else base.plan_revision_id,
            }
            if source is not None:
                started_payload.update(
                    {
                        "source_run_id": source.run.run_id,
                        "source_process_revision_id": source.process_revision.process_revision_id,
                        "source_approved_plan_revision_id": source.approved_plan.plan_revision_id,
                    }
                )
            uow.events.append(
                self._event(
                    EventType.PLANNING_TURN_STARTED,
                    conversation_id,
                    started_payload,
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"conversation_id": conversation_id, "turn_id": turn_id},
            )
            uow.commit()
        try:
            if source is not None:
                reply = self._planner.discuss(
                    goal,
                    discussion_criteria,
                    command.message,
                    history,
                    planner_base,
                    checks=checks,
                    source_context=source.context,
                    session_ref_id=planner_session_id,
                )
            elif checks:
                reply = self._planner.discuss(
                    goal,
                    discussion_criteria,
                    command.message,
                    history,
                    base,
                    checks=checks,
                    session_ref_id=planner_session_id,
                )
            else:
                reply = self._planner.discuss(
                    goal,
                    discussion_criteria,
                    command.message,
                    history,
                    base,
                    session_ref_id=planner_session_id,
                )
            if reply.agent_session_ref_id != planner_session_id:
                raise ApplicationError("Planner returned a different discussion Session identity")
            with self._uow_factory() as uow:
                if source is None:
                    if _required_goal(uow, goal.goal_id) != goal:
                        raise ApplicationError(
                            "Goal or approval changed while the Planner was responding"
                        )
                    _require_no_active_runs(uow, goal.goal_id)
                    current_plans = uow.states.list_plan_revisions(goal.goal_id)
                    if (current_plans[-1] if current_plans else None) != base:
                        raise ApplicationError("Plan changed while the Planner was responding")
                else:
                    assert base is not None
                    assert planner_base is not None
                    _recheck_discussion_source(
                        uow,
                        goal=goal,
                        base=base,
                        source=source,
                    )
                    if _base_check_specs(uow, planner_base) != checks:
                        raise ApplicationError("Discussion Check configuration changed")
                proposal = reply.proposal
                if proposal is not None:
                    previous_contract = None
                    if source is not None:
                        assert base is not None
                        previous_contract = _required_contract_for_plan(uow, base)
                    proposal = _discussion_revision(
                        goal,
                        base,
                        proposal,
                        previous_contract=previous_contract,
                    )
                    uow.states.put_completion_contract(proposal.contract)
                    if source is None:
                        uow.states.put_goal(goal.use_completion_contract(proposal.contract))
                    uow.states.put_plan_revision(proposal.plan_revision)
                    for spec in proposal.check_specs:
                        uow.states.put_check_spec(proposal.plan_revision.plan_revision_id, spec)
                    proposal_payload: dict[str, JsonValue] = {
                        "goal_id": goal.goal_id,
                        "plan_revision_id": proposal.plan_revision.plan_revision_id,
                        "completion_contract_id": proposal.contract.completion_contract_id,
                        "conversation_id": conversation_id,
                    }
                    if source is not None:
                        proposal_payload.update(
                            {
                                "source_run_id": source.run.run_id,
                                "source_process_revision_id": (
                                    source.process_revision.process_revision_id
                                ),
                                "source_approved_plan_revision_id": (
                                    source.approved_plan.plan_revision_id
                                ),
                            }
                        )
                    uow.events.append(
                        self._event(
                            EventType.PLAN_REVISION_PROPOSED,
                            proposal.plan_revision.plan_revision_id,
                            proposal_payload,
                        )
                    )
                uow.events.append(
                    self._event(
                        EventType.PLANNING_TURN_COMPLETED,
                        conversation_id,
                        {
                            "turn_id": turn_id,
                            "reply": redact_sensitive_text(reply.text),
                            "agent_session_ref_id": reply.agent_session_ref_id,
                            "plan_revision_id": (
                                None
                                if proposal is None
                                else proposal.plan_revision.plan_revision_id
                            ),
                        },
                    )
                )
                result = _required_conversation(uow, conversation_id)
                uow.commit()
                return result
        except Exception as error:
            with self._uow_factory() as uow:
                uow.events.append(
                    self._event(
                        EventType.PLANNING_TURN_FAILED,
                        conversation_id,
                        {
                            "turn_id": turn_id,
                            "error": bounded_redacted_text(str(error), max_bytes=2000),
                        },
                    )
                )
                uow.commit()
            raise ApplicationError(
                f"Planning turn failed; inspect conversation {conversation_id}: "
                f"{bounded_redacted_text(str(error), max_bytes=2000)}"
            ) from error

    @planning_operation
    def replan_plan(self: ExecutionServiceHost, command: ReplanPlan) -> PlanRevision:
        """Create a new draft revision without mutating its approved base or history."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            base = _required_plan(uow, command.base_plan_revision_id)
            goal = _required_goal(uow, base.goal_id)
            source_run = (
                None if command.source_run_id is None else _required_run(uow, command.source_run_id)
            )
            if source_run is None or source_run.status is not RunStatus.PAUSED:
                _require_no_active_runs(uow, goal.goal_id)
            else:
                _require_available_replan_version(uow, base)
            context = (
                None
                if source_run is None
                else _build_replan_context(uow, source_run=source_run, base=base)
            )
            planning_base = (
                base if source_run is None else _required_execution_plan(uow, source_run.run_id)
            )
            source_interventions = (
                None if source_run is None else list_interventions(uow.events, source_run.run_id)
            )

        proposal = self._planner.replan(goal, planning_base, command.criteria, context)
        aligned_goal = (
            goal
            if source_run is not None and source_run.status is RunStatus.PAUSED
            else goal.use_completion_contract(proposal.contract)
        )
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            current_goal = _required_goal(uow, goal.goal_id)
            current_base = _required_plan(uow, base.plan_revision_id)
            if source_run is None or source_run.status is not RunStatus.PAUSED:
                _require_no_active_runs(uow, goal.goal_id)
            else:
                _require_available_replan_version(uow, current_base)
            current_context = (
                None
                if source_run is None
                else _build_replan_context(
                    uow,
                    source_run=_required_run(uow, source_run.run_id),
                    base=current_base,
                )
            )
            current_planning_base = (
                current_base
                if source_run is None
                else _required_execution_plan(uow, source_run.run_id)
            )
            if (
                current_goal != goal
                or current_base != base
                or current_context != context
                or current_planning_base != planning_base
                or (
                    source_run is not None
                    and list_interventions(uow.events, source_run.run_id) != source_interventions
                )
            ):
                raise ApplicationError(
                    f"Goal {goal.goal_id}, base PlanRevision {base.plan_revision_id}, or source "
                    "Run evidence changed while replanning"
                )
            uow.states.put_completion_contract(proposal.contract)
            uow.states.put_goal(aligned_goal)
            uow.states.put_plan_revision(proposal.plan_revision)
            for check_spec in proposal.check_specs:
                uow.states.put_check_spec(proposal.plan_revision.plan_revision_id, check_spec)
            uow.events.append(
                self._event(
                    EventType.PLAN_REVISION_PROPOSED,
                    proposal.plan_revision.plan_revision_id,
                    {
                        "completion_contract_id": proposal.contract.completion_contract_id,
                        "goal_id": goal.goal_id,
                        "plan_revision_id": proposal.plan_revision.plan_revision_id,
                        "planner_diagnostics": list(proposal.planner_diagnostics),
                        "planner_event_types": list(proposal.planner_event_types),
                        "replan_context": None if context is None else context.to_dict(),
                        "supersedes_plan_revision_id": base.plan_revision_id,
                    },
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {
                    "completion_contract_id": proposal.contract.completion_contract_id,
                    "plan_revision_id": proposal.plan_revision.plan_revision_id,
                },
            )
            uow.commit()
            return proposal.plan_revision

    def approve_plan(self: ExecutionServiceHost, command: ApprovePlan) -> PlanRevision:
        """Confirm the exact CompletionContract and approve its PlanRevision."""
        with self._uow_factory() as uow:
            existing = self._existing_result(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
            )
            if existing is not None:
                return _required_plan(uow, _result_id(existing, "plan_revision_id"))
            plan = _required_plan(uow, command.plan_revision_id)
            contract = _required_contract(uow, command.completion_contract_id)
            goal = _required_goal(uow, plan.goal_id)
            succession = approved_succession(uow, plan.plan_revision_id)
            if command.supersession_json is not None:
                if plan.status is PlanRevisionStatus.APPROVED:
                    raise ApplicationError("An existing approval's succession context is immutable")
                document = json_loads(command.supersession_json)
                if not isinstance(document, dict):
                    raise ValueError("supersession must be an object")
                succession = approve_succession(uow, plan, document)
            if any(
                item.status in {RunStatus.PENDING, RunStatus.RUNNING}
                or any(
                    attempt.status in {AttemptStatus.PENDING, AttemptStatus.RUNNING}
                    for attempt in uow.states.list_attempts(item.run_id)
                )
                or any(
                    check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                    and check.human_request is None
                    for check in uow.states.list_check_runs(item.run_id)
                )
                for item in uow.states.list_runs(goal.goal_id)
            ):
                raise ApplicationError(
                    "Stop active execution before approving a changed Goal contract"
                )
            for run in uow.states.list_runs(goal.goal_id):
                if run.status is not RunStatus.PAUSED:
                    continue
                if any(
                    item.predecessor_run_id == run.run_id
                    for item in uow.states.list_runs(goal.goal_id)
                ):
                    continue
                if succession is not None and run.run_id == succession["predecessor_run_id"]:
                    continue
                source_plan = _required_plan(uow, run.plan_revision_id)
                if (
                    source_plan.completion_contract_id == contract.completion_contract_id
                    and source_plan.completion_contract_version == contract.version
                ):
                    continue
                if any(
                    check.human_request is not None
                    and check.status in {CheckRunStatus.PENDING, CheckRunStatus.RUNNING}
                    for check in uow.states.list_check_runs(run.run_id)
                ) or any(
                    notice["status"] == "open"
                    for notice in list_interventions(uow.events, run.run_id)
                ):
                    raise ApplicationError(
                        f"Paused Run {run.run_id} has unresolved human Checks or interventions; "
                        "approving a different contract would invalidate their replies. "
                        "Provide explicit supersession dispositions with approval"
                    )
            confirmed = contract.confirm(confirmed_at=self._clock())
            aligned_goal = goal.use_completion_contract(confirmed)
            approved = plan.approve(confirmed, approved_at=self._clock())
            uow.states.put_completion_contract(confirmed)
            uow.states.put_goal(aligned_goal)
            uow.states.put_plan_revision(approved)
            uow.events.append(
                self._event(
                    EventType.COMPLETION_CONTRACT_CONFIRMED,
                    confirmed.completion_contract_id,
                    {
                        "completion_contract_id": confirmed.completion_contract_id,
                        "goal_id": confirmed.goal_id,
                    },
                )
            )
            uow.events.append(
                self._event(
                    EventType.PLAN_REVISION_APPROVED,
                    approved.plan_revision_id,
                    {"plan_revision_id": approved.plan_revision_id, "succession": succession},
                )
            )
            self._record_receipt(
                uow,
                command.idempotency_key,
                type(command).__name__,
                command.fingerprint,
                {"plan_revision_id": approved.plan_revision_id},
            )
            uow.commit()
            return approved


def _base_check_specs(uow: UnitOfWork, base: PlanRevision) -> tuple[CheckSpec, ...]:
    """Return only CheckSpecs referenced by this exact base PlanRevision."""
    required_ids = tuple(
        dict.fromkeys(check_id for node in base.nodes for check_id in node.required_check_ids)
    )
    specs_by_id = {
        spec.check_id: spec for spec in uow.states.list_check_specs(base.plan_revision_id)
    }
    missing = tuple(check_id for check_id in required_ids if check_id not in specs_by_id)
    if missing:
        raise ApplicationError(
            f"PlanRevision {base.plan_revision_id} is missing CheckSpecs: {', '.join(missing)}"
        )
    return tuple(specs_by_id[check_id] for check_id in required_ids)


def _required_conversation(uow: UnitOfWork, conversation_id: ID) -> PlanningConversationView:
    result = planning_conversation(uow.events.list_events(), conversation_id)
    if result is None:
        raise EntityNotFoundError(f"PlanningConversation {conversation_id} was not found")
    return result


def _resolve_discussion_criteria(
    uow: UnitOfWork,
    *,
    conversation_id: ID,
    requested: tuple[str, ...],
    base: PlanRevision | None,
) -> tuple[str, ...]:
    """Resolve omitted criteria from an approved baseline, then prior discussion input."""
    if requested:
        return requested

    if base is not None and base.status is PlanRevisionStatus.APPROVED:
        contract = uow.states.get_completion_contract(base.completion_contract_id)
        if contract is not None:
            return require_p1_criteria(tuple(contract.criteria), "Planning discussion")

    recent_explicit: tuple[str, ...] | None = None
    prior_approved_base: PlanRevision | None = None
    for stored in reversed(uow.events.list_events()):
        event = stored.event
        if (
            event.correlation_id != conversation_id
            or event.type is not EventType.PLANNING_TURN_STARTED
        ):
            continue
        raw_criteria = event.payload.get("criteria")
        if raw_criteria is not None:
            if not isinstance(raw_criteria, list):
                raise ApplicationError("Planning turn criteria history is invalid")
            criteria: list[str] = []
            for criterion in raw_criteria:
                if not isinstance(criterion, str):
                    raise ApplicationError("Planning turn criteria history is invalid")
                criteria.append(criterion)
            if criteria and recent_explicit is None:
                recent_explicit = require_p1_criteria(tuple(criteria), "Planning discussion")

        raw_base_id = event.payload.get("base_plan_revision_id")
        if isinstance(raw_base_id, str):
            try:
                prior_base = uow.states.get_plan_revision(normalize_id(raw_base_id))
            except ValueError as error:
                raise ApplicationError(
                    "Planning turn base PlanRevision history is invalid"
                ) from error
            if (
                prior_approved_base is None
                and prior_base is not None
                and prior_base.status is PlanRevisionStatus.APPROVED
            ):
                prior_approved_base = prior_base

    if recent_explicit is not None:
        return recent_explicit
    if prior_approved_base is not None:
        contract = uow.states.get_completion_contract(prior_approved_base.completion_contract_id)
        if contract is not None:
            return require_p1_criteria(tuple(contract.criteria), "Planning discussion")
    return ()


def _require_available_replan_version(uow: UnitOfWork, base: PlanRevision) -> None:
    """Do not regenerate an occupied next version while the approved base stays active."""
    successor = next(
        (
            plan
            for plan in uow.states.list_plan_revisions(base.goal_id)
            if plan.version > base.version
        ),
        None,
    )
    if successor is not None:
        raise ApplicationError(
            f"PlanRevision {successor.plan_revision_id} already follows this approved base; "
            "inspect the retained draft with get-plan. Further paused-source draft revision "
            "is not yet supported; the active contract and existing draft were preserved"
        )


def _require_no_active_runs(uow: UnitOfWork, goal_id: ID) -> None:
    if any(
        run.status in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.PAUSED}
        for run in uow.states.list_runs(goal_id)
    ):
        raise ApplicationError("An active Run must be resolved before revising its Goal")


def _discussion_revision(
    goal: Goal,
    base: PlanRevision | None,
    proposal: PlanProposal,
    *,
    previous_contract: CompletionContract | None = None,
) -> PlanProposal:
    if proposal.plan_revision.design_document is None:
        raise ApplicationError("A discussion proposal must include its reviewable design")
    if base is None:
        return proposal
    current = previous_contract or goal.completion_contract
    if current is None or current.completion_contract_id != base.completion_contract_id:
        raise ApplicationError("Current plan and Goal completion contract do not match")
    contract = current.revise(
        proposal.contract.criteria,
        proposal.contract.required_check_ids,
        completion_contract_id=proposal.contract.completion_contract_id,
        created_at=proposal.contract.created_at,
    )
    plan = PlanRevision.draft(
        goal_id=goal.goal_id,
        completion_contract=contract,
        nodes=proposal.plan_revision.nodes,
        edges=proposal.plan_revision.edges,
        branches=proposal.plan_revision.branches,
        phases=proposal.plan_revision.phases,
        plan_revision_id=proposal.plan_revision.plan_revision_id,
        version=base.version + 1,
        supersedes_plan_revision_id=base.plan_revision_id,
        created_at=proposal.plan_revision.created_at,
        design_document=proposal.plan_revision.design_document,
    )
    return replace(proposal, contract=contract, plan_revision=plan)


def _validate_source_discussion_base(
    uow: UnitOfWork, approved: PlanRevision, base: PlanRevision
) -> None:
    current = base
    while current.plan_revision_id != approved.plan_revision_id:
        if (
            current.goal_id != approved.goal_id
            or current.status is not PlanRevisionStatus.DRAFT
            or current.supersedes_plan_revision_id is None
        ):
            raise ApplicationError("Latest draft does not descend from the source approval")
        parent = _required_plan(uow, current.supersedes_plan_revision_id)
        if current.version != parent.version + 1:
            raise ApplicationError("Source discussion draft version chain is not continuous")
        _required_contract_for_plan(uow, current)
        current = parent


def _recheck_discussion_source(
    uow: UnitOfWork, *, goal: Goal, base: PlanRevision, source: _DiscussionSource
) -> None:
    current_goal = _required_goal(uow, goal.goal_id)
    if current_goal != goal:
        raise ApplicationError("Goal or approval changed during source discussion")
    run = _required_run(uow, source.run.run_id)
    approved = _validate_source_discussion_run(uow, current_goal, run)
    _require_no_other_active_runs(uow, goal.goal_id, run.run_id)
    current_base = _source_discussion_base(uow, approved)
    if current_base != base:
        raise ApplicationError("Latest draft changed during source discussion")
    current = _capture_discussion_source(uow, source_run=run, approved_plan=approved)
    if current != source:
        raise ApplicationError("Source control, execution or intervention facts changed")


def _required_project(uow: UnitOfWork, project_id: ID) -> Project:
    project = uow.states.get_project(project_id)
    if project is None:
        raise EntityNotFoundError(f"Project {project_id} does not exist")
    return project
