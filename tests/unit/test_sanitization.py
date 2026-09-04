from ehai import json_dumps
from ehai.application.sanitization import bounded_redacted_text, sanitize_json_object


def test_session_payload_sanitization_redacts_and_bounds_encoded_json() -> None:
    payload, truncated = sanitize_json_object(
        {
            "authorization": "Bearer private-token",
            "nested": {"api_key": "sk-private-key"},
            "secret": "ordinary-private-value",
            "content": "x" * 4_096,
        },
        max_bytes=512,
    )

    encoded = json_dumps(payload).encode("utf-8")
    assert truncated
    assert len(encoded) <= 512
    assert b"private-token" not in encoded
    assert b"sk-private-key" not in encoded
    assert b"ordinary-private-value" not in encoded
    assert b"[REDACTED]" in encoded

    diagnostic = bounded_redacted_text("Bearer private-token " + "x" * 1_000, max_bytes=64)
    assert diagnostic is not None
    assert len(diagnostic.encode("utf-8")) <= 64
    assert "private-token" not in diagnostic
