"""Immutable code commits and resumable merges in EHAI-owned Git worktrees."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock

from ehai import JsonValue, json_dumps, json_loads, normalize_id
from ehai.domain.adoptions import ResultAdoption


class MetadataError(RuntimeError):
    """Code preparation or its persisted ownership record is inconsistent."""


class RunBaseNotFoundError(MetadataError):
    """The Run has not pinned its Git base yet."""


@dataclass(frozen=True, slots=True)
class RunBasePin:
    base_commit: str


@dataclass(frozen=True, slots=True)
class GitCodeResult:
    run_id: str
    attempt_id: str
    worktree: Path
    base_commit: str
    commit: str
    diff_path: Path


@dataclass(frozen=True, slots=True)
class WorktreePreparation:
    worktree: Path
    conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AttemptSnapshot:
    worktree: Path
    result: GitCodeResult | None


class GitCodeWorkspace:
    """Use native Git ancestry without writing to the user's index or branch."""

    def __init__(
        self,
        *,
        base_workspace: Path,
        owned_root: Path,
        metadata_root: Path | None = None,
    ) -> None:
        self.base_workspace = base_workspace.resolve(strict=True)
        self.owned_root = owned_root.resolve()
        self.metadata_root = self._owned(metadata_root or self.owned_root / ".ehai-code-results")
        if self.base_workspace == self.owned_root or self.base_workspace.is_relative_to(
            self.owned_root
        ):
            raise ValueError("The user workspace cannot be an owned execution workspace")
        self._common_dir: Path | None = None
        self._pin_lock = Lock()

    def _repository(self) -> Path:
        if self._common_dir is None:
            common = self._text(
                self.base_workspace, ("rev-parse", "--path-format=absolute", "--git-common-dir")
            )
            self._common_dir = Path(common).resolve(strict=True)
            if self.owned_root == self._common_dir or self.owned_root.is_relative_to(
                self._common_dir
            ):
                raise ValueError("Execution workspaces cannot be inside Git metadata")
        return self._common_dir

    def pin_run_base(self, *, run_id: str, base_commit: str) -> RunBasePin:
        with self._pin_lock:
            return self._pin_run_base(run_id=run_id, base_commit=base_commit)

    def _pin_run_base(self, *, run_id: str, base_commit: str) -> RunBasePin:
        path = self._path(run_id, "base.json")
        commit = self._commit_id(self.base_workspace, base_commit)
        if path.exists():
            existing = self.load_run_base(run_id=run_id)
            if existing.base_commit != commit:
                raise MetadataError("Run base cannot change after execution starts")
            return existing
        self._write(path, {"base_commit": commit, "repository": str(self._repository())})
        return RunBasePin(commit)

    def load_run_base(self, *, run_id: str) -> RunBasePin:
        path = self._path(run_id, "base.json")
        if not path.is_file():
            raise RunBaseNotFoundError(f"Run {run_id} has no Git base")
        document = self._read(path)
        if document.get("repository") != str(self._repository()):
            raise MetadataError("Run belongs to another Git repository")
        return RunBasePin(self._commit_id(self.base_workspace, _string(document, "base_commit")))

    def prepare_worktree(
        self,
        *,
        run_id: str,
        attempt_id: str,
        worktree: Path,
        base_commit: str,
        upstream_commits: Sequence[str] = (),
        dependency_commits: Sequence[str] | None = None,
    ) -> WorktreePreparation:
        workspace = self._worktree(worktree)
        pin = self.pin_run_base(run_id=run_id, base_commit=base_commit)
        path = self._attempt_path(run_id, attempt_id)
        upstreams = [self._commit_id(workspace, commit) for commit in upstream_commits]
        dependency_upstreams = [
            self._commit_id(workspace, commit)
            for commit in (upstream_commits if dependency_commits is None else dependency_commits)
        ]
        if path.exists():
            document = self._read_attempt(run_id, attempt_id)
            if document.get("upstreams") != upstreams or document.get("workspace") != str(
                workspace
            ):
                raise MetadataError("Attempt workspace or predecessor commits changed")
            if (
                dependency_commits is not None
                and document.get("dependency_upstreams") != dependency_upstreams
            ):
                raise MetadataError("Attempt dependency baseline changed")
            return self.resume_worktree(run_id=run_id, attempt_id=attempt_id)
        if self._text(workspace, ("status", "--porcelain")):
            raise MetadataError("New execution worktree must be clean before preparation")
        self._git(workspace, ("checkout", "--detach", pin.base_commit))
        document = {
            "workspace": str(workspace),
            "base_commit": pin.base_commit,
            "upstreams": list(upstreams),
            "dependency_upstreams": list(dependency_upstreams),
            "next_upstream": 0,
            "result": None,
        }
        self._write(path, document)
        return self._merge_remaining(path, document, workspace)

    def resume_worktree(self, *, run_id: str, attempt_id: str) -> WorktreePreparation:
        document = self._read_attempt(run_id, attempt_id)
        workspace = self._worktree(Path(_string(document, "workspace")))
        return self._merge_remaining(self._attempt_path(run_id, attempt_id), document, workspace)

    def _merge_remaining(
        self, path: Path, document: dict[str, JsonValue], workspace: Path
    ) -> WorktreePreparation:
        upstreams = document.get("upstreams")
        position = document.get("next_upstream")
        if not isinstance(upstreams, list) or type(position) is not int:
            raise MetadataError("Invalid upstream merge cursor")
        while position < len(upstreams):
            conflicts = self._conflicts(workspace)
            if conflicts:
                return WorktreePreparation(workspace, conflicts)
            upstream = upstreams[position]
            if not isinstance(upstream, str):
                raise MetadataError("Invalid upstream commit")
            if self._merge_heads(workspace):
                self._commit_index(workspace, "ehai: integrate resolved upstream code")
            else:
                merge = self._git(
                    workspace,
                    ("merge", "--no-commit", "--no-edit", upstream),
                    check=False,
                )
                if merge.returncode:
                    conflicts = self._conflicts(workspace)
                    if not conflicts:
                        raise MetadataError(merge.stderr.decode("utf-8", errors="replace"))
                    return WorktreePreparation(workspace, conflicts)
                if self._merge_heads(workspace):
                    self._commit_index(workspace, "ehai: integrate upstream code")
            position += 1
            document["next_upstream"] = position
            self._write(path, document)
        conflicts = self._conflicts(workspace)
        if not conflicts and document.get("prepared_commit") is None:
            document["prepared_commit"] = self._commit_id(workspace, "HEAD")
            self._write(path, document)
        return WorktreePreparation(workspace, conflicts)

    def load_attempt_snapshot(self, *, run_id: str, attempt_id: str) -> AttemptSnapshot:
        document = self._read_attempt(run_id, attempt_id)
        workspace = self._owned(Path(_string(document, "workspace")))
        captured = document.get("result")
        if captured is None:
            return AttemptSnapshot(workspace, None)
        if not isinstance(captured, dict):
            raise MetadataError("Invalid captured result")
        result = GitCodeResult(
            str(normalize_id(run_id)),
            str(normalize_id(attempt_id)),
            workspace,
            _string(document, "base_commit"),
            self._commit_id(self.base_workspace, _string(captured, "commit")),
            self._owned(Path(_string(captured, "diff_path"))),
        )
        return AttemptSnapshot(workspace, result)

    def load_attempt_dependency_commits(
        self, *, run_id: str, attempt_id: str
    ) -> tuple[str, ...] | None:
        """Return recorded dependencies; legacy absence never authorizes snapshot reuse."""
        document = self._read_attempt(run_id, attempt_id)
        if "dependency_upstreams" not in document:
            return None
        return self._dependency_baseline(document, attempt_id=attempt_id)

    def capture_handoff(
        self,
        run_id: str,
        attempt_id: str,
        handoff_id: str,
        worktree: Path,
        submission_fingerprint: str,
    ) -> GitCodeResult:
        """Capture one immutable, host-confirmed handoff without touching candidate state."""
        run_key = str(normalize_id(run_id))
        attempt_key = str(normalize_id(attempt_id))
        handoff_key = str(normalize_id(handoff_id))
        fingerprint = _fingerprint(submission_fingerprint)
        metadata_path = self._handoff_path(run_key, attempt_key, handoff_key)
        if metadata_path.exists():
            existing = self.load_handoff_snapshot(
                run_key,
                attempt_key,
                handoff_key,
                fingerprint,
            )
            if existing is None:  # pragma: no cover - metadata_path.exists() above
                raise MetadataError(f"Handoff {handoff_key} metadata disappeared")
            return existing

        pin = self.load_run_base(run_id=run_key)
        workspace = self._worktree(Path(worktree))
        attempt_document = self._read_attempt(run_key, attempt_key)
        if _string(attempt_document, "workspace") != str(workspace):
            raise MetadataError("Attempt does not own this worktree")
        dependencies = self._dependency_baseline(attempt_document, attempt_id=attempt_key)
        patch_path = self._handoff_patch_path(run_key, attempt_key, handoff_key)
        result = self._capture_git_snapshot(
            run_id=run_key,
            attempt_id=attempt_key,
            workspace=workspace,
            base_commit=pin.base_commit,
            diff_path=patch_path,
            message=f"ehai: capture handoff {handoff_key}",
        )
        patch = patch_path.read_bytes()
        metadata: dict[str, JsonValue] = {
            "kind": "handoff",
            "owner": {
                "run_id": run_key,
                "attempt_id": attempt_key,
                "handoff_id": handoff_key,
            },
            "run_id": run_key,
            "attempt_id": attempt_key,
            "handoff_id": handoff_key,
            "submission_fingerprint": fingerprint,
            "base_commit": pin.base_commit,
            "dependencies": list(dependencies),
            "snapshot": {
                "workspace": str(workspace),
                "commit": result.commit,
                "diff_path": str(patch_path),
                "patch_size_bytes": len(patch),
                "patch_sha256": sha256(patch).hexdigest(),
            },
        }
        # The patch is atomically visible before metadata. If metadata publication
        # fails, the orphan patch has no handoff identity and is never loadable.
        self._write(metadata_path, metadata)
        return result

    def load_handoff_snapshot(
        self,
        run_id: str,
        attempt_id: str,
        handoff_id: str,
        submission_fingerprint: str,
    ) -> GitCodeResult | None:
        """Load and fully verify one immutable handoff, or return None if absent."""
        run_key = str(normalize_id(run_id))
        attempt_key = str(normalize_id(attempt_id))
        handoff_key = str(normalize_id(handoff_id))
        fingerprint = _fingerprint(submission_fingerprint)
        metadata_path = self._handoff_path(run_key, attempt_key, handoff_key)
        if not metadata_path.exists():
            return None
        if not metadata_path.is_file():
            raise MetadataError(f"Handoff metadata is not a file: {metadata_path}")

        document = self._read(metadata_path)
        if document.get("kind") != "handoff":
            raise MetadataError(f"Handoff {handoff_key} has an invalid kind")
        owner = _object(document, "owner")
        if (
            _string(owner, "run_id") != run_key
            or _string(owner, "attempt_id") != attempt_key
            or _string(owner, "handoff_id") != handoff_key
        ):
            raise MetadataError(f"Handoff {handoff_key} owner does not match its path")
        if (
            _string(document, "run_id") != run_key
            or _string(document, "attempt_id") != attempt_key
            or _string(document, "handoff_id") != handoff_key
        ):
            raise MetadataError(f"Handoff {handoff_key} owner metadata is inconsistent")
        if _string(document, "submission_fingerprint") != fingerprint:
            raise MetadataError(f"Handoff {handoff_key} submission fingerprint changed")

        pin = self.load_run_base(run_id=run_key)
        base_commit = self._commit_id(self.base_workspace, _string(document, "base_commit"))
        if base_commit != pin.base_commit:
            raise MetadataError(f"Handoff {handoff_key} base does not match its Run")
        attempt_document = self._read_attempt(run_key, attempt_key)
        dependencies = self._dependency_baseline(attempt_document, attempt_id=attempt_key)
        if tuple(_strings(document, "dependencies")) != dependencies:
            raise MetadataError(f"Handoff {handoff_key} dependency baseline changed")
        for dependency in dependencies:
            self._commit_id(self.base_workspace, dependency)

        snapshot = _object(document, "snapshot")
        workspace = self._owned(Path(_string(snapshot, "workspace")))
        if _string(attempt_document, "workspace") != str(workspace):
            raise MetadataError(f"Handoff {handoff_key} workspace owner changed")
        commit = self._commit_id(self.base_workspace, _string(snapshot, "commit"))
        self._git(self.base_workspace, ("merge-base", "--is-ancestor", pin.base_commit, commit))
        patch_path = self._handoff_patch_path(run_key, attempt_key, handoff_key)
        if _string(snapshot, "diff_path") != str(patch_path):
            raise MetadataError(f"Handoff {handoff_key} patch path does not match its identity")
        if not patch_path.is_file():
            raise MetadataError(f"Handoff {handoff_key} patch is missing")
        try:
            patch = patch_path.read_bytes()
        except OSError as error:
            raise MetadataError(f"Handoff {handoff_key} patch cannot be read") from error
        if type(snapshot.get("patch_size_bytes")) is not int:
            raise MetadataError(f"Handoff {handoff_key} patch size is invalid")
        if snapshot["patch_size_bytes"] != len(patch):
            raise MetadataError(f"Handoff {handoff_key} patch size changed")
        patch_digest = _string(snapshot, "patch_sha256").lower()
        if not _is_sha256(patch_digest) or patch_digest != sha256(patch).hexdigest():
            raise MetadataError(f"Handoff {handoff_key} patch digest changed")
        expected_patch = self._git(
            self.base_workspace,
            ("diff", "--binary", "--full-index", "--no-ext-diff", pin.base_commit, commit),
        ).stdout
        if expected_patch != patch:
            raise MetadataError(f"Handoff {handoff_key} patch does not match its commit")
        return GitCodeResult(run_key, attempt_key, workspace, pin.base_commit, commit, patch_path)

    def capture_result(
        self,
        *,
        run_id: str,
        attempt_id: str,
        worktree: Path,
        message: str | None = None,
    ) -> GitCodeResult:
        workspace = self._worktree(worktree)
        document = self._read_attempt(run_id, attempt_id)
        if _string(document, "workspace") != str(workspace):
            raise MetadataError("Attempt does not own this worktree")
        snapshot = self.load_attempt_snapshot(run_id=run_id, attempt_id=attempt_id)
        if snapshot.result is not None:
            return snapshot.result
        diff_path = self._path(run_id, f"{normalize_id(attempt_id)}.patch")
        result = self._capture_git_snapshot(
            run_id=run_id,
            attempt_id=attempt_id,
            workspace=workspace,
            base_commit=self.load_run_base(run_id=run_id).base_commit,
            diff_path=diff_path,
            message=message or "ehai: capture candidate code",
        )
        document = self._read_attempt(run_id, attempt_id)
        document["result"] = {"commit": result.commit, "diff_path": str(diff_path)}
        self._write(self._attempt_path(run_id, attempt_id), document)
        return result

    def _capture_git_snapshot(
        self,
        *,
        run_id: str,
        attempt_id: str,
        workspace: Path,
        base_commit: str,
        diff_path: Path,
        message: str,
    ) -> GitCodeResult:
        """Capture the current worktree without deciding its candidate meaning."""
        preparation = self.resume_worktree(run_id=run_id, attempt_id=attempt_id)
        if preparation.conflicts:
            raise MetadataError(
                "Resolve and stage Git conflicts before submitting: "
                + ", ".join(preparation.conflicts)
            )
        self._git(workspace, ("add", "--all", "--", "."))
        ignored = self._text(workspace, ("ls-files", "-ci", "--exclude-standard"))
        changed = self._text(workspace, ("diff", "--cached", "--name-only"))
        if set(ignored.splitlines()).intersection(changed.splitlines()):
            raise MetadataError("Refusing to capture changed ignored files")
        if changed:
            self._commit_index(workspace, message)
        commit = self._commit_id(workspace, "HEAD")
        self._git(workspace, ("merge-base", "--is-ancestor", base_commit, commit))
        patch = self._git(
            workspace,
            ("diff", "--binary", "--full-index", "--no-ext-diff", base_commit, commit),
        ).stdout
        self._write_bytes(diff_path, patch)
        return GitCodeResult(
            str(normalize_id(run_id)),
            str(normalize_id(attempt_id)),
            workspace,
            base_commit,
            commit,
            diff_path,
        )

    def prepare_adoption_worktree(self, adoption: ResultAdoption) -> Path:
        """Prepare an isolated check workspace for a host-authorized accepted result.

        The caller must load the adoption from durable state and validate its
        artifact bytes. This Git operation does not approve result suitability.
        A different target base requires integration and fresh result evidence;
        it cannot silently use the old repository context for target checks.
        """
        source = self.load_attempt_snapshot(
            run_id=str(adoption.source_run_id), attempt_id=str(adoption.source_attempt_id)
        ).result
        if source is None:
            raise MetadataError("Adopted producer has no submitted code snapshot")
        target_base = self.load_run_base(run_id=str(adoption.target_run_id))
        if target_base.base_commit != source.base_commit:
            raise MetadataError("Adopted code needs integration against the changed target base")
        workspace = self._owned(self.owned_root / f"adoption-{adoption.adoption_id}")
        path = self._path(adoption.target_run_id, f"adoption-{adoption.adoption_id}.json")
        expected: dict[str, JsonValue] = {
            "adoption": adoption.to_dict(),
            "repository": str(self._repository()),
            "workspace": str(workspace),
            "base_commit": target_base.base_commit,
            "source_commit": source.commit,
        }
        if path.exists():
            if self._read(path) != expected:
                raise MetadataError("Adoption workspace identity or accepted code changed")
        else:
            if workspace.exists():
                raise MetadataError("Unregistered adoption workspace already exists")
            # Retain intent before invoking Git. A missing directory can be
            # created after interruption, but existing dirty state is not reset.
            self._write(path, expected)
        if not workspace.exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                self.base_workspace,
                ("worktree", "add", "--detach", str(workspace), source.commit),
            )
        verified = self._worktree(workspace)
        if self._commit_id(verified, "HEAD") != source.commit:
            raise MetadataError("Adoption workspace no longer contains its accepted commit")
        if self._merge_heads(verified) or self._text(verified, ("status", "--porcelain")):
            raise MetadataError("Adoption workspace changed; retain it for investigation")
        return verified

    def read_diff(self, result: GitCodeResult) -> bytes:
        return self._owned(result.diff_path).read_bytes()

    def prepared_commit(self, *, run_id: str, attempt_id: str) -> str:
        """Return the immutable code baseline recorded before a Worker started."""
        document = self._read_attempt(run_id, attempt_id)
        return self._commit_id(self.base_workspace, _string(document, "prepared_commit"))

    def _commit_index(self, workspace: Path, message: str) -> None:
        parents = (self._commit_id(workspace, "HEAD"), *self._merge_heads(workspace))
        tree = self._text(workspace, ("write-tree",))
        arguments = ["commit-tree", tree]
        for parent in dict.fromkeys(parents):
            arguments.extend(("-p", parent))
        commit = self._git(workspace, arguments, input_bytes=message.encode("utf-8")).stdout
        self._git(workspace, ("update-ref", "HEAD", commit.decode().strip(), parents[0]))
        if self._merge_heads(workspace):
            self._git(workspace, ("merge", "--quit"))

    def _merge_heads(self, workspace: Path) -> tuple[str, ...]:
        path = Path(self._text(workspace, ("rev-parse", "--git-path", "MERGE_HEAD")))
        if not path.is_absolute():
            path = workspace / path
        return tuple(path.read_text().splitlines()) if path.is_file() else ()

    def _conflicts(self, workspace: Path) -> tuple[str, ...]:
        return tuple(self._text(workspace, ("diff", "--name-only", "--diff-filter=U")).splitlines())

    def _read_attempt(self, run_id: str, attempt_id: str) -> dict[str, JsonValue]:
        base = self.load_run_base(run_id=run_id)
        document = self._read(self._attempt_path(run_id, attempt_id))
        if document.get("base_commit") != base.base_commit:
            raise MetadataError("Attempt base does not match its Run")
        return document

    def _attempt_path(self, run_id: str, attempt_id: str) -> Path:
        return self._path(run_id, f"{normalize_id(attempt_id)}.json")

    def _handoff_path(self, run_id: str, attempt_id: str, handoff_id: str) -> Path:
        return self._path(
            run_id,
            f"{normalize_id(attempt_id)}-handoff-{normalize_id(handoff_id)}.json",
        )

    def _handoff_patch_path(self, run_id: str, attempt_id: str, handoff_id: str) -> Path:
        return self._path(
            run_id,
            f"{normalize_id(attempt_id)}-handoff-{normalize_id(handoff_id)}.patch",
        )

    def _path(self, run_id: str, name: str) -> Path:
        return self._owned(self.metadata_root / str(normalize_id(run_id)) / name)

    def _owned(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved == self.owned_root or not resolved.is_relative_to(self.owned_root):
            raise MetadataError(f"Path is outside the owned workspace root: {resolved}")
        return resolved

    def _worktree(self, path: Path) -> Path:
        workspace = self._owned(path)
        if workspace == self.base_workspace or workspace.is_relative_to(self.metadata_root):
            raise MetadataError("The path is not an execution worktree")
        root = Path(self._text(workspace, ("rev-parse", "--show-toplevel"))).resolve()
        common = Path(
            self._text(workspace, ("rev-parse", "--path-format=absolute", "--git-common-dir"))
        ).resolve()
        detached = self._git(workspace, ("symbolic-ref", "-q", "HEAD"), check=False)
        if root != workspace or common != self._repository() or detached.returncode != 1:
            raise MetadataError("Expected a detached worktree of the authorized repository")
        return workspace

    def _commit_id(self, workspace: Path, reference: str) -> str:
        if not reference or reference.startswith("-") or "\x00" in reference:
            raise ValueError("Invalid Git commit reference")
        return self._text(workspace, ("rev-parse", "--verify", f"{reference}^{{commit}}"))

    def _text(self, workspace: Path, arguments: Sequence[str]) -> str:
        return self._git(workspace, arguments).stdout.decode("utf-8").strip()

    def _git(
        self,
        workspace: Path,
        arguments: Sequence[str],
        *,
        check: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.hooksPath=" + str(self.metadata_root / "disabled-hooks"),
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=EHAI",
                "-c",
                "user.email=ehai@localhost",
                "-C",
                str(workspace),
                *arguments,
            ],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if check and result.returncode:
            raise MetadataError(result.stderr.decode("utf-8", errors="replace").strip())
        return result

    @staticmethod
    def _read(path: Path) -> dict[str, JsonValue]:
        try:
            document = json_loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise MetadataError(f"Cannot read code state: {path}") from error
        if not isinstance(document, dict):
            raise MetadataError(f"Code state must be an object: {path}")
        return document

    @staticmethod
    def _write(path: Path, document: dict[str, JsonValue]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json_dumps(document).encode("utf-8"))
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _dependency_baseline(
        self, document: dict[str, JsonValue], *, attempt_id: str
    ) -> tuple[str, ...]:
        values = document.get("dependency_upstreams")
        if not isinstance(values, list):
            raise MetadataError(f"Attempt {attempt_id} has no valid persisted dependency baseline")
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise MetadataError(
                    f"Attempt {attempt_id} has no valid persisted dependency baseline"
                )
            normalized.append(value)
        try:
            return tuple(self._commit_id(self.base_workspace, value) for value in normalized)
        except (MetadataError, ValueError) as error:
            raise MetadataError(
                f"Attempt {attempt_id} has an invalid persisted dependency baseline"
            ) from error


def _string(document: dict[str, JsonValue], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise MetadataError(f"Code state has no string {name}")
    return value


def _object(document: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise MetadataError(f"Code state has no object {name}")
    return value


def _strings(document: dict[str, JsonValue], name: str) -> tuple[str, ...]:
    value = document.get(name)
    if not isinstance(value, list):
        raise MetadataError(f"Code state has no string array {name}")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MetadataError(f"Code state has no string array {name}")
        strings.append(item)
    return tuple(strings)


def _fingerprint(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("submission_fingerprint must be non-blank")
    normalized = value.strip()
    if len(normalized) > 4096 or "\x00" in normalized:
        raise ValueError("submission_fingerprint is invalid")
    return normalized


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
