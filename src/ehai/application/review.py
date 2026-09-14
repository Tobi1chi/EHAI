"""Framework-neutral validation of Reviewer evidence, not Gate decisions."""

from collections.abc import Iterable, Mapping

from ehai import ID, JsonValue, json_loads


def validate_review_submission(
    submission: Mapping[str, JsonValue],
    evidence_artifact_ids: Iterable[ID],
) -> None:
    """Require the same evidence contract for Built-in and external Reviewers."""
    if submission.get("name") != "review.json":
        raise ValueError("Reviewer candidate name must be review.json")
    if submission.get("media_type") != "application/json":
        raise ValueError("Reviewer candidate media_type must be application/json")
    content = submission.get("content")
    if not isinstance(content, str):
        raise ValueError("Reviewer content must be JSON text")
    try:
        document = json_loads(content)
    except ValueError as error:
        raise ValueError("Reviewer content must be valid JSON") from error
    required = {"summary", "findings", "evidence_artifact_ids", "recommended_action"}
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("Reviewer content must contain exactly the required keys")
    if not isinstance(document["summary"], str) or not document["summary"].strip():
        raise ValueError("Reviewer summary must not be blank")
    findings = document["findings"]
    if not isinstance(findings, list) or any(not _valid_finding(item) for item in findings):
        raise ValueError("Reviewer findings require severity, message, and evidence")
    evidence = document["evidence_artifact_ids"]
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ValueError("Reviewer evidence_artifact_ids must be a string array")
    expected = set(evidence_artifact_ids)
    if set(evidence) != expected or len(evidence) != len(expected):
        raise ValueError(
            "Reviewer evidence_artifact_ids must cover every input Artifact exactly once"
        )
    action = document["recommended_action"]
    if not isinstance(action, str) or action not in {"pass", "revise"}:
        raise ValueError("Reviewer recommended_action must be pass or revise")


def _valid_finding(item: JsonValue) -> bool:
    if not isinstance(item, dict) or set(item) != {"severity", "message", "evidence"}:
        return False
    severity = item.get("severity")
    message = item.get("message")
    evidence = item.get("evidence")
    return (
        isinstance(severity, str)
        and severity in {"blocker", "major", "minor", "note"}
        and isinstance(message, str)
        and bool(message.strip())
        and isinstance(evidence, str)
        and bool(evidence.strip())
    )
