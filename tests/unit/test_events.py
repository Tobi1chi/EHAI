from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from ehai.domain.events import EVENT_SCHEMA_VERSION, Event, EventType

CORRELATION_ID = "0f348af4-0184-4715-81f7-2f7c58123f23"
EVENT_ID = "a31a165a-a7a1-42de-bc40-4cef880661f9"
RUN_ID = "bdf405f8-7f84-48e2-b128-7479c3b9b7a7"


def test_pre_run_event_has_unique_id_and_explicit_null_run_id() -> None:
    first = Event(
        type=EventType.PROJECT_CREATED,
        correlation_id=CORRELATION_ID,
        payload={"project_id": "project-1"},
    )
    second = Event(
        type=EventType.GOAL_CREATED,
        correlation_id=CORRELATION_ID,
        payload={"goal_id": "goal-1"},
    )

    assert UUID(first.id).version == 4
    assert first.id != second.id
    assert first.occurred_at.tzinfo is UTC
    assert first.run_id is None
    assert first.to_dict()["run_id"] is None


def test_event_is_immutable_including_its_payload_snapshot() -> None:
    source_payload = {"evidence": ["first"]}
    event = Event(
        type=EventType.CHECK_PASSED,
        correlation_id=CORRELATION_ID,
        run_id=RUN_ID,
        payload=source_payload,
    )

    source_payload["evidence"].append("later")
    returned_payload = event.payload
    evidence = returned_payload["evidence"]
    assert isinstance(evidence, list)
    evidence.append("also later")

    assert event.payload == {"evidence": ["first"]}
    with pytest.raises(FrozenInstanceError):
        event.run_id = None  # type: ignore[misc]


@pytest.mark.parametrize("event_type", ["RunStart", "StartRun", "run_started", "UnknownEvent"])
def test_unknown_or_non_past_tense_event_name_is_rejected(event_type: str) -> None:
    with pytest.raises(ValueError, match="past-tense event type"):
        Event(type=event_type, correlation_id=CORRELATION_ID, payload={})


def test_naive_event_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        Event(
            type=EventType.RUN_STARTED,
            correlation_id=CORRELATION_ID,
            run_id=RUN_ID,
            payload={},
            occurred_at=datetime(2026, 8, 31, 12),
        )


@pytest.mark.parametrize(
    "payload",
    [
        ["not-an-object"],
        {"unsupported": object()},
        {"not_finite": float("inf")},
        {1: "non-string-key"},
    ],
)
def test_invalid_payload_is_rejected(payload: object) -> None:
    with pytest.raises(ValueError, match=r"JSON object|JSON cannot represent"):
        Event(
            type=EventType.ARTIFACT_CREATED,
            correlation_id=CORRELATION_ID,
            run_id=RUN_ID,
            payload=payload,  # type: ignore[arg-type]
        )


def test_event_json_round_trip_is_deterministic_and_normalizes_utc() -> None:
    event = Event(
        id=EVENT_ID,
        type=EventType.ATTEMPT_SUCCEEDED,
        occurred_at=datetime(
            2026,
            8,
            31,
            9,
            15,
            30,
            123,
            tzinfo=timezone(timedelta(hours=1)),
        ),
        schema_version=EVENT_SCHEMA_VERSION,
        run_id=RUN_ID,
        correlation_id=CORRELATION_ID,
        payload={"z": [2, 1], "message": "探索完成"},
    )

    encoded = event.to_json()

    assert encoded == (
        '{"correlation_id":"0f348af4-0184-4715-81f7-2f7c58123f23",'
        '"id":"a31a165a-a7a1-42de-bc40-4cef880661f9",'
        '"occurred_at":"2026-08-31T08:15:30.000123Z",'
        '"payload":{"message":"探索完成","z":[2,1]},'
        '"run_id":"bdf405f8-7f84-48e2-b128-7479c3b9b7a7",'
        '"schema_version":1,"type":"AttemptSucceeded"}'
    )
    assert Event.from_json(encoded) == event
    assert Event.from_dict(event.to_dict()) == event


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("run_id", "not-a-uuid"),
        ("correlation_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_invalid_envelope_fields_are_rejected(field: str, value: object) -> None:
    envelope: dict[str, object] = {
        "id": EVENT_ID,
        "type": "RunStarted",
        "occurred_at": "2026-08-31T08:15:30.000123Z",
        "schema_version": EVENT_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "correlation_id": CORRELATION_ID,
        "payload": {},
    }
    envelope[field] = value

    with pytest.raises(ValueError):
        Event.from_dict(envelope)


def test_round_trip_rejects_missing_and_extra_envelope_fields() -> None:
    envelope = Event(
        id=EVENT_ID,
        type=EventType.RUN_STARTED,
        correlation_id=CORRELATION_ID,
        run_id=RUN_ID,
        payload={},
    ).to_dict()
    del envelope["payload"]
    envelope["unexpected"] = True

    with pytest.raises(ValueError, match=r"missing=.*payload.*extra=.*unexpected"):
        Event.from_dict(envelope)
