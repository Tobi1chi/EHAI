"""Immutable code commits and resumable merges in EHAI-owned Git worktrees."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from ehai import JsonValue, json_dumps, json_loads, normalize_id


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
    ) -> WorktreePreparation:
        workspace = self._worktree(worktree)
        pin = self.pin_run_base(run_id=run_id, base_commit=base_commit)
        path = self._attempt_path(run_id, attempt_id)
        upstreams = [self._commit_id(workspace, commit) for commit in upstream_commits]
        if path.exists():
            document = self._read_attempt(run_id, attempt_id)
            if document.get("upstreams") != upstreams or document.get("workspace") != str(
                workspace
            ):
                raise MetadataError("Attempt workspace or predecessor commits changed")
            return self.resume_worktree(run_id=run_id, attempt_id=attempt_id)
        if self._text(workspace, ("status", "--porcelain")):
            raise MetadataError("New execution worktree must be clean before preparation")
        self._git(workspace, ("checkout", "--detach", pin.base_commit))
        document = {
            "workspace": str(workspace),
            "base_commit": pin.base_commit,
            "upstreams": list(upstreams),
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
        return WorktreePreparation(workspace, self._conflicts(workspace))

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
            self._commit_index(workspace, message or "ehai: capture candidate code")
        commit = self._commit_id(workspace, "HEAD")
        base = self.load_run_base(run_id=run_id).base_commit
        self._git(workspace, ("merge-base", "--is-ancestor", base, commit))
        patch = self._git(
            workspace, ("diff", "--binary", "--full-index", "--no-ext-diff", base, commit)
        ).stdout
        diff_path = self._path(run_id, f"{normalize_id(attempt_id)}.patch")
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_bytes(patch)
        document = self._read_attempt(run_id, attempt_id)
        document["result"] = {"commit": commit, "diff_path": str(diff_path)}
        self._write(self._attempt_path(run_id, attempt_id), document)
        return GitCodeResult(run_id, attempt_id, workspace, base, commit, diff_path)

    def read_diff(self, result: GitCodeResult) -> bytes:
        return self._owned(result.diff_path).read_bytes()

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


def _string(document: dict[str, JsonValue], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise MetadataError(f"Code state has no string {name}")
    return value
