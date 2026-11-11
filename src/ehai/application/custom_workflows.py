"""Validate and advance project-defined workflows inside the owning transaction.

The engine never waits on the network. An action step records its invocation intent and
queues a connector call in the same transaction; the connector's own process claims the
call, performs the I/O and reports the result, which the next scheduler tick applies.
A Run executes from the definition snapshot it pinned when it started.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing.exceptions import Unresolvable

from ehai import JsonValue, json_dumps, new_id, parse_utc_datetime
from ehai.application.connector_models import ConnectorAuthorization, ConnectorConnection
from ehai.application.connectors import (
    ConnectorTransaction,
    queue_connector_call,
    validate_connector_schema,
)
from ehai.application.custom_workflow_models import (
    REF_PATTERN,
    ActionStep,
    AgentStep,
    AllCondition,
    AnyCondition,
    CompareCondition,
    Condition,
    ConditionStep,
    EndStep,
    ExistsCondition,
    HumanStep,
    LiteralValue,
    NotCondition,
    RefValue,
    Step,
    TemplateValue,
    Value,
    WaitStep,
    WorkflowDefinitionBody,
    WorkflowDefinitionRevision,
)
from ehai.application.ports import StateConflictError
from ehai.application.workflow_execution_models import (
    WorkflowContractRef,
    WorkflowExecutionRecord,
    WorkflowInvocationRecord,
    WorkflowStepExecution,
    WorkflowWaitRecord,
)
from ehai.application.workflow_models import DecideWorkflowRequest
from ehai.application.workflow_recording import WorkflowRecordStore
from ehai.domain.events import Event, EventType

LIFE_WORKFLOWS = frozenset({"life.capture", "life.review"})
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_REF = re.compile(REF_PATTERN)


class CustomWorkflowTransaction(WorkflowRecordStore, Protocol):
    def execution_records(self, project_id: str) -> list[WorkflowExecutionRecord]: ...
    def definition(self, definition_id: str) -> WorkflowDefinitionRevision: ...
    def definitions(self, project_id: str) -> list[WorkflowDefinitionRevision]: ...
    def save_definition(self, revision: WorkflowDefinitionRevision) -> None: ...
    def waiting_custom_runs(self) -> list[str]: ...
    @property
    def connectors(self) -> ConnectorTransaction: ...
    def emit(self, event: Event) -> None: ...


def is_custom(record: WorkflowExecutionRecord) -> bool:
    return record.definition.name not in LIFE_WORKFLOWS


# Definition validation ---------------------------------------------------------------


def _successors(step: Step) -> list[str]:
    if isinstance(step, ConditionStep):
        return [case.next for case in step.cases] + [step.default]
    if isinstance(step, HumanStep):
        return [step.on_approve, step.on_reject] + (
            [] if step.on_timeout is None else [step.on_timeout]
        )
    if isinstance(step, EndStep):
        return []
    return [step.next]


def _value_refs(value: Value) -> list[str]:
    if isinstance(value, RefValue):
        return [value.ref]
    if isinstance(value, TemplateValue):
        return _PLACEHOLDER.findall(value.template)
    return []


def _condition_refs(condition: Condition) -> list[str]:
    if isinstance(condition, CompareCondition):
        return _value_refs(condition.left) + _value_refs(condition.right)
    if isinstance(condition, ExistsCondition):
        return [condition.ref]
    if isinstance(condition, NotCondition):
        return _condition_refs(condition.condition)
    return [ref for item in condition.conditions for ref in _condition_refs(item)]


def _step_refs(step: Step) -> list[str]:
    values: list[Value] = []
    if isinstance(step, ActionStep):
        values = list(step.inputs.values())
    elif isinstance(step, AgentStep):
        values = [step.instruction]
    elif isinstance(step, HumanStep):
        values = list(step.subject.values())
        if step.grant is not None and step.grant.limit is not None:
            values.append(step.grant.limit.value)
    elif isinstance(step, WaitStep):
        values = [step.until]
    elif isinstance(step, EndStep):
        values = list(step.result.values())
    refs = [ref for value in values for ref in _value_refs(value)]
    if isinstance(step, ConditionStep):
        refs += [ref for case in step.cases for ref in _condition_refs(case.when)]
    return refs


def validate_definition(
    body: WorkflowDefinitionBody, connections: Mapping[str, ConnectorConnection]
) -> None:
    """Refuse a definition that could not run as written; nothing is guessed at run time."""
    if body.name.startswith("life."):
        raise ValueError("Names under life. belong to the fixed workflows")
    problems: list[str] = []
    try:
        validate_connector_schema(body.inputs_schema)
    except ValueError as error:
        problems.append(f"inputs_schema: {error}")
    steps: dict[str, Step] = {}
    for step in body.steps:
        if step.id in steps:
            problems.append(f"duplicate step id {step.id}")
        steps[step.id] = step
    if body.start not in steps:
        problems.append(f"start step {body.start} does not exist")
    for step in body.steps:
        for target in _successors(step):
            if target not in steps:
                problems.append(f"step {step.id} leads to missing step {target}")
    if problems:
        raise ValueError("Invalid workflow definition: " + "; ".join(problems))

    # Depth-first order from start: refuses cycles and finds unreachable steps.
    order: list[str] = []
    state: dict[str, Literal["open", "done"]] = {}

    def visit(node: str) -> None:
        state[node] = "open"
        for target in _successors(steps[node]):
            if state.get(target) == "open":
                problems.append(f"step {node} leads back to {target}; loops are not supported")
            elif target not in state:
                visit(target)
        state[node] = "done"
        order.append(node)

    visit(body.start)
    for step in body.steps:
        if step.id not in state:
            problems.append(f"step {step.id} cannot be reached from start")
    if problems:
        raise ValueError("Invalid workflow definition: " + "; ".join(problems))

    topological = list(reversed(order))
    predecessors: dict[str, list[str]] = {node: [] for node in topological}
    for node in topological:
        for target in set(_successors(steps[node])):
            predecessors[target].append(node)
    # Steps on every path from start: a reference may only read their outputs.
    dominators: dict[str, set[str]] = {}
    for node in topological:
        incoming = [dominators[p] for p in predecessors[node]]
        dominators[node] = (set.intersection(*incoming) if incoming else set()) | {node}
    reachable: dict[str, set[str]] = {}
    for node in order:
        reachable[node] = {node}.union(*(reachable[t] for t in _successors(steps[node])))

    properties = body.inputs_schema.get("properties")
    for node in topological:
        step = steps[node]
        for ref in _step_refs(step):
            if not _REF.fullmatch(ref):
                problems.append(f"step {node} has an invalid reference {{{ref}}}")
                continue
            parts = ref.split(".")
            if parts[0] == "input":
                if len(parts) > 1 and isinstance(properties, dict) and parts[1] not in properties:
                    problems.append(f"step {node} reads undeclared input {parts[1]}")
                continue
            source = parts[1]
            if source == node or source not in dominators[node]:
                problems.append(f"step {node} reads {source}, which does not run before it")
            elif isinstance(steps[source], EndStep):
                problems.append(f"step {node} reads end step {source}")
        if isinstance(step, ActionStep):
            _check_action(step, steps, connections, dominators, reachable, problems)
        elif isinstance(step, AgentStep):
            _check_agent(step, connections, problems)
    if problems:
        raise ValueError("Invalid workflow definition: " + "; ".join(problems))


def agent_tool_name(action: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", action)


def _check_agent(
    step: AgentStep,
    connections: Mapping[str, ConnectorConnection],
    problems: list[str],
) -> None:
    names = {"submit_result"}
    try:
        validate_connector_schema(step.output_schema)
    except ValueError as error:
        problems.append(f"step {step.id} output_schema: {error}")
    for tool in step.tools:
        name = agent_tool_name(tool.action)
        if name in names:
            problems.append(f"step {step.id} action {tool.action}: duplicate or reserved tool name")
        names.add(name)
        connection = connections.get(tool.connector_id)
        action = (
            None
            if connection is None
            else next(
                (
                    a
                    for a in connection.manifest.actions
                    if a.name == tool.action and a.version == tool.action_version
                ),
                None,
            )
        )
        if action is None:
            problems.append(
                f"step {step.id} action {tool.action}: unregistered connector/action/version"
            )
            continue
        if not action.read_only:
            problems.append(f"step {step.id} action {tool.action}: agent tools must be read-only")
        try:
            validate_connector_schema(action.input_schema)
        except ValueError as error:
            problems.append(f"step {step.id} action {tool.action}: {error}")


def _check_action(
    step: ActionStep,
    steps: Mapping[str, Step],
    connections: Mapping[str, ConnectorConnection],
    dominators: Mapping[str, set[str]],
    reachable: Mapping[str, set[str]],
    problems: list[str],
) -> None:
    connection = connections.get(step.connector_id)
    if connection is None:
        problems.append(f"step {step.id} names a connector this project has not registered")
        return
    action = next(
        (
            a
            for a in connection.manifest.actions
            if a.name == step.action and a.version == step.action_version
        ),
        None,
    )
    if action is None:
        problems.append(f"step {step.id} names an action/version the connector does not offer")
        return
    required = action.input_schema.get("required")
    for name in required if isinstance(required, list) else []:
        if isinstance(name, str) and name not in step.inputs:
            problems.append(f"step {step.id} does not provide required input {name}")
    declared = action.input_schema.get("properties")
    if action.input_schema.get("additionalProperties") is False and isinstance(declared, dict):
        for name in step.inputs:
            if name not in declared:
                problems.append(f"step {step.id} provides undeclared input {name}")
    if action.read_only:
        if step.approval_step is not None or step.standing_write:
            problems.append(f"step {step.id} authorizes writes for a read-only action")
        return
    if (step.approval_step is None) == (not step.standing_write):
        problems.append(
            f"step {step.id} writes and needs exactly one of approval_step or standing_write"
        )
        return
    if step.approval_step is None:
        return
    for name, value in step.inputs.items():
        allowed = (
            f"steps.{step.approval_step}.subject",
            f"steps.{step.approval_step}.authorization",
        )
        if not isinstance(value, LiteralValue) and not all(
            any(ref == prefix or ref.startswith(prefix + ".") for prefix in allowed)
            for ref in _value_refs(value)
        ):
            problems.append(
                f"step {step.id} input {name} must bind to {step.approval_step} "
                "subject or authorization, or be a literal value"
            )
    approval = steps.get(step.approval_step)
    if not isinstance(approval, HumanStep):
        problems.append(f"step {step.id} names {step.approval_step}, which is not a human step")
    elif step.approval_step not in dominators[step.id] or any(
        step.id in reachable[exit]
        for exit in (approval.on_reject, approval.on_timeout)
        if exit is not None
    ):
        problems.append(f"step {step.id} can run without approval from {step.approval_step}")


def validate_inputs(schema: dict[str, JsonValue], inputs: dict[str, JsonValue]) -> None:
    if len(json_dumps(inputs).encode()) > 1_000_000:
        raise ValueError("Workflow inputs exceed the 1 MB limit")
    try:
        Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(
            inputs
        )
    except ValidationError as error:
        raise ValueError(f"Workflow inputs do not match inputs_schema: {error.message}") from error
    except Unresolvable as error:
        raise ValueError("Workflow inputs_schema has an unresolvable reference") from error


def definition_digest(body: WorkflowDefinitionBody) -> str:
    return sha256(json_dumps(body.model_dump(mode="json")).encode()).hexdigest()


# Execution ---------------------------------------------------------------------------


class _StepError(Exception):
    """A technical step failure; it fails the Run and never selects a business branch."""


_MISSING = object()


class _Execution:
    """One Run inside one write transaction; ``commit`` saves the Run and emits its view."""

    def __init__(
        self, tx: CustomWorkflowTransaction, record: WorkflowExecutionRecord, now: datetime
    ) -> None:
        if not is_custom(record) or record.definition_snapshot is None:
            raise StateConflictError("This workflow run is not a custom workflow run")
        self.tx, self.record, self.now = tx, record, now
        self.body = WorkflowDefinitionBody.model_validate(record.definition_snapshot["definition"])
        self.steps = {step.id: step for step in self.body.steps}
        self.executions = [
            WorkflowStepExecution.model_validate(s)
            for s in tx.execution_children("step", record.workflow_run_id)
        ]
        self.changed = False

    # Values

    def _outputs(self) -> dict[str, JsonValue]:
        return {
            e.node_id: e.output
            for e in self.executions
            if e.status == "completed" and e.output is not None
        }

    def _lookup(self, path: str) -> object:
        parts = path.split(".")
        current: object
        if parts[0] == "input":
            current, keys = self.record.inputs, parts[1:]
        else:
            current, keys = self._outputs().get(parts[1], _MISSING), parts[2:]
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
                current = current[int(key)]
            else:
                return _MISSING
        return current

    def _ref(self, path: str) -> JsonValue:
        value = self._lookup(path)
        if value is _MISSING:
            raise _StepError(f"Reference {path} has no value")
        return value  # type: ignore[return-value]

    def _value(self, value: Value) -> JsonValue:
        if isinstance(value, LiteralValue):
            return value.value
        if isinstance(value, RefValue):
            return self._ref(value.ref)

        def render(match: re.Match[str]) -> str:
            item = self._ref(match.group(1))
            return item if isinstance(item, str) else json_dumps(item)

        return _PLACEHOLDER.sub(render, value.template)

    def _values(self, values: Mapping[str, Value]) -> dict[str, JsonValue]:
        return {name: self._value(value) for name, value in values.items()}

    def _condition(self, condition: Condition) -> bool:
        if isinstance(condition, ExistsCondition):
            return self._lookup(condition.ref) not in (_MISSING, None)
        if isinstance(condition, NotCondition):
            return not self._condition(condition.condition)
        if isinstance(condition, AllCondition):
            return all(self._condition(item) for item in condition.conditions)
        if isinstance(condition, AnyCondition):
            return any(self._condition(item) for item in condition.conditions)
        left, right = self._value(condition.left), self._value(condition.right)
        numbers = all(isinstance(v, int | float) and not isinstance(v, bool) for v in (left, right))
        if condition.op in ("eq", "ne"):
            # 1 and 1.0 are the same JSON number; everything else compares by its JSON form.
            same = left == right if numbers else json_dumps(left) == json_dumps(right)
            return same if condition.op == "eq" else not same
        if not numbers and not (isinstance(left, str) and isinstance(right, str)):
            raise _StepError(f"Cannot order {json_dumps(left)} and {json_dumps(right)}")
        a, b = left, right
        if condition.op == "lt":
            return a < b  # type: ignore[operator]
        if condition.op == "le":
            return a <= b  # type: ignore[operator]
        if condition.op == "gt":
            return a > b  # type: ignore[operator]
        return a >= b  # type: ignore[operator]

    # Records

    def _save_step(self, execution: WorkflowStepExecution) -> WorkflowStepExecution:
        self.tx.save_execution_record("step", execution.step_execution_id, execution)
        self.executions = [
            e for e in self.executions if e.step_execution_id != execution.step_execution_id
        ] + [execution]
        self.changed = True
        return execution

    def _new_step(
        self,
        step: Step,
        status: Literal["completed", "waiting", "failed"],
        inputs: dict[str, JsonValue],
        output: dict[str, JsonValue] | None,
    ) -> WorkflowStepExecution:
        return self._save_step(
            WorkflowStepExecution(
                step_execution_id=str(new_id()),
                workflow_run_id=self.record.workflow_run_id,
                node_id=step.id,
                execution_index=1,
                contract=WorkflowContractRef(kind="step", name=step.type, version="1"),
                status=status,
                inputs=inputs,
                output=output,
                recorded_at=self.now,
                completed_at=None if status == "waiting" else self.now,
            )
        )

    def _finish_step(
        self,
        execution: WorkflowStepExecution,
        status: Literal["completed", "failed"],
        output: dict[str, JsonValue] | None,
    ) -> None:
        self._save_step(
            execution.model_copy(
                update={"status": status, "output": output, "completed_at": self.now}
            )
        )

    def _finish_run(
        self, status: Literal["completed", "failed"], result: dict[str, JsonValue]
    ) -> None:
        self.record = self.record.model_copy(
            update={"status": status, "result": result, "completed_at": self.now}
        )
        self.changed = True

    def _fail(self, step: Step, execution: WorkflowStepExecution | None, error: str) -> None:
        if execution is None:
            self._new_step(step, "failed", {}, {"error": error})
        else:
            self._finish_step(execution, "failed", {"error": error})
        self._finish_run("failed", {"error": error, "failed_step": step.id})

    def _invocation(self, execution: WorkflowStepExecution) -> WorkflowInvocationRecord:
        return next(
            WorkflowInvocationRecord.model_validate(i)
            for i in self.tx.execution_children("invocation", self.record.workflow_run_id)
            if i["step_execution_id"] == execution.step_execution_id
        )

    def _wait(self, execution: WorkflowStepExecution) -> WorkflowWaitRecord:
        return next(
            WorkflowWaitRecord.model_validate(w)
            for w in self.tx.execution_children("wait", self.record.workflow_run_id)
            if w["step_execution_id"] == execution.step_execution_id
        )

    def _resolve_wait(self, wait: WorkflowWaitRecord, resolution: dict[str, JsonValue]) -> None:
        self.tx.save_execution_record(
            "wait",
            wait.wait_id,
            wait.model_copy(
                update={"status": "resolved", "resolution": resolution, "resolved_at": self.now}
            ),
        )

    def waiting(self) -> WorkflowStepExecution | None:
        return next((e for e in self.executions if e.status == "waiting"), None)

    # Advancing

    def enter(self, node: str) -> None:
        """Run synchronous steps until the Run waits, ends or fails."""
        self.record = self.record.model_copy(update={"status": "running"})
        while True:
            step = self.steps[node]
            try:
                if isinstance(step, ConditionStep):
                    selected, case = step.default, None
                    for index, candidate in enumerate(step.cases):
                        if self._condition(candidate.when):
                            selected, case = candidate.next, index
                            break
                    self._new_step(step, "completed", {}, {"selected": selected, "case": case})
                    node = selected
                    continue
                if isinstance(step, EndStep):
                    result = self._values(step.result)
                    self._new_step(step, "completed", result, {"outcome": step.outcome})
                    self._finish_run("completed", {"outcome": step.outcome, "result": result})
                    return
                if isinstance(step, ActionStep):
                    self._start_action(step)
                    return
                if isinstance(step, AgentStep):
                    self._start_agent(step)
                    return
                if isinstance(step, HumanStep):
                    self._start_human(step)
                    return
                if self._start_wait(step):
                    node = step.next
                    continue
                return
            except _StepError as error:
                self._fail(step, None, str(error))
                return

    def _start_agent(self, step: AgentStep) -> None:
        value = self._value(step.instruction)
        instruction = value if isinstance(value, str) else json_dumps(value)
        execution = self._new_step(step, "waiting", {"instruction": instruction}, None)
        invocation = WorkflowInvocationRecord(
            invocation_id=str(new_id()),
            workflow_run_id=self.record.workflow_run_id,
            step_execution_id=execution.step_execution_id,
            kind="agent",
            action=WorkflowContractRef(kind="action", name="agent", version="1"),
            status="prepared",
            idempotency_key=execution.step_execution_id,
            inputs={"instruction": instruction},
            output=None,
            external_operation_ref=None,
            error=None,
            recorded_at=self.now,
            completed_at=None,
            agent={
                "attempt": 1,
                "harness": None,
                "model": None,
                "started_at": self.now.isoformat(),
                "completed_at": None,
                "tool_calls": [],
            },
        )
        self.tx.save_execution_record("invocation", invocation.invocation_id, invocation)
        wait = WorkflowWaitRecord(
            wait_id=str(new_id()),
            workflow_run_id=self.record.workflow_run_id,
            step_execution_id=execution.step_execution_id,
            kind="agent",
            status="waiting",
            condition={
                "attempt": 1,
                "started_at": self.now.isoformat(),
                "timeout_seconds": step.timeout_seconds,
                "max_tool_calls": step.max_tool_calls,
                "dispatched": False,
            },
            resolution=None,
            recorded_at=self.now,
            resolved_at=None,
        )
        self.tx.save_execution_record("wait", wait.wait_id, wait)
        self.record = self.record.model_copy(update={"status": "waiting"})

    def _start_action(self, step: ActionStep) -> None:
        inputs = self._values(step.inputs)
        connection, _ = self.tx.connectors.connection(step.connector_id)
        if connection.project_id != self.record.project_id:
            raise _StepError("The connector belongs to another project")
        authorization = None
        if step.approval_step is not None:
            raw = self._lookup(f"steps.{step.approval_step}.authorization")
            if isinstance(raw, dict):
                authorization = ConnectorAuthorization.model_validate(
                    {key: value for key, value in raw.items() if key != "approved_at"}
                )
                if authorization.expires_at is not None and authorization.expires_at <= self.now:
                    raise _StepError(
                        f"Authorization for {step.approval_step} expired at "
                        f"{authorization.expires_at.isoformat()}"
                    )
                limit, count = authorization.limit, inputs.get("count")
                if (
                    limit is not None
                    and limit.kind == "count"
                    and isinstance(count, int | float)
                    and not isinstance(count, bool)
                    and count > limit.value
                ):
                    raise _StepError(f"Authorization for {step.approval_step} count limit exceeded")
        execution = self._new_step(step, "waiting", inputs, None)
        invocation = WorkflowInvocationRecord(
            invocation_id=str(new_id()),
            workflow_run_id=self.record.workflow_run_id,
            step_execution_id=execution.step_execution_id,
            kind="connector",
            action=WorkflowContractRef(
                kind="action",
                name=f"{connection.manifest.connector_type}.{step.action}",
                version=step.action_version,
            ),
            status="prepared",
            idempotency_key=execution.step_execution_id,
            inputs=inputs,
            output=None,
            external_operation_ref=None,
            error=None,
            recorded_at=self.now,
            completed_at=None,
        )
        # Intent first, in the same transaction that queues the call for the connector.
        self.tx.save_execution_record("invocation", invocation.invocation_id, invocation)
        try:
            call = queue_connector_call(
                self.tx.connectors,
                step.connector_id,
                step.action,
                step.action_version,
                inputs,
                authorize_write=step.approval_step is not None or step.standing_write,
                authorization=authorization,
            )
        except (ValueError, StateConflictError) as error:
            # The connector changed since the definition was saved; this Run cannot go on.
            self._fail_invocation(invocation, str(error))
            self._fail(step, execution, str(error))
            return
        self.tx.save_execution_record(
            "invocation",
            invocation.invocation_id,
            invocation.model_copy(update={"connector_call_id": call.call_id}),
        )
        self.record = self.record.model_copy(update={"status": "waiting"})

    def _fail_invocation(self, invocation: WorkflowInvocationRecord, error: str) -> None:
        self.tx.save_execution_record(
            "invocation",
            invocation.invocation_id,
            invocation.model_copy(
                update={"status": "failed", "error": error, "completed_at": self.now}
            ),
        )

    def _start_human(self, step: HumanStep) -> None:
        subject = self._values(step.subject)
        grant: dict[str, JsonValue] | None = None
        if step.grant is not None:
            limit: dict[str, JsonValue] | None = None
            if step.grant.limit is not None:
                value = self._value(step.grant.limit.value)
                if (
                    not isinstance(value, int | float)
                    or isinstance(value, bool)
                    or (isinstance(value, float) and not math.isfinite(value))
                ):
                    raise _StepError(f"Grant limit value for {step.id} must be a number")
                limit = {
                    "kind": step.grant.limit.kind,
                    "value": value,
                    "unit": step.grant.limit.unit,
                }
            grant = {"limit": limit, "valid_seconds": step.grant.valid_seconds}
        execution = self._new_step(step, "waiting", subject, None)
        deadline = (
            None
            if step.timeout_seconds is None
            else self.now + timedelta(seconds=step.timeout_seconds)
        )
        wait = WorkflowWaitRecord(
            wait_id=str(new_id()),
            workflow_run_id=self.record.workflow_run_id,
            step_execution_id=execution.step_execution_id,
            kind="human",
            status="waiting",
            condition={
                "question": step.question,
                "subject": subject,
                "subject_sha256": sha256(json_dumps(subject).encode()).hexdigest(),
                "deadline": None if deadline is None else deadline.isoformat(),
                **({} if grant is None else {"grant": grant}),
            },
            resolution=None,
            recorded_at=self.now,
            resolved_at=None,
        )
        self.tx.save_execution_record("wait", wait.wait_id, wait)
        self.record = self.record.model_copy(update={"status": "waiting"})

    def _start_wait(self, step: WaitStep) -> bool:
        """Record a time wait; return True when it is already due and resolved."""
        until = self._value(step.until)
        try:
            due = parse_utc_datetime(until) if isinstance(until, str) else None
        except ValueError:
            due = None
        if due is None:
            raise _StepError("Wait time must be an ISO 8601 time with an offset")
        due += timedelta(seconds=step.offset_seconds)
        execution = self._new_step(step, "waiting", {"until": due.isoformat()}, None)
        wait = WorkflowWaitRecord(
            wait_id=str(new_id()),
            workflow_run_id=self.record.workflow_run_id,
            step_execution_id=execution.step_execution_id,
            kind="time",
            status="waiting",
            condition={"until": due.isoformat()},
            resolution=None,
            recorded_at=self.now,
            resolved_at=None,
        )
        self.tx.save_execution_record("wait", wait.wait_id, wait)
        if due > self.now:
            self.record = self.record.model_copy(update={"status": "waiting"})
            return False
        self._resolve_wait(wait, {"fired_at": self.now.isoformat()})
        self._finish_step(
            execution, "completed", {"until": due.isoformat(), "fired_at": self.now.isoformat()}
        )
        return True

    def poll(self, *, apply: bool) -> bool:
        """Apply a connector result, an elapsed time or a human timeout to the waiting step.

        With ``apply`` false nothing is written; the answer says whether a write would be.
        """
        if self.record.status != "waiting":
            return False
        execution = self.waiting()
        if execution is None:
            return False
        step = self.steps[execution.node_id]
        if isinstance(step, ActionStep):
            invocation = self._invocation(execution)
            if invocation.connector_call_id is None:
                return False
            call, _, _ = self.tx.connectors.call(invocation.connector_call_id)
            if call.status in ("queued", "claimed"):
                return False
            if call.status == "unknown":
                if invocation.status == "unknown":
                    return False
                if apply:
                    self.tx.save_execution_record(
                        "invocation",
                        invocation.invocation_id,
                        invocation.model_copy(update={"status": "unknown", "error": call.error}),
                    )
                    self.changed = True
                return True
            if not apply:
                return True
            if call.status == "failed":
                error = call.error or "Connector call failed"
                self._fail_invocation(invocation, error)
                self._fail(step, execution, error)
                return True
            self.tx.save_execution_record(
                "invocation",
                invocation.invocation_id,
                invocation.model_copy(
                    update={
                        "status": "completed",
                        "output": call.output,
                        "external_operation_ref": call.external_operation_ref,
                        "error": None,
                        "completed_at": self.now,
                    }
                ),
            )
            self._finish_step(execution, "completed", call.output)
            self.enter(step.next)
            return True
        wait = self._wait(execution)
        if isinstance(step, HumanStep):
            deadline = wait.condition.get("deadline")
            if not isinstance(deadline, str) or parse_utc_datetime(deadline) > self.now:
                return False
            if apply:
                self._resolve_wait(wait, {"decision": "timeout"})
                self._finish_step(
                    execution, "completed", {"decision": "timeout", "subject": execution.inputs}
                )
                assert step.on_timeout is not None
                self.enter(step.on_timeout)
            return True
        if isinstance(step, WaitStep):
            until = wait.condition.get("until")
            if not isinstance(until, str) or parse_utc_datetime(until) > self.now:
                return False
            if apply:
                self._resolve_wait(wait, {"fired_at": self.now.isoformat()})
                self._finish_step(
                    execution, "completed", {"until": until, "fired_at": self.now.isoformat()}
                )
                self.enter(step.next)
            return True
        return False

    def decide(self, request: DecideWorkflowRequest) -> None:
        if self.record.version != request.expected_version:
            raise StateConflictError(
                f"Expected version {request.expected_version}; "
                f"current version is {self.record.version}"
            )
        execution = self.waiting()
        step = None if execution is None else self.steps[execution.node_id]
        if execution is None or not isinstance(step, HumanStep):
            raise StateConflictError("Workflow run is not waiting for a human decision")
        decision: dict[str, JsonValue] = {
            "decision": request.decision,
            "actor": request.actor,
            "reason": request.reason,
            "decided_at": self.now.isoformat(),
        }
        wait = self._wait(execution)
        if request.decision == "approve":
            grant = wait.condition.get("grant")
            validity = grant.get("valid_seconds") if isinstance(grant, dict) else None
            expires = self.now + timedelta(seconds=validity) if isinstance(validity, int) else None
            decision["authorization"] = {
                "authorization_id": str(new_id()),
                "subject_sha256": wait.condition.get("subject_sha256")
                or sha256(json_dumps(execution.inputs).encode()).hexdigest(),
                "limit": grant.get("limit") if isinstance(grant, dict) else None,
                "approved_at": self.now.isoformat(),
                "expires_at": None if expires is None else expires.isoformat(),
            }
        self._resolve_wait(wait, decision)
        self._finish_step(execution, "completed", {**decision, "subject": execution.inputs})
        self.enter(step.on_approve if request.decision == "approve" else step.on_reject)

    def commit(self, *, created: bool = False) -> WorkflowExecutionRecord:
        if not created:
            self.record = self.record.model_copy(update={"version": self.record.version + 1})
        self.tx.save_execution_record("run", self.record.workflow_run_id, self.record)
        run_id = self.record.workflow_run_id
        self.tx.emit(
            Event(
                type=EventType.WORKFLOW_EXECUTION_CHANGED,
                correlation_id=run_id,
                payload={
                    "run": self.record.model_dump(mode="json"),
                    "steps": [e.model_dump(mode="json") for e in self.executions],
                    "invocations": list(self.tx.execution_children("invocation", run_id)),
                    "waits": list(self.tx.execution_children("wait", run_id)),
                },
            )
        )
        return self.record


def start_custom_run(
    tx: CustomWorkflowTransaction,
    revision: WorkflowDefinitionRevision,
    inputs: dict[str, JsonValue],
    trigger: dict[str, JsonValue],
    now: datetime,
) -> WorkflowExecutionRecord:
    validate_inputs(revision.definition.inputs_schema, inputs)
    record = WorkflowExecutionRecord(
        workflow_run_id=str(new_id()),
        project_id=revision.project_id,
        definition=WorkflowContractRef(
            kind="workflow",
            name=revision.name,
            version=str(revision.revision),
            digest=revision.digest,
        ),
        definition_snapshot={
            "definition_id": revision.definition_id,
            "definition": revision.definition.model_dump(mode="json"),
        },
        status="running",
        version=1,
        inputs=inputs,
        result={},
        trigger=trigger,
        recording_origin="native",
        created_at=now,
        completed_at=None,
    )
    tx.save_execution_record("run", record.workflow_run_id, record)
    execution = _Execution(tx, record, now)
    execution.enter(revision.definition.start)
    return execution.commit(created=True)


def poll_custom_run(
    tx: CustomWorkflowTransaction, run_id: str, now: datetime, *, apply: bool
) -> bool:
    record = tx.execution_record(run_id)
    if record is None or not is_custom(record):
        return False
    execution = _Execution(tx, record, now)
    changed = execution.poll(apply=apply)
    if apply and execution.changed:
        execution.commit()
    return changed


def decide_custom_run(
    tx: CustomWorkflowTransaction,
    record: WorkflowExecutionRecord,
    request: DecideWorkflowRequest,
    now: datetime,
) -> WorkflowExecutionRecord:
    execution = _Execution(tx, record, now)
    execution.decide(request)
    return execution.commit()


def pending_agent(
    tx: CustomWorkflowTransaction, run_id: str, now: datetime
) -> tuple[AgentStep, WorkflowStepExecution, WorkflowWaitRecord, WorkflowInvocationRecord] | None:
    record = tx.execution_record(run_id)
    if record is None or record.status != "waiting" or not is_custom(record):
        return None
    engine = _Execution(tx, record, now)
    execution = engine.waiting()
    if execution is None:
        return None
    step = engine.steps[execution.node_id]
    if not isinstance(step, AgentStep):
        return None
    wait = engine._wait(execution)
    invocations = [
        WorkflowInvocationRecord.model_validate(i)
        for i in tx.execution_children("invocation", run_id)
        if i["step_execution_id"] == execution.step_execution_id and i["kind"] == "agent"
    ]
    invocation = next(
        i
        for i in invocations
        if i.agent is not None and i.agent.get("attempt") == wait.condition["attempt"]
    )
    return step, execution, wait, invocation


def finish_agent(
    tx: CustomWorkflowTransaction,
    run_id: str,
    invocation_id: str,
    output: dict[str, JsonValue] | None,
    error: str | None,
    now: datetime,
) -> None:
    pending = pending_agent(tx, run_id, now)
    if pending is None:
        return
    step, execution, wait, invocation = pending
    if invocation.invocation_id != invocation_id or invocation.status != "prepared":
        return
    assert invocation.agent is not None
    tx.save_execution_record(
        "invocation",
        invocation_id,
        invocation.model_copy(
            update={
                "status": "completed" if error is None else "failed",
                "output": output,
                "error": error,
                "completed_at": now,
                "agent": {**invocation.agent, "completed_at": now.isoformat()},
            }
        ),
    )
    record = tx.execution_record(run_id)
    assert record is not None
    engine = _Execution(tx, record, now)
    engine._resolve_wait(wait, {"attempt": wait.condition["attempt"], "error": error})
    if error is not None:
        engine._fail(step, execution, error)
    else:
        engine._finish_step(execution, "completed", output)
        engine.enter(step.next)
    engine.commit()


def claim_agent(
    tx: CustomWorkflowTransaction,
    run_id: str,
    harness: str | None,
    model: str | None,
    now: datetime,
) -> tuple[AgentStep, WorkflowInvocationRecord] | None:
    pending = pending_agent(tx, run_id, now)
    if pending is None:
        return None
    step, _, wait, invocation = pending
    if wait.condition.get("dispatched"):
        if wait.condition["attempt"] == 2:
            finish_agent(
                tx, run_id, invocation.invocation_id, None, "Agent step interrupted twice", now
            )
            return None
        tx.save_execution_record(
            "invocation",
            invocation.invocation_id,
            invocation.model_copy(
                update={
                    "status": "failed",
                    "error": "Agent step interrupted",
                    "completed_at": now,
                    "agent": {**(invocation.agent or {}), "completed_at": now.isoformat()},
                }
            ),
        )
        invocation = invocation.model_copy(
            update={
                "invocation_id": str(new_id()),
                "idempotency_key": str(new_id()),
                "recorded_at": now,
                "agent": {"attempt": 2, "tool_calls": []},
            }
        )
    attempt = (invocation.agent or {}).get("attempt", 1)
    invocation = invocation.model_copy(
        update={
            "agent": {
                **(invocation.agent or {}),
                "harness": harness,
                "model": model,
                "started_at": now.isoformat(),
                "completed_at": None,
            }
        }
    )
    tx.save_execution_record("invocation", invocation.invocation_id, invocation)
    tx.save_execution_record(
        "wait",
        wait.wait_id,
        wait.model_copy(
            update={
                "condition": {
                    **wait.condition,
                    "attempt": attempt,
                    "dispatched": True,
                    "started_at": now.isoformat(),
                }
            }
        ),
    )
    record = tx.execution_record(run_id)
    assert record is not None
    _Execution(tx, record, now).commit()
    return step, invocation


def record_agent_call(
    tx: CustomWorkflowTransaction,
    run_id: str,
    invocation_id: str,
    calls: list[JsonValue],
    now: datetime,
) -> None:
    pending = pending_agent(tx, run_id, now)
    if pending is None or pending[3].invocation_id != invocation_id:
        return
    invocation = pending[3]
    tx.save_execution_record(
        "invocation",
        invocation_id,
        invocation.model_copy(
            update={
                "agent": {**(invocation.agent or {}), "tool_calls": calls},
            }
        ),
    )
    record = tx.execution_record(run_id)
    assert record is not None
    _Execution(tx, record, now).commit()
