"""Small, explicit forms for the supported live Worker request protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ehai import JsonValue, json_dumps
from ehai.application.sanitization import sanitize_json_object


@dataclass(frozen=True, slots=True)
class WorkerRequestForm:
    context: dict[str, JsonValue]
    resolution_schema: dict[str, JsonValue] | None
    unavailable_reason: str | None


def _object(properties: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def worker_request_form(kind: str, params: Mapping[str, JsonValue]) -> WorkerRequestForm:
    """Expose only the decision context; scope approvals to this request/turn."""
    fields = {
        "command_approval": ("command", "cwd", "reason", "availableDecisions"),
        "file_change_approval": ("reason", "grantRoot"),
        "permission_approval": ("cwd", "reason", "permissions"),
        "user_input": ("questions", "isBlocking"),
    }
    original = {key: params[key] for key in fields[kind] if key in params}
    context, truncated = sanitize_json_object(original, max_bytes=32_768)
    if truncated or json_dumps(context) != json_dumps(original):
        return WorkerRequestForm(context, None, "Request context is redacted or truncated")
    schema: dict[str, JsonValue]
    if kind == "user_input":
        questions = context.get("questions")
        if not isinstance(questions, list) or not questions:
            return WorkerRequestForm(context, None, "Worker did not provide answerable questions")
        answers: dict[str, JsonValue] = {}
        for question in questions:
            if not isinstance(question, dict) or not isinstance(question.get("id"), str):
                return WorkerRequestForm(context, None, "Worker question has no identifier")
            question_id = str(question["id"])
            if not question_id or question_id in answers:
                return WorkerRequestForm(context, None, "Worker question identifiers are ambiguous")
            answer: dict[str, JsonValue] = {"type": "string", "minLength": 1}
            options = question.get("options")
            if isinstance(options, list) and options and not question.get("isOther", False):
                labels = [o.get("label") for o in options if isinstance(o, dict)]
                if len(labels) != len(options) or not all(
                    isinstance(label, str) for label in labels
                ):
                    return WorkerRequestForm(context, None, "Worker options are incomplete")
                answer["enum"] = labels
            answers[question_id] = _object(
                {"answers": {"type": "array", "items": answer, "minItems": 1, "maxItems": 1}}
            )
        schema = _object({"answers": _object(answers)})
    elif kind == "permission_approval":
        permissions = context.get("permissions")
        if not isinstance(permissions, dict) or not permissions:
            return WorkerRequestForm(context, None, "Worker did not specify requested permissions")
        # The UI can grant exactly the displayed request for this turn or decline it.
        schema = _object(
            {"permissions": {"const": permissions}, "scope": {"type": "string", "const": "turn"}}
        )
    else:
        if kind == "command_approval":
            available = context.get("availableDecisions")
            if isinstance(available, list) and "accept" not in available:
                return WorkerRequestForm(
                    context, None, "Worker does not offer single-command approval"
                )
            if not isinstance(context.get("command"), str):
                return WorkerRequestForm(context, None, "Worker did not provide the command")
        if kind == "file_change_approval" and context.get("grantRoot") is not None:
            return WorkerRequestForm(
                context, None, "Session-wide write grants are not an inbox action"
            )
        schema = _object({"decision": {"type": "string", "const": "accept"}})
    return WorkerRequestForm(context, schema, None)
