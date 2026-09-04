from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ehai import ID, JsonValue, new_id
from ehai.application.builtin_agent import ModelRequest, ModelResponse, ToolCall
from ehai.application.orchestrator import ready_nodes
from ehai.domain import Goal, PlanNodeKind, PlanNodeStatus, PlanRevisionStatus
from ehai.domain.planning import PlanNode, PlanRevision
from ehai.infrastructure.planners import BuiltinPlannerAdapter, BuiltinPlannerError
from ehai.infrastructure.planners.plan_graph_tools import MAX_PLAN_OPERATIONS

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

_EXPLORATION_STEPS: tuple[tuple[ToolCall, ...], ...] = (
    tuple(
        ToolCall(
            f"node-{key}",
            "add_plan_node",
            {"key": key, "title": title, "instruction": title, "kind": kind},
        )
        for key, title, kind in (
            ("research", "Gather context", "work"),
            ("fork", "Fork exploration", "fork"),
            ("alpha-first", "Explore alpha", "work"),
            ("alpha-second", "Refine alpha", "work"),
            ("beta-first", "Explore beta", "work"),
            ("beta-second", "Refine beta", "work"),
            ("evaluator", "Evaluate candidates", "evaluator"),
            ("merge", "Merge selection", "merge"),
            ("verify", "Verify final result", "work"),
        )
    ),
    (
        ToolCall(
            "branch-alpha",
            "set_plan_branch",
            {
                "branch_key": "alpha",
                "label": "alpha",
                "fork_node_key": "fork",
                "node_keys": ["alpha-first", "alpha-second"],
                "merge_node_key": "merge",
                "remove": False,
            },
        ),
        ToolCall(
            "branch-beta",
            "set_plan_branch",
            {
                "branch_key": "beta",
                "label": "beta",
                "fork_node_key": "fork",
                "node_keys": ["beta-first", "beta-second"],
                "merge_node_key": "merge",
                "remove": False,
            },
        ),
    ),
    tuple(
        ToolCall(
            f"edge-{index}",
            "add_plan_edge",
            {
                "source": source,
                "target": target,
                "edge_type": edge_type,
                "branch_key": branch_key,
                "condition": None,
            },
        )
        for index, (source, target, edge_type, branch_key) in enumerate(
            (
                ("research", "fork", "dependency", None),
                ("fork", "alpha-first", "exploration", "alpha"),
                ("fork", "alpha-first", "dependency", None),
                ("alpha-first", "alpha-second", "dependency", "alpha"),
                ("alpha-second", "merge", "merge", "alpha"),
                ("fork", "beta-first", "exploration", "beta"),
                ("fork", "beta-first", "dependency", None),
                ("beta-first", "beta-second", "dependency", "beta"),
                ("beta-second", "merge", "merge", "beta"),
                ("alpha-second", "evaluator", "dependency", None),
                ("beta-second", "evaluator", "dependency", None),
                ("evaluator", "merge", "dependency", None),
                ("merge", "verify", "dependency", None),
            )
        )
    ),
    (ToolCall("finish", "finish_plan", {}),),
)


class _ScriptedPlannerClient:
    """Serve one scripted ToolCall batch per model step."""

    def __init__(self, steps: tuple[tuple[ToolCall, ...], ...]) -> None:
        self._steps = list(steps)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._steps:
            raise AssertionError("ScriptedPlannerClient has no steps left")
        calls = self._steps.pop(0)
        return ModelResponse("", calls, provider_response_id=f"resp-{len(self.requests)}")

    async def aclose(self) -> None:
        self.closed = True


class _RepairingPlannerClient:
    """Finish a cyclic graph, read the diagnostics, then repair and finish again."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        step = len(self.requests)
        if step == 1:
            calls = (
                ToolCall(
                    "node-implement",
                    "add_plan_node",
                    {
                        "key": "implement",
                        "title": "Implement",
                        "instruction": "Do it",
                        "kind": "work",
                    },
                ),
                ToolCall(
                    "node-inspect",
                    "add_plan_node",
                    {"key": "inspect", "title": "Inspect", "instruction": "Check", "kind": "work"},
                ),
                ToolCall(
                    "edge-1",
                    "add_plan_edge",
                    {
                        "source": "implement",
                        "target": "inspect",
                        "edge_type": "dependency",
                        "branch_key": None,
                        "condition": None,
                    },
                ),
                ToolCall(
                    "edge-2",
                    "add_plan_edge",
                    {
                        "source": "inspect",
                        "target": "implement",
                        "edge_type": "dependency",
                        "branch_key": None,
                        "condition": None,
                    },
                ),
                ToolCall("finish-1", "finish_plan", {}),
            )
        elif step == 2 and _saw_cycle_diagnostic(request):
            calls = (
                ToolCall(
                    "repair",
                    "remove_plan_edge",
                    {
                        "source": "inspect",
                        "target": "implement",
                        "edge_type": "dependency",
                        "branch_key": None,
                    },
                ),
                ToolCall("finish-2", "finish_plan", {}),
            )
        else:
            raise AssertionError(f"unexpected planner step {step}")
        return ModelResponse("", calls, provider_response_id=f"resp-{step}")

    async def aclose(self) -> None:
        self.closed = True


def _saw_cycle_diagnostic(request: ModelRequest) -> bool:
    for message in reversed(request.messages):
        if message.role.value == "tool":
            return "CYCLE_DETECTED" in message.content
    return False


class _NeverFinishingClient:
    """Always call finish_plan on an invalid graph."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            "",
            (
                ToolCall(
                    "node-stray",
                    "add_plan_node",
                    {"key": "stray", "title": "Stray", "instruction": "Stray", "kind": "fork"},
                ),
                ToolCall(f"finish-{len(self.requests)}", "finish_plan", {}),
            ),
        )

    async def aclose(self) -> None:
        self.closed = True


def _goal() -> Goal:
    return Goal.create(new_id(), "produce a verified artifact", created_at=NOW)


def _adapter(client: object) -> BuiltinPlannerAdapter:
    return BuiltinPlannerAdapter(
        model="gpt-test",
        model_client_factory=lambda _profile: client,  # type: ignore[arg-type,return-value]
        id_factory=_SequenceIds(),
        clock=lambda: NOW,
    )


class _SequenceIds:
    def __init__(self) -> None:
        self._count = 0

    def __call__(self) -> ID:
        self._count += 1
        return f"00000000-0000-4000-8000-{self._count:012d}"


def test_adapter_builds_shared_node_two_multi_node_branches_from_tool_calls() -> None:
    client = _ScriptedPlannerClient(_EXPLORATION_STEPS)
    goal = _goal()

    proposal = _adapter(client).propose(goal, ("artifact:non-empty",))

    assert client.closed
    assert all(request.tool_choice == "required" for request in client.requests)
    revision = proposal.plan_revision
    assert revision.status is PlanRevisionStatus.DRAFT
    keys = {node.plan_node_id: node.title for node in revision.nodes}
    assert len(revision.nodes) == 9
    kinds = [node.kind for node in revision.nodes]
    assert kinds.count(PlanNodeKind.FORK) == 1
    assert kinds.count(PlanNodeKind.EVALUATOR) == 1
    assert kinds.count(PlanNodeKind.MERGE) == 1
    assert len(revision.branches) == 2
    assert {len(branch.node_ids) for branch in revision.branches} == {2}
    node_by_title = {node.title: node for node in revision.nodes}
    assert node_by_title["Evaluate candidates"].required_dependency_ids == (
        node_by_title["Refine alpha"].plan_node_id,
        node_by_title["Refine beta"].plan_node_id,
    )
    assert "planner.responses.completed" in proposal.planner_event_types
    assert keys
    # Goal 工具循环没有把 CompletionContract 之外的任何领域对象暴露给模型
    assert goal.completion_contract is None


def test_adapter_generated_branches_are_simultaneously_ready_after_fork() -> None:
    client = _ScriptedPlannerClient(_EXPLORATION_STEPS)
    goal = _goal()

    proposal = _adapter(client).propose(goal, ("artifact:non-empty",))
    revision = proposal.plan_revision
    approved = revision.approve(
        proposal.contract.confirm(confirmed_at=NOW),
        approved_at=NOW,
    )
    node_by_id = {node.plan_node_id: node for node in approved.nodes}
    branch_entries = {branch.node_ids[0]: branch.label for branch in approved.branches}
    fork_id = approved.branches[0].fork_node_id
    research_id = next(
        node.plan_node_id
        for node in approved.nodes
        if node.required_dependency_ids == () and node.plan_node_id != fork_id
    )
    assert {node_by_id[entry].required_dependency_ids for entry in branch_entries} == {(fork_id,)}

    def _completed(node: PlanNode) -> PlanNode:
        return PlanNode.rehydrate(
            plan_node_id=node.plan_node_id,
            title=node.title,
            instruction=node.instruction,
            kind=node.kind,
            required_dependency_ids=node.required_dependency_ids,
            required_check_ids=node.required_check_ids,
            status=PlanNodeStatus.COMPLETED,
        )

    with_prefix_done = PlanRevision.rehydrate(
        plan_revision_id=approved.plan_revision_id,
        goal_id=approved.goal_id,
        version=approved.version,
        completion_contract_id=approved.completion_contract_id,
        completion_contract_version=approved.completion_contract_version,
        nodes=tuple(
            _completed(node) if node.plan_node_id in {research_id, fork_id} else node
            for node in approved.nodes
        ),
        edges=approved.edges,
        branches=approved.branches,
        created_at=approved.created_at,
        status=PlanRevisionStatus.APPROVED,
        approved_at=approved.approved_at,
        supersedes_plan_revision_id=approved.supersedes_plan_revision_id,
    )

    ready = ready_nodes(with_prefix_done)

    ready_keys = {node.plan_node_id for node in ready}
    assert ready_keys == set(branch_entries)


def test_adapter_returns_diagnostics_and_model_repairs_in_same_call() -> None:
    client = _RepairingPlannerClient()
    goal = _goal()

    proposal = _adapter(client).propose(goal, ("artifact:non-empty",))

    assert client.closed
    assert len(client.requests) == 2
    revision = proposal.plan_revision
    node_by_title = {node.title: node for node in revision.nodes}
    assert node_by_title["Inspect"].required_dependency_ids == (
        node_by_title["Implement"].plan_node_id,
    )
    assert node_by_title["Implement"].required_dependency_ids == ()


def test_adapter_requires_successful_finish_plan_to_be_last_tool_call() -> None:
    class _FinishInMiddleClient:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []
            self.closed = False

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse(
                    "",
                    (
                        ToolCall(
                            "node-first",
                            "add_plan_node",
                            {
                                "key": "first",
                                "title": "First",
                                "instruction": "First",
                                "kind": "work",
                            },
                        ),
                        ToolCall("finish-middle", "finish_plan", {}),
                        ToolCall(
                            "node-second",
                            "add_plan_node",
                            {
                                "key": "second",
                                "title": "Second",
                                "instruction": "Second",
                                "kind": "work",
                            },
                        ),
                    ),
                    provider_response_id="resp-middle",
                )
            if len(self.requests) == 2:
                assert any(
                    "FINISH_TOOL_NOT_LAST" in message.content
                    and '"location":"response.tool_calls[1]"' in message.content
                    for message in request.input_messages
                    if message.role.value == "tool"
                )
                return ModelResponse(
                    "",
                    (ToolCall("finish-last", "finish_plan", {}),),
                    provider_response_id="resp-last",
                )
            raise AssertionError("unexpected planner step")

        async def aclose(self) -> None:
            self.closed = True

    client = _FinishInMiddleClient()

    proposal = _adapter(client).propose(_goal(), ("artifact:non-empty",))

    assert client.closed
    assert len(client.requests) == 2
    assert {node.title for node in proposal.plan_revision.nodes} == {"First", "Second"}


def test_adapter_terminates_with_validation_budget_exhausted() -> None:
    client = _NeverFinishingClient()
    goal = _goal()

    with pytest.raises(BuiltinPlannerError, match="validation budget exhausted"):
        _adapter(client).propose(goal, ("artifact:non-empty",))
    assert client.closed
    assert len(client.requests) == 5


def test_adapter_rejects_model_response_without_tool_calls() -> None:
    class _PlainClient:
        def __init__(self) -> None:
            self.closed = False

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            return ModelResponse("Here is my plan", final_text="Here is my plan")

        async def aclose(self) -> None:
            self.closed = True

    goal = _goal()
    with pytest.raises(BuiltinPlannerError, match="no ToolCalls"):
        _adapter(_PlainClient()).propose(goal, ("artifact:non-empty",))


def test_adapter_cannot_bypass_completion_checks_or_contract() -> None:
    client = _ScriptedPlannerClient(_EXPLORATION_STEPS)
    goal = _goal()

    proposal = _adapter(client).propose(goal, ("artifact:non-empty",))

    assert not proposal.contract.is_confirmed
    assert proposal.check_specs
    required_check_ids = set(proposal.contract.required_check_ids)
    assert {
        check.check_id for check in proposal.check_specs if check.required
    } == required_check_ids
    assert all(
        node.required_check_ids == proposal.contract.required_check_ids
        for node in proposal.plan_revision.nodes
    )
    for request in client.requests:
        for tool in request.tools:
            schema: dict[str, JsonValue] = tool.input_schema
            properties = schema.get("properties")
            assert isinstance(properties, dict)
            assert not any("check" in name for name in properties)


def test_adapter_passes_input_document_and_keeps_continuation_history() -> None:
    client = _ScriptedPlannerClient(_EXPLORATION_STEPS)
    goal = _goal()

    _adapter(client).propose(goal, ("artifact:non-empty",))

    first = client.requests[0]
    assert "EHAI BUILT-IN PLANNER PROTOCOL v2" in first.messages[-1].content
    assert goal.objective in first.messages[-1].content
    assert str(MAX_PLAN_OPERATIONS) in first.messages[-1].content
    assert first.previous_response_id is None
    second = client.requests[1]
    assert second.previous_response_id == "resp-1"
    tool_outputs = [message for message in second.input_messages if message.role.value == "tool"]
    assert len(tool_outputs) == len(_EXPLORATION_STEPS[0])
