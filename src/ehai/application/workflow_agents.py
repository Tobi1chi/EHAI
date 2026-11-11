"""Host-local admission and durable receipts for bounded workflow delegations."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from ehai import JsonValue, json_dumps, utc_now
from ehai.application.agent_contracts import RecoverableToolError
from ehai.application.agent_delegation import AgentDelegation
from ehai.application.connector_models import ConnectorAction
from ehai.application.connectors import queue_connector_call
from ehai.application.custom_workflow_models import AgentStep
from ehai.application.custom_workflows import (
    agent_tool_name,
    claim_agent,
    finish_agent,
    pending_agent,
    record_agent_call,
    validate_inputs,
)
from ehai.application.sanitization import redact_sensitive_text, sanitize_json_object
from ehai.application.workflow_execution_models import WorkflowInvocationRecord

if TYPE_CHECKING:
    from ehai.application.workflows import WorkflowStore


class WorkflowAgents:
    def __init__(self, store: WorkflowStore, delegation: AgentDelegation | None) -> None:
        self.store, self.delegation = store, delegation
        self.tasks: dict[str, tuple[str, asyncio.Task[None]]] = {}

    async def tick(self) -> None:
        # This method and its task registry belong only to the scheduler's event loop.
        for run_id, (invocation_id, task) in list(self.tasks.items()):
            if task.done():
                await asyncio.gather(task, return_exceptions=True)
                del self.tasks[run_id]
                continue
            with self.store.transaction(write=False) as tx:
                pending = pending_agent(tx, run_id, utc_now())
            if pending is None or pending[3].invocation_id != invocation_id:
                task.cancel()  # Only this run's role, never a sibling's.
        with self.store.transaction(write=False) as tx:
            waiting = tx.waiting_custom_runs()
        for run_id in waiting:
            if len(self.tasks) >= 2:
                break
            if run_id in self.tasks:
                continue
            delegation = self.delegation
            with self.store.transaction(write=True) as tx:
                claimed = claim_agent(
                    tx,
                    run_id,
                    None if delegation is None else delegation.harness_kind,
                    None if delegation is None else delegation.model,
                    utc_now(),
                )
                if claimed is None:
                    continue
                step, invocation = claimed
                if delegation is None:
                    finish_agent(
                        tx,
                        run_id,
                        invocation.invocation_id,
                        None,
                        "Agent delegation is not configured on this host",
                        utc_now(),
                    )
                    continue
            task = asyncio.create_task(self._run(run_id, step, invocation, delegation))
            self.tasks[run_id] = (invocation.invocation_id, task)

    async def close(self) -> None:
        tasks = [task for _, task in self.tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def _run(
        self,
        run_id: str,
        step: AgentStep,
        invocation: WorkflowInvocationRecord,
        delegation: AgentDelegation,
    ) -> None:
        calls: list[JsonValue] = []
        count = 0
        attempt = int(str((invocation.agent or {}).get("attempt", 1)))

        def persist() -> None:
            with self.store.transaction(write=True) as tx:
                record_agent_call(tx, run_id, invocation.invocation_id, calls, utc_now())

        async def query(
            name: str, inputs: dict[str, JsonValue], refusal_reason: str | None
        ) -> JsonValue:
            nonlocal count
            count += 1
            index = count
            tool = next(t for t in step.tools if agent_tool_name(t.action) == name)
            safe, _ = sanitize_json_object(inputs, max_bytes=1_000_000)
            summary: dict[str, JsonValue] = {
                key: (value if isinstance(value, str) else json_dumps(value))[:200]
                for key, value in safe.items()
            }
            entry: dict[str, JsonValue] = {
                "tool": name,
                "action": tool.action,
                "inputs": summary,
                "connector_call_id": None,
                "status": "refused",
                "duration_ms": 0,
            }
            calls.append(entry)
            started = monotonic()
            try:
                if index > step.max_tool_calls:
                    raise RecoverableToolError("limit", "Query limit reached; submit_result now")
                if refusal_reason is not None:
                    raise RecoverableToolError("invalid_arguments", refusal_reason)
                validate_inputs(actions[name].input_schema, inputs)
                with self.store.transaction(write=True) as tx:
                    pending = pending_agent(tx, run_id, utc_now())
                    if pending is None or pending[3].invocation_id != invocation.invocation_id:
                        raise asyncio.CancelledError
                    connection, _ = tx.connectors.connection(tool.connector_id)
                    record = tx.execution_record(run_id)
                    assert record is not None
                    current = next(
                        (
                            a
                            for a in connection.manifest.actions
                            if a.name == tool.action and a.version == tool.action_version
                        ),
                        None,
                    )
                    if (
                        connection.project_id != record.project_id
                        or current is None
                        or not current.read_only
                    ):
                        raise ValueError(
                            "Agent action is no longer a registered project read-only action"
                        )
                    call = queue_connector_call(
                        tx.connectors,
                        tool.connector_id,
                        tool.action,
                        tool.action_version,
                        inputs,
                        authorize_write=False,
                        idempotency_key=f"{invocation.invocation_id}.{attempt}.{index}",
                    )
                    entry["connector_call_id"] = call.call_id
                    entry["status"] = "timeout"
                    record_agent_call(tx, run_id, invocation.invocation_id, calls, utc_now())
                deadline = monotonic() + 60
                while True:
                    with self.store.transaction(write=False) as tx:
                        call = tx.connectors.call(call.call_id)[0]
                    if call.status == "completed":
                        entry["status"] = "completed"
                        text = json_dumps(call.output).encode()
                        if len(text) > 24 * 1024:
                            return (
                                text[: 24 * 1024].decode("utf-8", errors="ignore")
                                + "\n[Truncated to 24 KB]"
                            )
                        return call.output
                    if call.status in ("failed", "unknown"):
                        entry["status"] = "failed"
                        raise RecoverableToolError(
                            "connector_failed",
                            redact_sensitive_text(call.error or "Connector call failed")[:200],
                        )
                    if monotonic() >= deadline:
                        entry["status"] = "timeout"
                        raise RecoverableToolError(
                            "connector_timeout", "The connector did not answer within 60 s"
                        )
                    await asyncio.sleep(0.5)
            except RecoverableToolError as error:
                entry["error"] = redact_sensitive_text(error.safe_message)[:500]
                raise
            except ValueError as error:
                entry["error"] = redact_sensitive_text(str(error))[:500]
                raise RecoverableToolError(
                    "invalid_query", redact_sensitive_text(str(error))[:500]
                ) from error
            finally:
                entry["duration_ms"] = int((monotonic() - started) * 1000)
                persist()

        output = None
        error = None
        actions: dict[str, ConnectorAction] = {}
        try:
            with self.store.transaction(write=False) as tx:
                for tool in step.tools:
                    connection, _ = tx.connectors.connection(tool.connector_id)
                    action = next(
                        a
                        for a in connection.manifest.actions
                        if a.name == tool.action and a.version == tool.action_version
                    )
                    if not action.read_only:
                        raise ValueError("Agent tools must be read-only")
                    actions[agent_tool_name(tool.action)] = action
            output = await asyncio.wait_for(
                delegation.run(
                    run_id=run_id,
                    invocation_id=invocation.invocation_id,
                    attempt=attempt,
                    step=step,
                    instruction=str(invocation.inputs["instruction"]),
                    actions=actions,
                    query=query,
                ),
                timeout=step.timeout_seconds,
            )
            validate_inputs(step.output_schema, output)
        except asyncio.CancelledError:
            # Leave the durable attempt interrupted for the next host to restart once.
            raise
        except TimeoutError:
            error = f"Agent step timed out after {step.timeout_seconds} s"
        except Exception as failure:
            error = (
                "Agent harness failed: "
                + redact_sensitive_text(str(failure) or type(failure).__name__)[:500]
            )
            if str(failure) == "Agent did not submit a result":
                error = str(failure)
        with self.store.transaction(write=True) as tx:
            finish_agent(
                tx,
                run_id,
                invocation.invocation_id,
                output if error is None else None,
                error,
                utc_now(),
            )
