"""Canonical JSON codecs between SQLite rows and validated domain objects."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from ehai import ID, JsonValue, format_utc_datetime, json_dumps, json_loads, parse_utc_datetime
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import (
    CheckKind,
    Checkpoint,
    CheckResult,
    CheckRun,
    CheckRunStatus,
    CheckSpec,
    GateDecision,
)
from ehai.domain.execution import Attempt, AttemptStatus, Run, RunStatus
from ehai.domain.goal import CompletionContract, Goal, GoalStatus, Project
from ehai.domain.planning import (
    Branch,
    BranchStatus,
    Edge,
    EdgeType,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanRevision,
    PlanRevisionStatus,
)
from ehai.domain.runtime import DispatchWork, DispatchWorkStatus
from ehai.domain.workers import (
    AgentSessionRef,
    AttemptActivity,
    BuiltinExecutionRef,
    ExecutionHandle,
    ExternalExecutionRef,
    SessionPolicy,
    WorkerCapability,
    WorkerEndpoint,
    WorkerEndpointStatus,
    WorkerEndpointType,
    WorkerKind,
    WorkerProfile,
)


def encode_project(project: Project) -> str:
    return json_dumps(
        {
            "project_id": project.project_id,
            "name": project.name,
            "created_at": format_utc_datetime(project.created_at),
        }
    )


def decode_project(snapshot: str) -> Project:
    document = _load_object(snapshot, "Project")
    return Project(
        project_id=ID(_string(document, "project_id")),
        name=_string(document, "name"),
        created_at=parse_utc_datetime(_string(document, "created_at")),
    )


def encode_completion_contract(contract: CompletionContract) -> str:
    return json_dumps(_completion_contract_document(contract))


def decode_completion_contract(snapshot: str) -> CompletionContract:
    return _decode_completion_contract_document(_load_object(snapshot, "CompletionContract"))


def encode_goal(goal: Goal) -> str:
    return json_dumps(
        {
            "goal_id": goal.goal_id,
            "project_id": goal.project_id,
            "objective": goal.objective,
            "created_at": format_utc_datetime(goal.created_at),
            "completion_contract_id": (
                None
                if goal.completion_contract is None
                else goal.completion_contract.completion_contract_id
            ),
            "status": goal.status.value,
            "final_gate_id": goal.final_gate_id,
            "satisfied_at": _format_optional_datetime(goal.satisfied_at),
        }
    )


def decode_goal(snapshot: str, completion_contract: CompletionContract | None) -> Goal:
    document = _load_object(snapshot, "Goal")
    stored_contract_id = _optional_string(document, "completion_contract_id")
    actual_contract_id = (
        None if completion_contract is None else str(completion_contract.completion_contract_id)
    )
    if stored_contract_id != actual_contract_id:
        raise ValueError("Goal snapshot CompletionContract reference does not match its row")
    return Goal.rehydrate(
        goal_id=ID(_string(document, "goal_id")),
        project_id=ID(_string(document, "project_id")),
        objective=_string(document, "objective"),
        created_at=parse_utc_datetime(_string(document, "created_at")),
        completion_contract=completion_contract,
        status=GoalStatus(_string(document, "status")),
        final_gate_id=_optional_id(document, "final_gate_id"),
        satisfied_at=_optional_datetime(document, "satisfied_at"),
    )


def encode_plan_revision(revision: PlanRevision) -> str:
    return json_dumps(_plan_revision_document(revision))


def decode_plan_revision(
    snapshot: str,
    nodes: tuple[PlanNode, ...],
    edges: tuple[Edge, ...],
    branches: tuple[Branch, ...],
) -> PlanRevision:
    return _decode_plan_revision_document(
        _load_object(snapshot, "PlanRevision"),
        nodes,
        edges,
        branches,
    )


def encode_plan_node(node: PlanNode) -> str:
    return json_dumps(_plan_node_document(node))


def decode_plan_node(snapshot: str) -> PlanNode:
    return _decode_plan_node_document(_load_object(snapshot, "PlanNode"))


def encode_edge(edge: Edge) -> str:
    return json_dumps(_edge_document(edge))


def decode_edge(snapshot: str) -> Edge:
    return _decode_edge_document(_load_object(snapshot, "Edge"))


def encode_branch(branch: Branch) -> str:
    return json_dumps(_branch_document(branch))


def decode_branch(snapshot: str) -> Branch:
    return _decode_branch_document(_load_object(snapshot, "Branch"))


def encode_run(run: Run) -> str:
    return json_dumps(_run_document(run))


def decode_run(snapshot: str) -> Run:
    return _decode_run_document(_load_object(snapshot, "Run"))


def encode_attempt(attempt: Attempt) -> str:
    return json_dumps(
        {
            "attempt_id": attempt.attempt_id,
            "run_id": attempt.run_id,
            "plan_node_id": attempt.plan_node_id,
            "sequence": attempt.sequence,
            "status": attempt.status.value,
            "artifact_ids": list(attempt.artifact_ids),
            "created_at": format_utc_datetime(attempt.created_at),
            "started_at": _format_optional_datetime(attempt.started_at),
            "ended_at": _format_optional_datetime(attempt.ended_at),
            "outcome_reason": attempt.outcome_reason,
            "worker_profile_id": attempt.worker_profile_id,
            "worker_endpoint_id": attempt.worker_endpoint_id,
            "agent_session_ref_id": attempt.agent_session_ref_id,
            "execution_handle": (
                None
                if attempt.execution_handle is None
                else _execution_handle_document(attempt.execution_handle)
            ),
            "activity": None if attempt.activity is None else attempt.activity.value,
            "event_cursor": attempt.event_cursor,
            "heartbeat_at": _format_optional_datetime(attempt.heartbeat_at),
            "progress_at": _format_optional_datetime(attempt.progress_at),
            "deadline_at": _format_optional_datetime(attempt.deadline_at),
            "lease_expires_at": _format_optional_datetime(attempt.lease_expires_at),
        }
    )


def decode_attempt(snapshot: str) -> Attempt:
    document = _load_object(snapshot, "Attempt")
    handle_document = document.get("execution_handle")
    activity = _optional_string(document, "activity")
    return Attempt.rehydrate(
        attempt_id=ID(_string(document, "attempt_id")),
        run_id=ID(_string(document, "run_id")),
        plan_node_id=ID(_string(document, "plan_node_id")),
        sequence=_integer(document, "sequence"),
        status=AttemptStatus(_string(document, "status")),
        artifact_ids=_ids(document, "artifact_ids"),
        created_at=parse_utc_datetime(_string(document, "created_at")),
        started_at=_optional_datetime(document, "started_at"),
        ended_at=_optional_datetime(document, "ended_at"),
        outcome_reason=_optional_string(document, "outcome_reason"),
        worker_profile_id=_optional_id(document, "worker_profile_id"),
        worker_endpoint_id=_optional_id(document, "worker_endpoint_id"),
        agent_session_ref_id=_optional_id(document, "agent_session_ref_id"),
        execution_handle=(
            None
            if handle_document is None
            else _decode_execution_handle_document(_object(handle_document, "ExecutionHandle"))
        ),
        activity=None if activity is None else AttemptActivity(activity),
        event_cursor=_optional_string(document, "event_cursor"),
        heartbeat_at=_optional_datetime(document, "heartbeat_at"),
        progress_at=_optional_datetime(document, "progress_at"),
        deadline_at=_optional_datetime(document, "deadline_at"),
        lease_expires_at=_optional_datetime(document, "lease_expires_at"),
    )


def encode_dispatch_work(work: DispatchWork) -> str:
    return json_dumps(
        {
            "dispatch_work_id": work.dispatch_work_id,
            "run_id": work.run_id,
            "status": work.status.value,
            "created_at": format_utc_datetime(work.created_at),
            "claimed_at": _format_optional_datetime(work.claimed_at),
            "completed_at": _format_optional_datetime(work.completed_at),
        }
    )


def decode_dispatch_work(snapshot: str) -> DispatchWork:
    document = _load_object(snapshot, "DispatchWork")
    return DispatchWork.rehydrate(
        dispatch_work_id=ID(_string(document, "dispatch_work_id")),
        run_id=ID(_string(document, "run_id")),
        status=DispatchWorkStatus(_string(document, "status")),
        created_at=parse_utc_datetime(_string(document, "created_at")),
        claimed_at=_optional_datetime(document, "claimed_at"),
        completed_at=_optional_datetime(document, "completed_at"),
    )


def encode_worker_profile(profile: WorkerProfile) -> str:
    capabilities: list[JsonValue] = [item.name for item in sorted(profile.capabilities)]
    return json_dumps(
        {
            "worker_profile_id": profile.worker_profile_id,
            "name": profile.name,
            "kind": profile.kind.value,
            "model": profile.model,
            "capabilities": capabilities,
            "session_policy": profile.session_policy.value,
            "budget_ref": profile.budget_ref,
            "credential_ref": profile.credential_ref,
        }
    )


def decode_worker_profile(snapshot: str) -> WorkerProfile:
    document = _load_object(snapshot, "WorkerProfile")
    return WorkerProfile(
        worker_profile_id=ID(_string(document, "worker_profile_id")),
        name=_string(document, "name"),
        kind=WorkerKind(_string(document, "kind")),
        model=_string(document, "model"),
        capabilities=frozenset(
            WorkerCapability(item) for item in _strings(document, "capabilities")
        ),
        session_policy=SessionPolicy(_string(document, "session_policy")),
        budget_ref=_optional_string(document, "budget_ref"),
        credential_ref=_optional_string(document, "credential_ref"),
    )


def encode_worker_endpoint(endpoint: WorkerEndpoint) -> str:
    return json_dumps(
        {
            "worker_endpoint_id": endpoint.worker_endpoint_id,
            "name": endpoint.name,
            "worker_kind": endpoint.worker_kind.value,
            "endpoint_type": endpoint.endpoint_type.value,
            "endpoint_ref": endpoint.endpoint_ref,
            "capacity": endpoint.capacity,
            "status": endpoint.status.value,
        }
    )


def decode_worker_endpoint(snapshot: str) -> WorkerEndpoint:
    document = _load_object(snapshot, "WorkerEndpoint")
    return WorkerEndpoint(
        worker_endpoint_id=ID(_string(document, "worker_endpoint_id")),
        name=_string(document, "name"),
        worker_kind=WorkerKind(_string(document, "worker_kind")),
        endpoint_type=WorkerEndpointType(_string(document, "endpoint_type")),
        endpoint_ref=_string(document, "endpoint_ref"),
        capacity=_integer(document, "capacity"),
        status=WorkerEndpointStatus(_string(document, "status")),
    )


def encode_agent_session_ref(session: AgentSessionRef) -> str:
    return json_dumps(
        {
            "agent_session_ref_id": session.agent_session_ref_id,
            "run_id": session.run_id,
            "worker_profile_id": session.worker_profile_id,
            "worker_endpoint_id": session.worker_endpoint_id,
            "provider_session_id": session.provider_session_id,
            "recoverable": session.recoverable,
            "created_at": format_utc_datetime(session.created_at),
        }
    )


def decode_agent_session_ref(snapshot: str) -> AgentSessionRef:
    document = _load_object(snapshot, "AgentSessionRef")
    return AgentSessionRef(
        agent_session_ref_id=ID(_string(document, "agent_session_ref_id")),
        run_id=ID(_string(document, "run_id")),
        worker_profile_id=ID(_string(document, "worker_profile_id")),
        worker_endpoint_id=ID(_string(document, "worker_endpoint_id")),
        provider_session_id=_string(document, "provider_session_id"),
        recoverable=_boolean(document, "recoverable"),
        created_at=parse_utc_datetime(_string(document, "created_at")),
    )


def encode_external_execution_ref(reference: ExternalExecutionRef) -> str:
    return json_dumps(_external_execution_document(reference))


def decode_external_execution_ref(snapshot: str) -> ExternalExecutionRef:
    return _decode_external_execution_document(_load_object(snapshot, "ExternalExecutionRef"))


def encode_builtin_execution_ref(reference: BuiltinExecutionRef) -> str:
    return json_dumps(_builtin_execution_document(reference))


def decode_builtin_execution_ref(snapshot: str) -> BuiltinExecutionRef:
    return _decode_builtin_execution_document(_load_object(snapshot, "BuiltinExecutionRef"))


def encode_check_spec(check_spec: CheckSpec) -> str:
    return json_dumps(
        {
            "check_id": check_spec.check_id,
            "name": check_spec.name,
            "kind": check_spec.kind.value,
            "description": check_spec.description,
            "required": check_spec.required,
        }
    )


def decode_check_spec(snapshot: str) -> CheckSpec:
    document = _load_object(snapshot, "CheckSpec")
    return CheckSpec(
        check_id=ID(_string(document, "check_id")),
        name=_string(document, "name"),
        kind=CheckKind(_string(document, "kind")),
        description=_string(document, "description"),
        required=_boolean(document, "required"),
    )


def encode_check_run(check_run: CheckRun) -> str:
    return json_dumps(
        {
            "check_run_id": check_run.check_run_id,
            "run_id": check_run.run_id,
            "plan_node_id": check_run.plan_node_id,
            "attempt_id": check_run.attempt_id,
            "check_id": check_run.check_id,
            "status": check_run.status.value,
            "created_at": format_utc_datetime(check_run.created_at),
            "started_at": _format_optional_datetime(check_run.started_at),
            "ended_at": _format_optional_datetime(check_run.ended_at),
            "result": (
                None if check_run.result is None else _check_result_document(check_run.result)
            ),
            "failure_reason": check_run.failure_reason,
        }
    )


def decode_check_run(snapshot: str) -> CheckRun:
    document = _load_object(snapshot, "CheckRun")
    result_document = document.get("result")
    result = (
        None
        if result_document is None
        else _decode_check_result_document(_object(result_document, "CheckResult"))
    )
    return CheckRun.rehydrate(
        check_run_id=ID(_string(document, "check_run_id")),
        run_id=ID(_string(document, "run_id")),
        plan_node_id=ID(_string(document, "plan_node_id")),
        attempt_id=ID(_string(document, "attempt_id")),
        check_id=ID(_string(document, "check_id")),
        status=CheckRunStatus(_string(document, "status")),
        created_at=parse_utc_datetime(_string(document, "created_at")),
        started_at=_optional_datetime(document, "started_at"),
        ended_at=_optional_datetime(document, "ended_at"),
        result=result,
        failure_reason=_optional_string(document, "failure_reason"),
    )


def encode_checkpoint(checkpoint: Checkpoint) -> str:
    plan_document = _plan_revision_document(checkpoint.plan_revision)
    plan_document["nodes"] = [_plan_node_document(node) for node in checkpoint.plan_revision.nodes]
    plan_document["edges"] = [_edge_document(edge) for edge in checkpoint.plan_revision.edges]
    plan_document["branches"] = [
        _branch_document(branch) for branch in checkpoint.plan_revision.branches
    ]
    return json_dumps(
        {
            "checkpoint_id": checkpoint.checkpoint_id,
            "plan_revision": plan_document,
            "run": _run_document(checkpoint.run),
            "event_offset": checkpoint.event_offset,
            "gate_decision": _gate_decision_document(checkpoint.gate_decision),
            "branch_selections": {
                str(fork_id): str(branch_id)
                for fork_id, branch_id in checkpoint.branch_selections.items()
            },
            "artifact_refs": list(checkpoint.artifact_refs),
            "created_at": format_utc_datetime(checkpoint.created_at),
        }
    )


def decode_checkpoint(snapshot: str) -> Checkpoint:
    document = _load_object(snapshot, "Checkpoint")
    plan_document = _object(document.get("plan_revision"), "PlanRevision")
    nodes = tuple(
        _decode_plan_node_document(item)
        for item in _object_list(plan_document, "nodes", "PlanNode")
    )
    edges = tuple(
        _decode_edge_document(item) for item in _object_list(plan_document, "edges", "Edge")
    )
    branches = tuple(
        _decode_branch_document(item) for item in _object_list(plan_document, "branches", "Branch")
    )
    plan_revision = _decode_plan_revision_document(plan_document, nodes, edges, branches)
    run = _decode_run_document(_object(document.get("run"), "Run"))
    decision = _decode_gate_decision_document(
        _object(document.get("gate_decision"), "GateDecision")
    )
    selections_document = _object(document.get("branch_selections"), "branch_selections")
    selections = {
        ID(key): ID(_json_string(value, f"branch selection {key}"))
        for key, value in selections_document.items()
    }
    return Checkpoint(
        checkpoint_id=ID(_string(document, "checkpoint_id")),
        plan_revision=plan_revision,
        run=run,
        event_offset=_integer(document, "event_offset"),
        gate_decision=decision,
        branch_selections=selections,
        artifact_refs=_ids(document, "artifact_refs"),
        created_at=parse_utc_datetime(_string(document, "created_at")),
    )


def encode_artifact(artifact: Artifact) -> str:
    return json_dumps(artifact.to_dict())


def decode_artifact(snapshot: str) -> Artifact:
    return Artifact.from_dict(_load_object(snapshot, "Artifact"))


def _completion_contract_document(contract: CompletionContract) -> dict[str, JsonValue]:
    return {
        "completion_contract_id": contract.completion_contract_id,
        "goal_id": contract.goal_id,
        "version": contract.version,
        "criteria": list(contract.criteria),
        "required_check_ids": list(contract.required_check_ids),
        "created_at": format_utc_datetime(contract.created_at),
        "confirmed_at": _format_optional_datetime(contract.confirmed_at),
        "supersedes_completion_contract_id": contract.supersedes_completion_contract_id,
    }


def _decode_completion_contract_document(
    document: Mapping[str, JsonValue],
) -> CompletionContract:
    return CompletionContract(
        completion_contract_id=ID(_string(document, "completion_contract_id")),
        goal_id=ID(_string(document, "goal_id")),
        version=_integer(document, "version"),
        criteria=_strings(document, "criteria"),
        required_check_ids=_ids(document, "required_check_ids"),
        created_at=parse_utc_datetime(_string(document, "created_at")),
        confirmed_at=_optional_datetime(document, "confirmed_at"),
        supersedes_completion_contract_id=_optional_id(
            document, "supersedes_completion_contract_id"
        ),
    )


def _plan_revision_document(revision: PlanRevision) -> dict[str, JsonValue]:
    return {
        "plan_revision_id": revision.plan_revision_id,
        "goal_id": revision.goal_id,
        "version": revision.version,
        "completion_contract_id": revision.completion_contract_id,
        "completion_contract_version": revision.completion_contract_version,
        "created_at": format_utc_datetime(revision.created_at),
        "status": revision.status.value,
        "approved_at": _format_optional_datetime(revision.approved_at),
        "supersedes_plan_revision_id": revision.supersedes_plan_revision_id,
    }


def _decode_plan_revision_document(
    document: Mapping[str, JsonValue],
    nodes: tuple[PlanNode, ...],
    edges: tuple[Edge, ...],
    branches: tuple[Branch, ...],
) -> PlanRevision:
    return PlanRevision.rehydrate(
        plan_revision_id=ID(_string(document, "plan_revision_id")),
        goal_id=ID(_string(document, "goal_id")),
        version=_integer(document, "version"),
        completion_contract_id=ID(_string(document, "completion_contract_id")),
        completion_contract_version=_integer(document, "completion_contract_version"),
        nodes=nodes,
        edges=edges,
        branches=branches,
        created_at=parse_utc_datetime(_string(document, "created_at")),
        status=PlanRevisionStatus(_string(document, "status")),
        approved_at=_optional_datetime(document, "approved_at"),
        supersedes_plan_revision_id=_optional_id(document, "supersedes_plan_revision_id"),
    )


def _plan_node_document(node: PlanNode) -> dict[str, JsonValue]:
    capabilities: list[JsonValue] = [item.name for item in sorted(node.required_capabilities)]
    return {
        "plan_node_id": node.plan_node_id,
        "title": node.title,
        "instruction": node.instruction,
        "kind": node.kind.value,
        "required_dependency_ids": list(node.required_dependency_ids),
        "required_check_ids": list(node.required_check_ids),
        "required_capabilities": capabilities,
        "session_policy": node.session_policy.value,
        "status": node.status.value,
    }


def _decode_plan_node_document(document: Mapping[str, JsonValue]) -> PlanNode:
    capability_names = (
        _strings(document, "required_capabilities") if "required_capabilities" in document else ()
    )
    session_policy = _optional_string(document, "session_policy")
    return PlanNode.rehydrate(
        plan_node_id=ID(_string(document, "plan_node_id")),
        title=_string(document, "title"),
        instruction=_string(document, "instruction"),
        kind=PlanNodeKind(_string(document, "kind")),
        required_dependency_ids=_ids(document, "required_dependency_ids"),
        required_check_ids=_ids(document, "required_check_ids"),
        required_capabilities=tuple(WorkerCapability(item) for item in capability_names),
        session_policy=(
            SessionPolicy.NEW if session_policy is None else SessionPolicy(session_policy)
        ),
        status=PlanNodeStatus(_string(document, "status")),
    )


def _execution_handle_document(handle: ExecutionHandle) -> dict[str, JsonValue]:
    return {
        "builtin": (
            None if handle.builtin is None else _builtin_execution_document(handle.builtin)
        ),
        "external": (
            None if handle.external is None else _external_execution_document(handle.external)
        ),
    }


def _decode_execution_handle_document(
    document: Mapping[str, JsonValue],
) -> ExecutionHandle:
    builtin = document.get("builtin")
    external = document.get("external")
    return ExecutionHandle(
        builtin=(
            None
            if builtin is None
            else _decode_builtin_execution_document(_object(builtin, "BuiltinExecutionRef"))
        ),
        external=(
            None
            if external is None
            else _decode_external_execution_document(_object(external, "ExternalExecutionRef"))
        ),
    )


def _external_execution_document(reference: ExternalExecutionRef) -> dict[str, JsonValue]:
    return {
        "external_execution_ref_id": reference.external_execution_ref_id,
        "attempt_id": reference.attempt_id,
        "agent_session_ref_id": reference.agent_session_ref_id,
        "provider_execution_id": reference.provider_execution_id,
    }


def _decode_external_execution_document(
    document: Mapping[str, JsonValue],
) -> ExternalExecutionRef:
    return ExternalExecutionRef(
        external_execution_ref_id=ID(_string(document, "external_execution_ref_id")),
        attempt_id=ID(_string(document, "attempt_id")),
        agent_session_ref_id=ID(_string(document, "agent_session_ref_id")),
        provider_execution_id=_string(document, "provider_execution_id"),
    )


def _builtin_execution_document(reference: BuiltinExecutionRef) -> dict[str, JsonValue]:
    return {
        "builtin_execution_ref_id": reference.builtin_execution_ref_id,
        "builtin_execution_id": reference.builtin_execution_id,
        "attempt_id": reference.attempt_id,
        "agent_session_ref_id": reference.agent_session_ref_id,
    }


def _decode_builtin_execution_document(
    document: Mapping[str, JsonValue],
) -> BuiltinExecutionRef:
    return BuiltinExecutionRef(
        builtin_execution_ref_id=ID(_string(document, "builtin_execution_ref_id")),
        builtin_execution_id=ID(_string(document, "builtin_execution_id")),
        attempt_id=ID(_string(document, "attempt_id")),
        agent_session_ref_id=ID(_string(document, "agent_session_ref_id")),
    )


def _edge_document(edge: Edge) -> dict[str, JsonValue]:
    return {
        "edge_id": edge.edge_id,
        "source_node_id": edge.source_node_id,
        "target_node_id": edge.target_node_id,
        "edge_type": edge.edge_type.value,
        "branch_id": edge.branch_id,
        "condition": edge.condition,
    }


def _decode_edge_document(document: Mapping[str, JsonValue]) -> Edge:
    return Edge(
        edge_id=ID(_string(document, "edge_id")),
        source_node_id=ID(_string(document, "source_node_id")),
        target_node_id=ID(_string(document, "target_node_id")),
        edge_type=EdgeType(_string(document, "edge_type")),
        branch_id=_optional_id(document, "branch_id"),
        condition=_optional_string(document, "condition"),
    )


def _branch_document(branch: Branch) -> dict[str, JsonValue]:
    return {
        "branch_id": branch.branch_id,
        "label": branch.label,
        "fork_node_id": branch.fork_node_id,
        "node_ids": list(branch.node_ids),
        "merge_node_id": branch.merge_node_id,
        "status": branch.status.value,
    }


def _decode_branch_document(document: Mapping[str, JsonValue]) -> Branch:
    return Branch.rehydrate(
        branch_id=ID(_string(document, "branch_id")),
        label=_string(document, "label"),
        fork_node_id=ID(_string(document, "fork_node_id")),
        node_ids=_ids(document, "node_ids"),
        merge_node_id=ID(_string(document, "merge_node_id")),
        status=BranchStatus(_string(document, "status")),
    )


def _run_document(run: Run) -> dict[str, JsonValue]:
    return {
        "run_id": run.run_id,
        "goal_id": run.goal_id,
        "plan_revision_id": run.plan_revision_id,
        "status": run.status.value,
        "created_at": format_utc_datetime(run.created_at),
        "started_at": _format_optional_datetime(run.started_at),
        "ended_at": _format_optional_datetime(run.ended_at),
        "status_reason": run.status_reason,
    }


def _decode_run_document(document: Mapping[str, JsonValue]) -> Run:
    return Run.rehydrate(
        run_id=ID(_string(document, "run_id")),
        goal_id=ID(_string(document, "goal_id")),
        plan_revision_id=ID(_string(document, "plan_revision_id")),
        status=RunStatus(_string(document, "status")),
        created_at=parse_utc_datetime(_string(document, "created_at")),
        started_at=_optional_datetime(document, "started_at"),
        ended_at=_optional_datetime(document, "ended_at"),
        status_reason=_optional_string(document, "status_reason"),
    )


def _check_result_document(result: CheckResult) -> dict[str, JsonValue]:
    return {
        "check_id": result.check_id,
        "check_run_id": result.check_run_id,
        "run_id": result.run_id,
        "plan_node_id": result.plan_node_id,
        "attempt_id": result.attempt_id,
        "passed": result.passed,
        "evaluated_at": format_utc_datetime(result.evaluated_at),
        "evidence_artifact_ids": list(result.evidence_artifact_ids),
        "output": result.output,
        "failure_reason": result.failure_reason,
    }


def _decode_check_result_document(document: Mapping[str, JsonValue]) -> CheckResult:
    return CheckResult(
        check_id=ID(_string(document, "check_id")),
        check_run_id=ID(_string(document, "check_run_id")),
        run_id=ID(_string(document, "run_id")),
        plan_node_id=ID(_string(document, "plan_node_id")),
        attempt_id=ID(_string(document, "attempt_id")),
        passed=_boolean(document, "passed"),
        evaluated_at=parse_utc_datetime(_string(document, "evaluated_at")),
        evidence_artifact_ids=_ids(document, "evidence_artifact_ids"),
        output=_optional_string(document, "output"),
        failure_reason=_optional_string(document, "failure_reason"),
    )


def _gate_decision_document(decision: GateDecision) -> dict[str, JsonValue]:
    return {
        "gate_id": decision.gate_id,
        "run_id": decision.run_id,
        "plan_node_id": decision.plan_node_id,
        "attempt_id": decision.attempt_id,
        "passed": decision.passed,
        "evaluated_at": format_utc_datetime(decision.evaluated_at),
        "required_check_ids": list(decision.required_check_ids),
        "failed_check_ids": list(decision.failed_check_ids),
        "evidence_artifact_ids": list(decision.evidence_artifact_ids),
        "reason": decision.reason,
    }


def _decode_gate_decision_document(document: Mapping[str, JsonValue]) -> GateDecision:
    return GateDecision(
        gate_id=ID(_string(document, "gate_id")),
        run_id=ID(_string(document, "run_id")),
        plan_node_id=ID(_string(document, "plan_node_id")),
        attempt_id=ID(_string(document, "attempt_id")),
        passed=_boolean(document, "passed"),
        evaluated_at=parse_utc_datetime(_string(document, "evaluated_at")),
        required_check_ids=_ids(document, "required_check_ids"),
        failed_check_ids=_ids(document, "failed_check_ids"),
        evidence_artifact_ids=_ids(document, "evidence_artifact_ids"),
        reason=_optional_string(document, "reason"),
    )


def _load_object(snapshot: str, name: str) -> dict[str, JsonValue]:
    return _object(json_loads(snapshot), name)


def _object(value: JsonValue | None, name: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} snapshot must contain a JSON object")
    return value


def _object_list(
    document: Mapping[str, JsonValue],
    key: str,
    item_name: str,
) -> tuple[dict[str, JsonValue], ...]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a JSON array")
    return tuple(_object(item, item_name) for item in value)


def _string(document: Mapping[str, JsonValue], key: str) -> str:
    return _json_string(document.get(key), key)


def _json_string(value: JsonValue | None, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _optional_string(document: Mapping[str, JsonValue], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    return _json_string(value, key)


def _integer(document: Mapping[str, JsonValue], key: str) -> int:
    value = document.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _boolean(document: Mapping[str, JsonValue], key: str) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _strings(document: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array of strings")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} must be an array of strings")
        values.append(item)
    return tuple(values)


def _ids(document: Mapping[str, JsonValue], key: str) -> tuple[ID, ...]:
    return tuple(ID(value) for value in _strings(document, key))


def _optional_id(document: Mapping[str, JsonValue], key: str) -> ID | None:
    value = _optional_string(document, key)
    return None if value is None else ID(value)


def _optional_datetime(document: Mapping[str, JsonValue], key: str) -> datetime | None:
    value = _optional_string(document, key)
    return None if value is None else parse_utc_datetime(value)


def _format_optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else format_utc_datetime(value)
