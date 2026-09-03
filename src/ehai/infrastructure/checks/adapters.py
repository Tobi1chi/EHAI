"""Command, Artifact, and transparent semantic Check adapters for P1."""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath

from ehai import ID, JsonValue, json_dumps, normalize_id
from ehai.application.checks import (
    CheckAdapterError,
    CheckAdapterTimeout,
    CheckContext,
    CheckOutcome,
)
from ehai.application.ports import ArtifactStore
from ehai.domain.artifacts import Artifact
from ehai.domain.checking import CheckSpec


class CommandCheckAdapter:
    """Run preconfigured argv commands without a shell."""

    def __init__(
        self,
        commands: Mapping[ID, Sequence[str]],
        *,
        store: ArtifactStore | None = None,
        default_argv: Sequence[str] | None = None,
        timeout_seconds: float = 30.0,
        max_output_bytes: int = 64 * 1024,
        max_artifact_bytes: int = 256 * 1024,
        max_total_artifact_bytes: int = 1024 * 1024,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")
        if type(max_artifact_bytes) is not int or max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be a positive integer")
        if type(max_total_artifact_bytes) is not int or max_total_artifact_bytes < 1:
            raise ValueError("max_total_artifact_bytes must be a positive integer")
        if store is not None and not isinstance(store, ArtifactStore):
            raise TypeError("store must implement ArtifactStore")
        self._commands = _argv_mapping(commands)
        self._default_argv = None if default_argv is None else _argv(default_argv)
        self._store = store
        self._timeout_seconds = float(timeout_seconds)
        self._max_output_bytes = max_output_bytes
        self._max_artifact_bytes = max_artifact_bytes
        self._max_total_artifact_bytes = max_total_artifact_bytes

    def evaluate(self, spec: CheckSpec, context: CheckContext) -> CheckOutcome:
        argv = spec.command_argv or self._commands.get(spec.check_id, self._default_argv)
        if argv is None:
            raise CheckAdapterError(f"Command Check {spec.check_id} has no configured argv")
        with tempfile.TemporaryDirectory(prefix="ehai-check-") as check_dir:
            check_path = Path(check_dir).resolve(strict=True)
            materialized = (
                self._materialize_artifacts(context, check_path) if self._store is not None else ()
            )
            try:
                completed = subprocess.run(
                    argv,
                    cwd=check_path if self._store is not None else context.workspace,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    shell=False,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                output = self._command_output(
                    argv,
                    exit_code=None,
                    stdout=error.stdout,
                    stderr=error.stderr,
                    timed_out=True,
                    materialized=materialized,
                )
                raise CheckAdapterTimeout(
                    f"Command Check {spec.check_id} timed out after {self._timeout_seconds:g}s; "
                    f"output={output}"
                ) from error
            except OSError as error:
                raise CheckAdapterError(
                    f"Command Check {spec.check_id} could not start: {error}"
                ) from error

            output = self._command_output(
                argv,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                timed_out=False,
                materialized=materialized,
            )
        evidence = tuple(artifact.artifact_id for artifact in context.artifacts)
        if completed.returncode != 0:
            return CheckOutcome(
                passed=False,
                output=output,
                evidence_artifact_ids=evidence,
                failure_reason=f"command exited with code {completed.returncode}",
            )
        if not evidence:
            return CheckOutcome(
                passed=False,
                output=output,
                evidence_artifact_ids=(),
                failure_reason="command passed but no Artifact evidence was supplied",
            )
        return CheckOutcome(passed=True, output=output, evidence_artifact_ids=evidence)

    def _command_output(
        self,
        argv: tuple[str, ...],
        *,
        exit_code: int | None,
        stdout: bytes | str | None,
        stderr: bytes | str | None,
        timed_out: bool,
        materialized: tuple[dict[str, JsonValue], ...],
    ) -> str:
        stdout_text, stdout_truncated = _truncate_output(stdout, self._max_output_bytes)
        stderr_text, stderr_truncated = _truncate_output(stderr, self._max_output_bytes)
        return json_dumps(
            {
                "argv": list(argv),
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "timed_out": timed_out,
                "materialized_artifacts": list(materialized),
            }
        )

    def _materialize_artifacts(
        self,
        context: CheckContext,
        check_path: Path,
    ) -> tuple[dict[str, JsonValue], ...]:
        assert self._store is not None
        total_bytes = 0
        used_names: set[str] = set()
        materialized: list[dict[str, JsonValue]] = []
        for artifact in context.artifacts:
            name = _safe_materialized_name(artifact.name)
            if name in used_names:
                raise CheckAdapterError(f"duplicate materialized Artifact filename: {name}")
            stored, content, problem = _load_artifact(self._store, artifact)
            if problem is not None:
                raise CheckAdapterError(problem)
            assert stored is not None and content is not None
            if len(content) > self._max_artifact_bytes:
                raise CheckAdapterError(
                    f"Artifact {artifact.artifact_id} exceeds Check input limit"
                )
            total_bytes += len(content)
            if total_bytes > self._max_total_artifact_bytes:
                raise CheckAdapterError("Command Check Artifact inputs exceed total limit")
            target = (check_path / name).resolve(strict=False)
            if not target.is_relative_to(check_path):
                raise CheckAdapterError(
                    f"Artifact {artifact.artifact_id} filename escapes check dir"
                )
            target.write_bytes(content)
            used_names.add(name)
            materialized.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "name": name,
                    "size_bytes": len(content),
                    "sha256": stored.sha256,
                }
            )
        return tuple(materialized)


@dataclass(frozen=True, slots=True)
class ArtifactCheckRule:
    """Transparent count, media-type, and non-empty requirements."""

    allowed_media_types: tuple[str, ...] = ()
    require_non_empty: bool = True
    minimum_count: int = 1
    maximum_count: int | None = None

    def __post_init__(self) -> None:
        media_types = tuple(self.allowed_media_types)
        if any(
            not isinstance(item, str) or not item.strip() or "/" not in item for item in media_types
        ):
            raise ValueError("allowed_media_types must contain valid media types")
        if len(set(media_types)) != len(media_types):
            raise ValueError("allowed_media_types must not contain duplicates")
        if not isinstance(self.require_non_empty, bool):
            raise ValueError("require_non_empty must be a boolean")
        if type(self.minimum_count) is not int or self.minimum_count < 0:
            raise ValueError("minimum_count must be a non-negative integer")
        if self.maximum_count is not None and (
            type(self.maximum_count) is not int or self.maximum_count < self.minimum_count
        ):
            raise ValueError("maximum_count must be an integer no smaller than minimum_count")
        object.__setattr__(self, "allowed_media_types", media_types)


class ArtifactCheckAdapter:
    """Validate persisted Artifact bytes against preconfigured rules."""

    def __init__(
        self,
        store: ArtifactStore,
        rules: Mapping[ID, ArtifactCheckRule],
        *,
        default_rule: ArtifactCheckRule | None = None,
    ) -> None:
        if not isinstance(store, ArtifactStore):
            raise TypeError("store must implement ArtifactStore")
        if default_rule is not None and not isinstance(default_rule, ArtifactCheckRule):
            raise TypeError("default_rule must be an ArtifactCheckRule")
        self._store = store
        self._rules = _rule_mapping(rules, ArtifactCheckRule, "Artifact Check rule")
        self._default_rule = default_rule

    def evaluate(self, spec: CheckSpec, context: CheckContext) -> CheckOutcome:
        rule = self._rules.get(spec.check_id, self._default_rule)
        if rule is None:
            raise CheckAdapterError(f"Artifact Check {spec.check_id} has no configured rule")

        violations: list[str] = []
        evidence: list[ID] = []
        inspected: list[JsonValue] = []
        count = len(context.artifacts)
        if count < rule.minimum_count:
            violations.append(f"expected at least {rule.minimum_count} Artifacts, got {count}")
        if rule.maximum_count is not None and count > rule.maximum_count:
            violations.append(f"expected at most {rule.maximum_count} Artifacts, got {count}")

        for artifact in context.artifacts:
            stored, content, problem = _load_artifact(self._store, artifact)
            if problem is not None:
                violations.append(problem)
                continue
            assert stored is not None and content is not None
            evidence.append(stored.artifact_id)
            inspected_entry: dict[str, JsonValue] = {
                "artifact_id": stored.artifact_id,
                "media_type": stored.media_type,
                "size_bytes": len(content),
            }
            inspected.append(inspected_entry)
            if rule.allowed_media_types and stored.media_type not in rule.allowed_media_types:
                violations.append(
                    f"Artifact {stored.artifact_id} media type {stored.media_type!r} is not allowed"
                )
            if rule.require_non_empty and not content:
                violations.append(f"Artifact {stored.artifact_id} is empty")

        output = json_dumps(
            {
                "rule": {
                    "allowed_media_types": list(rule.allowed_media_types),
                    "require_non_empty": rule.require_non_empty,
                    "minimum_count": rule.minimum_count,
                    "maximum_count": rule.maximum_count,
                },
                "inspected": inspected,
                "violations": [violation for violation in violations],
            }
        )
        if violations:
            return CheckOutcome(
                passed=False,
                output=output,
                evidence_artifact_ids=tuple(evidence),
                failure_reason="; ".join(violations),
            )
        return CheckOutcome(passed=True, output=output, evidence_artifact_ids=tuple(evidence))


@dataclass(frozen=True, slots=True)
class SemanticRubric:
    """A transparent, deterministic P1 semantic scoring rubric."""

    description: str
    required_terms: tuple[str, ...] = ()
    minimum_score: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Semantic rubric description must not be blank")
        terms = tuple(self.required_terms)
        if any(not isinstance(term, str) or not term.strip() for term in terms):
            raise ValueError("Semantic rubric terms must not be blank")
        normalized_terms = tuple(term.casefold() for term in terms)
        if len(set(normalized_terms)) != len(normalized_terms):
            raise ValueError("Semantic rubric terms must not contain duplicates")
        if (
            isinstance(self.minimum_score, bool)
            or not isinstance(self.minimum_score, (int, float))
            or not math.isfinite(self.minimum_score)
            or not 0 <= self.minimum_score <= 1
        ):
            raise ValueError("Semantic rubric minimum_score must be between 0 and 1")
        object.__setattr__(self, "required_terms", normalized_terms)
        object.__setattr__(self, "minimum_score", float(self.minimum_score))


@dataclass(frozen=True, slots=True)
class SemanticEvaluation:
    """A score and explanation returned by a deterministic evaluator."""

    score: float
    explanation: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not math.isfinite(self.score)
            or not 0 <= self.score <= 1
        ):
            raise ValueError("Semantic score must be between 0 and 1")
        if not isinstance(self.explanation, str) or not self.explanation.strip():
            raise ValueError("Semantic explanation must not be blank")
        object.__setattr__(self, "score", float(self.score))


SemanticEvaluator = Callable[
    [CheckSpec, CheckContext, tuple[bytes, ...], SemanticRubric],
    SemanticEvaluation,
]


class SemanticCheckAdapter:
    """Evaluate existing Artifact evidence with an inspectable P1 rubric."""

    def __init__(
        self,
        store: ArtifactStore,
        rubrics: Mapping[ID, SemanticRubric],
        *,
        default_rubric: SemanticRubric | None = None,
        evaluator: SemanticEvaluator | None = None,
    ) -> None:
        if not isinstance(store, ArtifactStore):
            raise TypeError("store must implement ArtifactStore")
        if default_rubric is not None and not isinstance(default_rubric, SemanticRubric):
            raise TypeError("default_rubric must be a SemanticRubric")
        self._store = store
        self._rubrics = _rule_mapping(rubrics, SemanticRubric, "Semantic rubric")
        self._default_rubric = default_rubric
        configured_rubrics = tuple(self._rubrics.values()) + (
            () if self._default_rubric is None else (self._default_rubric,)
        )
        if evaluator is None and any(not rubric.required_terms for rubric in configured_rubrics):
            raise ValueError("default Semantic evaluator requires at least one required term")
        self._evaluator = _required_terms_evaluator if evaluator is None else evaluator

    def evaluate(self, spec: CheckSpec, context: CheckContext) -> CheckOutcome:
        rubric = (
            SemanticRubric(
                description=spec.description,
                required_terms=spec.semantic_required_terms,
                minimum_score=1.0,
            )
            if spec.semantic_required_terms
            else self._rubrics.get(spec.check_id, self._default_rubric)
        )
        if rubric is None:
            raise CheckAdapterError(f"Semantic Check {spec.check_id} has no configured rubric")

        contents: list[bytes] = []
        evidence: list[ID] = []
        problems: list[str] = []
        for artifact in context.artifacts:
            stored, content, problem = _load_artifact(self._store, artifact)
            if problem is not None:
                problems.append(problem)
                continue
            assert stored is not None and content is not None
            contents.append(content)
            evidence.append(stored.artifact_id)

        if not contents:
            explanation = "no readable Artifact evidence was supplied"
            evaluation = SemanticEvaluation(score=0.0, explanation=explanation)
            problems.append(explanation)
        elif problems:
            evaluation = SemanticEvaluation(
                score=0.0,
                explanation="semantic evaluation stopped because Artifact evidence was incomplete",
            )
        else:
            evaluation = self._evaluator(spec, context, tuple(contents), rubric)
            if not isinstance(evaluation, SemanticEvaluation):
                raise CheckAdapterError(
                    f"Semantic evaluator for Check {spec.check_id} returned an invalid result"
                )

        passed = not problems and evaluation.score >= rubric.minimum_score
        output = json_dumps(
            {
                "rubric": {
                    "description": rubric.description,
                    "required_terms": list(rubric.required_terms),
                    "minimum_score": rubric.minimum_score,
                },
                "score": evaluation.score,
                "explanation": evaluation.explanation,
                "evidence_artifact_ids": list(evidence),
                "problems": [problem for problem in problems],
            }
        )
        if passed:
            return CheckOutcome(
                passed=True,
                output=output,
                evidence_artifact_ids=tuple(evidence),
            )
        reason = (
            "; ".join(problems)
            if problems
            else f"semantic score {evaluation.score:g} is below {rubric.minimum_score:g}"
        )
        return CheckOutcome(
            passed=False,
            output=output,
            evidence_artifact_ids=tuple(evidence),
            failure_reason=reason,
        )


def _required_terms_evaluator(
    spec: CheckSpec,
    context: CheckContext,
    contents: tuple[bytes, ...],
    rubric: SemanticRubric,
) -> SemanticEvaluation:
    del spec, context
    text = "\n".join(content.decode("utf-8", errors="replace") for content in contents).casefold()
    matched = tuple(term for term in rubric.required_terms if term in text)
    missing = tuple(term for term in rubric.required_terms if term not in text)
    score = len(matched) / len(rubric.required_terms)
    return SemanticEvaluation(
        score=score,
        explanation=f"matched terms={matched}; missing terms={missing}",
    )


def _load_artifact(
    store: ArtifactStore,
    expected: Artifact,
) -> tuple[Artifact | None, bytes | None, str | None]:
    try:
        stored = store.get(expected.artifact_id)
        if stored is None:
            return None, None, f"Artifact {expected.artifact_id} is missing"
        if stored != expected:
            return None, None, f"Artifact {expected.artifact_id} metadata does not match"
        return stored, store.read(expected.artifact_id), None
    except (FileNotFoundError, OSError, RuntimeError) as error:
        return None, None, f"Artifact {expected.artifact_id} could not be read: {error}"


def _argv_mapping(commands: Mapping[ID, Sequence[str]]) -> dict[ID, tuple[str, ...]]:
    normalized: dict[ID, tuple[str, ...]] = {}
    for check_id, argv_source in commands.items():
        normalized_id = normalize_id(check_id)
        if normalized_id in normalized:
            raise ValueError("commands contains duplicate normalized Check IDs")
        normalized[normalized_id] = _argv(argv_source)
    return normalized


def _argv(argv_source: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv_source, str):
        raise ValueError("command argv must be a sequence of arguments, not shell text")
    argv = tuple(argv_source)
    if not argv or any(
        not isinstance(argument, str) or not argument or "\x00" in argument for argument in argv
    ):
        raise ValueError("command argv must contain non-empty string arguments")
    return argv


def _safe_materialized_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip() or "\x00" in name:
        raise CheckAdapterError("Artifact filename must not be blank or contain NUL")
    path = PurePath(name)
    if path.is_absolute() or path.name != name or name in {".", ".."}:
        raise CheckAdapterError(f"Artifact filename is unsafe: {name!r}")
    if any(separator in name for separator in ("/", "\\")):
        raise CheckAdapterError(f"Artifact filename is unsafe: {name!r}")
    return name


def _rule_mapping[RuleT](
    values: Mapping[ID, RuleT],
    expected_type: type[RuleT],
    label: str,
) -> dict[ID, RuleT]:
    normalized: dict[ID, RuleT] = {}
    for check_id, rule in values.items():
        normalized_id = normalize_id(check_id)
        if normalized_id in normalized:
            raise ValueError(f"{label} mapping contains duplicate normalized Check IDs")
        if not isinstance(rule, expected_type):
            raise TypeError(f"{label} mapping contains an invalid value")
        normalized[normalized_id] = rule
    return normalized


def _truncate_output(value: bytes | str | None, limit: int) -> tuple[str, bool]:
    if value is None:
        return "", False
    encoded = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
    truncated = len(encoded) > limit
    return encoded[:limit].decode("utf-8", errors="replace"), truncated
