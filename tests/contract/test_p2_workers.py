from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ehai import JsonValue, new_id
from ehai.application import (
    OPENAI_CREDENTIAL_REF,
    AttemptActivity,
    AttemptExecutionKind,
    Connector,
    SessionPolicy,
    normalize_capabilities,
    supports_capabilities,
)
from ehai.application.async_runtime import (
    ConnectorExecution,
    ConnectorRecoveryRequest,
    ConnectorStartRequest,
    WorkerEvent,
    WorkerEventType,
)
from ehai.application.workers import WorkerRequest
from ehai.domain import (
    Attempt,
    AttemptStatus,
    CompletionContract,
    PlanNode,
    Run,
)
from ehai.infrastructure.workers import CodexAppServerConnector

type JsonObject = dict[str, JsonValue]


class _ContractConnector:
    async def start(self, request: str) -> str:
        return request

    async def events(
        self,
        execution: str,
        *,
        after_cursor: str | None = None,
    ) -> AsyncIterator[str]:
        del execution, after_cursor
        if False:
            yield ""

    async def inspect(self, execution: str) -> str:
        return execution

    async def cancel(self, execution: str) -> None:
        del execution

    async def recover(self, request: str) -> str:
        return request


def test_p2_execution_contract_is_fixed_and_unknown_capabilities_fail_closed() -> None:
    offered = normalize_capabilities(("workspace.read", "workspace.write"))

    assert supports_capabilities(offered=offered, required=("workspace.read",))
    assert not supports_capabilities(offered=offered, required=("provider.unknown",))
    with pytest.raises(ValueError, match="capabilities"):
        normalize_capabilities(("Workspace Read",))

    assert tuple(policy.value for policy in SessionPolicy) == ("new", "reuse", "fork")
    with pytest.raises(ValueError):
        SessionPolicy("unknown")

    assert tuple(kind.value for kind in AttemptExecutionKind) == (
        "builtin_turn",
        "external_execution",
    )
    assert tuple(activity.value for activity in AttemptActivity) == (
        "queued",
        "running",
        "waiting",
        "stalled",
    )
    assert AttemptActivity.RUNNING.value == AttemptStatus.RUNNING.value
    assert "queued" not in {status.value for status in AttemptStatus}
    assert isinstance(_ContractConnector(), Connector)
    assert OPENAI_CREDENTIAL_REF == "env:OPENAI_API_KEY"


class _FakeAppServerTransport:
    def __init__(
        self,
        *,
        threads: dict[str, JsonObject] | None = None,
        reissue_waiting_on_resume: bool = False,
    ) -> None:
        self.incoming: asyncio.Queue[JsonObject | None] = asyncio.Queue()
        self.sent: list[JsonObject] = []
        self.threads = {} if threads is None else threads
        self.reissue_waiting_on_resume = reissue_waiting_on_resume
        self.thread_sequence = len(self.threads)
        self.turn_sequence = sum(
            len(thread.get("turns", []))
            for thread in self.threads.values()
            if isinstance(thread.get("turns"), list)
        )

    async def open(self) -> None:
        return None

    async def send(self, message: JsonObject) -> None:
        self.sent.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return
        if method is None:
            return
        if method == "initialize":
            self.emit({"id": request_id, "result": {"userAgent": "fake-app-server"}})
            return
        params = message.get("params")
        assert isinstance(params, dict)
        if method == "thread/start":
            self.thread_sequence += 1
            thread_id = f"thread-{self.thread_sequence}"
            thread: JsonObject = {"id": thread_id, "turns": []}
            self.threads[thread_id] = thread
            self.emit({"id": request_id, "result": {"thread": thread}})
            return
        if method == "turn/start":
            self.turn_sequence += 1
            turn_id = f"turn-{self.turn_sequence}"
            thread_id = _fake_text(params["threadId"])
            turn: JsonObject = {
                "id": turn_id,
                "status": "inProgress",
                "items": [],
                "error": None,
            }
            turns = self.threads[thread_id]["turns"]
            assert isinstance(turns, list)
            turns.append(turn)
            self.emit({"id": request_id, "result": {"turn": turn}})
            return
        if method == "turn/interrupt":
            thread_id = _fake_text(params["threadId"])
            turn_id = _fake_text(params["turnId"])
            self.emit({"id": request_id, "result": {}})
            self.complete_turn(thread_id, turn_id, status="interrupted")
            return
        if method in {"thread/read", "thread/resume"}:
            thread_id = _fake_text(params["threadId"])
            self.emit({"id": request_id, "result": {"thread": self.threads[thread_id]}})
            if method == "thread/resume" and self.reissue_waiting_on_resume:
                turns = self.threads[thread_id]["turns"]
                assert isinstance(turns, list) and turns
                turn = turns[-1]
                assert isinstance(turn, dict)
                self.emit(
                    _waiting_request(
                        request_id=200,
                        thread_id=thread_id,
                        turn_id=_fake_text(turn["id"]),
                    )
                )
            return
        if method == "thread/fork":
            source_id = _fake_text(params["threadId"])
            self.thread_sequence += 1
            thread_id = f"thread-{self.thread_sequence}"
            thread = {"id": thread_id, "turns": [], "forkedFromId": source_id}
            self.threads[thread_id] = thread
            self.emit({"id": request_id, "result": {"thread": thread}})

    async def receive(self) -> JsonObject | None:
        return await self.incoming.get()

    async def close(self) -> None:
        self.incoming.put_nowait(None)

    def emit(self, message: JsonObject) -> None:
        self.incoming.put_nowait(message)

    def disconnect(self) -> None:
        self.incoming.put_nowait(None)

    def complete_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        status: str = "completed",
        candidate: str | None = None,
    ) -> None:
        turns = self.threads[thread_id]["turns"]
        assert isinstance(turns, list)
        turn = next(item for item in turns if isinstance(item, dict) and item.get("id") == turn_id)
        if candidate is not None:
            item: JsonObject = {
                "type": "agentMessage",
                "id": f"item-{turn_id}",
                "text": candidate,
                "phase": "final_answer",
            }
            items = turn["items"]
            assert isinstance(items, list)
            items.append(item)
            self.emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": item,
                        "completedAtMs": 1,
                    },
                }
            )
        turn["status"] = status
        self.emit(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": turn},
            }
        )


def test_codex_app_server_demultiplexes_two_threads_and_interrupts_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        server = _FakeAppServerTransport()
        connector = CodexAppServerConnector(
            workspace=tmp_path,
            model="test-model",
            transport_factory=lambda: server,
        )
        first_request = ConnectorStartRequest(_worker_request("first"))
        second_request = ConnectorStartRequest(_worker_request("second"))
        first, second = await asyncio.gather(
            connector.start(first_request), connector.start(second_request)
        )
        assert first.provider_session_id != second.provider_session_id
        assert first.provider_execution_id != second.provider_execution_id

        first_events = asyncio.create_task(_collect_events(connector, first))
        second_events = asyncio.create_task(_collect_events(connector, second))
        server.emit(_progress(first, "item-first"))
        server.emit(_progress(second, "item-second"))
        server.complete_turn(
            first.provider_session_id,
            first.provider_execution_id,
            candidate=_candidate("first"),
        )
        await connector.cancel(second)
        observed_first, observed_second = await asyncio.gather(first_events, second_events)

        assert [event.attempt_id for event in observed_first] == [first.attempt_id] * len(
            observed_first
        )
        assert [event.attempt_id for event in observed_second] == [second.attempt_id] * len(
            observed_second
        )
        assert observed_first[-2].type is WorkerEventType.CANDIDATE
        assert observed_first[-1].type is WorkerEventType.COMPLETED
        assert observed_second[-1].type is WorkerEventType.FAILED
        interrupt = next(
            message for message in server.sent if message.get("method") == "turn/interrupt"
        )
        assert interrupt["params"] == {
            "threadId": second.provider_session_id,
            "turnId": second.provider_execution_id,
        }
        await connector.close()

    asyncio.run(scenario())


def test_codex_app_server_resumes_original_turn_and_preserves_waiting_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first_server = _FakeAppServerTransport()
        second_server = _FakeAppServerTransport(
            threads=first_server.threads,
            reissue_waiting_on_resume=True,
        )
        transports = iter((first_server, second_server))
        connector = CodexAppServerConnector(
            workspace=tmp_path,
            model="test-model",
            transport_factory=lambda: next(transports),
        )
        execution = await connector.start(ConnectorStartRequest(_worker_request("resume")))
        second_server.thread_sequence = first_server.thread_sequence
        second_server.turn_sequence = first_server.turn_sequence
        events = connector.events(execution).__aiter__()
        first_server.emit(
            _waiting_request(
                request_id=99,
                thread_id=execution.provider_session_id,
                turn_id=execution.provider_execution_id,
            )
        )
        initial_waiting = await asyncio.wait_for(anext(events), 1)
        assert initial_waiting.type is WorkerEventType.WAITING
        assert connector.pending_requests(execution)[0].request_id == 99

        first_server.disconnect()
        for _ in range(20):
            if await connector.inspect(execution) is AttemptActivity.STALLED:
                break
            await asyncio.sleep(0)
        recovered = await connector.recover(
            ConnectorRecoveryRequest(
                execution.attempt_id,
                execution.provider_session_id,
                execution.provider_execution_id,
                initial_waiting.cursor,
            )
        )
        assert recovered == execution
        resumed_waiting = await asyncio.wait_for(anext(events), 1)
        assert resumed_waiting.type is WorkerEventType.WAITING
        pending = connector.pending_requests(execution)
        assert len(pending) == 1 and pending[0].request_id == 200
        await connector.resolve_request(200, {"decision": "decline"})
        assert connector.pending_requests(execution) == ()
        assert any(
            message.get("id") == 200 and message.get("result") == {"decision": "decline"}
            for message in second_server.sent
        )
        assert not any(message.get("method") == "turn/start" for message in second_server.sent)
        fork = await connector.fork_thread(execution.provider_session_id)
        assert fork["forkedFromId"] == execution.provider_session_id
        await connector.close()

    asyncio.run(scenario())


async def _collect_events(
    connector: CodexAppServerConnector,
    execution: ConnectorExecution,
) -> list[WorkerEvent]:
    events: list[WorkerEvent] = []
    async for event in connector.events(execution):
        events.append(event)
    return events


def _worker_request(label: str) -> WorkerRequest:
    now = datetime(2026, 9, 2, tzinfo=UTC)
    goal_id = new_id()
    run = Run(goal_id, new_id(), created_at=now).start(at=now)
    check_id = new_id()
    contract = CompletionContract.draft(
        goal_id,
        ("artifact:non-empty",),
        (check_id,),
        created_at=now,
    ).confirm(confirmed_at=now)
    node = (
        PlanNode(
            new_id(),
            label,
            f"return candidate {label}",
            required_check_ids=(check_id,),
        )
        .mark_ready()
        .start()
    )
    attempt = Attempt(run.run_id, node.plan_node_id, 1, created_at=now).start(at=now)
    return WorkerRequest(
        run=run,
        attempt=attempt,
        plan_node=node,
        completion_contract=contract,
        context={},
    )


def _candidate(label: str) -> str:
    return (
        '{"artifacts":[{"content":"'
        + label
        + '","kind":"candidate","media_type":"text/plain","name":"result.txt"}],'
        + '"summary":"done"}'
    )


def _progress(execution: ConnectorExecution, item_id: str) -> JsonObject:
    thread_id = execution.provider_session_id
    turn_id = execution.provider_execution_id
    return {
        "method": "item/started",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {"type": "commandExecution", "id": item_id},
            "startedAtMs": 1,
        },
    }


def _waiting_request(*, request_id: int, thread_id: str, turn_id: str) -> JsonObject:
    return {
        "id": request_id,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "itemId": f"approval-{request_id}",
            "startedAtMs": 1,
            "command": "test command",
        },
    }


def _fake_text(value: object) -> str:
    assert isinstance(value, str)
    return value
