import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from ehai import new_id
from ehai.application.planner import (
    NON_EMPTY_ARTIFACT_CRITERION,
    DeterministicPlanner,
    ExplorationBudget,
)
from ehai.domain.goal import Goal
from ehai.infrastructure.planners.codex_protocol import (
    CodexPlannerProtocolError,
    build_codex_planner_input,
    codex_planner_input_schema_json,
    codex_planner_output_schema_json,
    map_codex_planner_event_types,
    parse_codex_planner_result,
)


def _document() -> dict[str, object]:
    return {
        "summary": "two approaches",
        "fork": {"title": "Fork", "instruction": "Start both."},
        "branches": [
            {"label": "alpha", "title": "Alpha", "instruction": "Try alpha."},
            {"label": "beta", "title": "Beta", "instruction": "Try beta."},
        ],
        "evaluator": {"title": "Evaluate", "instruction": "Compare evidence."},
        "merge": {"title": "Merge", "instruction": "Merge the winner."},
    }


def test_codex_planner_protocol_parses_exact_two_branch_document() -> None:
    parsed = parse_codex_planner_result(json.dumps(_document()))

    assert parsed.summary == "two approaches"
    assert tuple(branch.label for branch in parsed.branches) == ("alpha", "beta")
    assert json.loads(codex_planner_output_schema_json())["additionalProperties"] is False


def test_codex_planner_input_schema_distinguishes_propose_and_replan() -> None:
    schema = json.loads(codex_planner_input_schema_json())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    goal = Goal.create(new_id(), "produce evidence")
    budget = ExplorationBudget(max_attempts=5)

    proposed = build_codex_planner_input(
        goal,
        (NON_EMPTY_ARTIFACT_CRITERION,),
        budget,
    )
    validator.validate(proposed)
    assert proposed["operation"] == "propose"
    assert "base_plan_revision" not in proposed

    base_proposal = DeterministicPlanner().propose(goal, (NON_EMPTY_ARTIFACT_CRITERION,))
    base = base_proposal.plan_revision.approve(base_proposal.contract.confirm())
    replanned = build_codex_planner_input(
        goal,
        (NON_EMPTY_ARTIFACT_CRITERION,),
        budget,
        base,
    )
    validator.validate(replanned)
    assert replanned["operation"] == "replan"
    base_document = replanned["base_plan_revision"]
    assert isinstance(base_document, dict)
    assert base_document["plan_revision_id"] == base.plan_revision_id
    assert set(base_document) == {
        "plan_revision_id",
        "version",
        "completion_contract_id",
        "completion_contract_version",
        "nodes",
        "edges",
        "branches",
    }

    proposed["unexpected"] = True
    with pytest.raises(ValidationError):
        validator.validate(proposed)


def test_codex_planner_event_mapping_is_role_specific_and_bounded() -> None:
    mapped = map_codex_planner_event_types(
        ("thread.started", "turn.started", "future.event", "future.second")
    )

    assert mapped == (
        "planner.provider.started",
        "planner.started",
        "planner.provider.event",
    )


@pytest.mark.parametrize("mutation", ["extra", "duplicate-label", "blank"])
def test_codex_planner_protocol_rejects_non_strict_documents(mutation: str) -> None:
    document = _document()
    if mutation == "extra":
        document["completed"] = True
    elif mutation == "duplicate-label":
        branches = document["branches"]
        assert isinstance(branches, list)
        second = branches[1]
        assert isinstance(second, dict)
        second["label"] = "alpha"
    else:
        fork = document["fork"]
        assert isinstance(fork, dict)
        fork["instruction"] = "   "

    with pytest.raises(CodexPlannerProtocolError):
        parse_codex_planner_result(json.dumps(document))
