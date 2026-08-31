from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

import ehai


def test_package_exposes_identifier_conventions() -> None:
    identifier = ehai.new_id()

    assert str(UUID(identifier)) == identifier
    assert UUID(identifier).version == 4
    assert ehai.normalize_id(identifier.upper()) == identifier


@pytest.mark.parametrize("value", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_invalid_identifier_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        ehai.normalize_id(value)


def test_utc_timestamp_round_trip_is_canonical() -> None:
    source = datetime(2026, 8, 31, 8, 15, 30, 123, tzinfo=timezone(timedelta(hours=8)))

    encoded = ehai.format_utc_datetime(source)

    assert encoded == "2026-08-31T00:15:30.000123Z"
    assert ehai.parse_utc_datetime(encoded) == source.astimezone(UTC)
    assert ehai.utc_now().tzinfo is UTC


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        ehai.format_utc_datetime(datetime(2026, 8, 31))

    with pytest.raises(ValueError, match="UTC offset"):
        ehai.parse_utc_datetime("2026-08-31T00:15:30")


def test_json_round_trip_is_deterministic_and_unicode_safe() -> None:
    value: ehai.JsonValue = {"z": [True, None, 3], "message": "探索"}

    encoded = ehai.json_dumps(value)

    assert encoded == '{"message":"探索","z":[true,null,3]}'
    assert ehai.json_loads(encoded) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), ("not", "json")])
def test_non_json_values_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        ehai.json_dumps(value)  # type: ignore[arg-type]


def test_non_standard_json_constant_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-standard JSON constant"):
        ehai.json_loads('{"score": NaN}')
