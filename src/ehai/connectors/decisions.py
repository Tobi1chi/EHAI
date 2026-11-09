"""Independent OpenAI Decisions connector; item data never becomes instructions."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import httpx
from pydantic import ValidationError

from ehai import JsonValue
from ehai.application.connector_models import ConnectorAction, ConnectorClaim, ConnectorManifest
from ehai.connectors.bridge import ConnectorOutcome, _api_base, _initialize, _lock, _read, _run_once
from ehai.connectors.decisions_contract import (
    ClassifyInput,
    ClassifyOutput,
    DecisionCounts,
    DecisionFailure,
    DecisionResult,
    DecisionUsage,
)


def decisions_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_type="openai_decisions",
        version="1",
        actions=[
            ConnectorAction(
                name="decisions.classify",
                version="1",
                read_only=True,
                description="Classify JSON items independently and keep selected choices",
                input_schema=ClassifyInput.model_json_schema(),
                output_schema=ClassifyOutput.model_json_schema(),
            )
        ],
    )


class DecisionsConnectorAdapter:
    def __init__(self, client: httpx.Client, model: str) -> None:
        self.client = client
        self.model = model

    def _item(
        self, request: ClassifyInput, item: dict[str, JsonValue]
    ) -> tuple[DecisionResult | DecisionFailure, DecisionUsage]:
        usage = DecisionUsage(input_tokens=0, output_tokens=0)
        identifier = item.get(request.id_field)
        if request.id_field not in item or any(key not in item for key in request.text_fields):
            return DecisionFailure(id=identifier, reason="Missing id or text field"), usage
        body = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_json",
                            "data": {key: item[key] for key in request.text_fields},
                        }
                    ],
                }
            ],
            "questions": [
                {
                    "type": "choice",
                    "name": "decision",
                    "instructions": request.question,
                    "choices": [choice.model_dump() for choice in request.choices],
                }
            ],
        }
        try:
            for attempt in range(2):
                response = self.client.post("decisions", json=body)
                if attempt == 0 and (
                    response.status_code == 429 or 500 <= response.status_code <= 599
                ):
                    time.sleep(0.5)
                    continue
                break
            if not response.is_success:
                return DecisionFailure(
                    id=identifier, reason=f"Provider HTTP {response.status_code}"
                ), usage
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid response")
            provider_usage = data.get("usage")
            if isinstance(provider_usage, dict):
                # Missing or invalid usage does not invalidate an answer.
                with suppress(ValidationError):
                    usage = DecisionUsage.model_validate(
                        {
                            "input_tokens": provider_usage.get("input_tokens"),
                            "output_tokens": provider_usage.get("output_tokens"),
                        }
                    )
            answers = data.get("answers")
            if not isinstance(answers, list) or not answers or not isinstance(answers[0], dict):
                raise ValueError("Missing answer")
            answer = answers[0]
            if answer.get("type") == "refusal":
                return DecisionFailure(
                    id=identifier, reason="Provider refused classification"
                ), usage
            if answer.get("type") != "choice":
                raise ValueError("Invalid answer type")
            result = DecisionResult.model_validate(
                {
                    "id": identifier,
                    "choice": answer.get("choice"),
                    "confidence": answer.get("confidence"),
                    "probabilities": answer.get("probabilities"),
                }
            )
            choices = {choice.value for choice in request.choices}
            values = [entry.value for entry in result.probabilities]
            if (
                result.choice not in choices
                or set(values) != choices
                or len(values) != len(choices)
            ):
                raise ValueError("Invalid choice values")
            return result, usage
        except httpx.HTTPError:
            return DecisionFailure(id=identifier, reason="Provider request failed"), usage
        except (ValueError, TypeError, KeyError):
            return DecisionFailure(id=identifier, reason="Invalid provider answer"), usage

    def execute(self, claim: ConnectorClaim) -> ConnectorOutcome:
        manifest = decisions_manifest()
        if (
            claim.connection.manifest != manifest
            or claim.call.action != manifest.actions[0]
            or claim.connection.configuration != {"model": self.model}
        ):
            return ConnectorOutcome(
                "failed", error="Decisions contract does not match this connection"
            )
        try:
            request = ClassifyInput.model_validate(claim.call.inputs)
        except ValidationError:
            return ConnectorOutcome("failed", error="Invalid decisions.classify inputs")
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(lambda item: self._item(request, item), request.items))
        results: list[DecisionResult] = []
        failed: list[DecisionFailure] = []
        kept: list[dict[str, JsonValue]] = []
        for item, (result, _) in zip(request.items, entries, strict=True):
            if isinstance(result, DecisionFailure):
                failed.append(result)
            else:
                results.append(result)
                if result.choice in request.keep:
                    kept.append(
                        {
                            **item,
                            "decision_choice": result.choice,
                            "decision_confidence": result.confidence,
                        }
                    )
        if not results:
            reason = Counter(item.reason for item in failed).most_common(1)[0][0]
            return ConnectorOutcome("failed", error=f"Every item failed classification: {reason}")
        output = ClassifyOutput(
            model=self.model,
            results=results,
            kept=kept,
            failed=failed,
            counts=DecisionCounts(total=len(request.items), kept=len(kept), failed=len(failed)),
            usage=DecisionUsage(
                input_tokens=sum(u.input_tokens for _, u in entries),
                output_tokens=sum(u.output_tokens for _, u in entries),
            ),
        )
        return ConnectorOutcome("completed", output=output.model_dump(mode="json"))


def _provider_url(value: str) -> str:
    parsed = httpx.URL(value)
    if (
        parsed.scheme != "http"
        or parsed.host not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Test provider must be a loopback HTTP URL")
    return value.rstrip("/") + "/"


def _run(directory: Path, *, once: bool, test_provider_url: str | None) -> None:
    base = _provider_url(test_provider_url) if test_provider_url else "https://api.openai.com/v1/"
    key = "loopback-test" if test_provider_url else os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY is required")
    config = _read(directory / "bridge.json")
    token = _read(directory / "bridge-token.json")["token"]
    registration = _read(directory / "registration.json")
    model = registration["request"]["configuration"]["model"]
    with (
        httpx.Client(
            base_url=_api_base(config["api_url"]),
            headers={"X-EHAI-Connector-Token": token},
            timeout=30,
            trust_env=False,
        ) as core,
        httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {key}"},
            timeout=60,
            trust_env=False,
            follow_redirects=False,
        ) as provider,
    ):
        adapter = DecisionsConnectorAdapter(provider, model)
        while True:
            _run_once(directory, core, adapter, config)
            if once:
                return
            time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independent OpenAI Decisions connector")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--api-url", required=True)
    init.add_argument("--project-id", required=True)
    init.add_argument("--model", default="gpt-6-luna")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--test-provider-url")
    for command in (init, run):
        command.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with _lock(args.state_dir):
            if args.command == "init":
                if not args.model.strip():
                    raise ValueError("Model must not be empty")
                _initialize(
                    args.state_dir,
                    args.api_url,
                    args.project_id,
                    decisions_manifest(),
                    {"model": args.model},
                    "OpenAI Decisions",
                )
            else:
                _run(args.state_dir, once=args.once, test_provider_url=args.test_provider_url)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            f"Decisions connector stopped ({type(error).__name__}); private state retained",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
