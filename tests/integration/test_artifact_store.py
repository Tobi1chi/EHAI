from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from ehai import ID, new_id
from ehai.application.ports import ArtifactStore
from ehai.domain.artifacts import Artifact, ArtifactKind
from ehai.infrastructure.artifacts import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    FilesystemArtifactStore,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def test_store_round_trips_unicode_and_binary_bytes(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    content = "探索结果".encode() + b"\x00\xff\x10"
    run_id = new_id()
    node_id = new_id()
    attempt_id = new_id()

    artifact = store.store(
        content,
        kind=ArtifactKind.CANDIDATE,
        name="候选结果.bin",
        media_type="application/octet-stream",
        run_id=run_id,
        plan_node_id=node_id,
        attempt_id=attempt_id,
        created_at=NOW,
    )

    assert store.read(artifact.artifact_id) == content
    assert store.get(artifact.artifact_id) == artifact
    assert artifact.size_bytes == len(content)
    assert artifact.sha256 == sha256(content).hexdigest()
    assert artifact.created_at.tzinfo is UTC
    assert (store.root / artifact.relative_path).read_bytes() == content
    assert isinstance(store, ArtifactStore)


def test_artifact_metadata_and_source_containers_are_immutable(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    content = b"evidence"
    artifact = store.store(
        content,
        kind=ArtifactKind.EVIDENCE,
        name="proof.txt",
        media_type="text/plain",
        created_at=NOW,
    )

    with pytest.raises(FrozenInstanceError):
        artifact.name = "changed"  # type: ignore[misc]
    assert artifact.to_dict().keys().isdisjoint({"content", "bytes", "data"})
    assert store.read(artifact.artifact_id) == content


def test_put_is_idempotent_but_same_id_cannot_overwrite_content(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    original = store.store(
        b"first",
        kind=ArtifactKind.WORKER_OUTPUT,
        name="raw.out",
        media_type="application/octet-stream",
        created_at=NOW,
    )
    store.put(original, b"first")

    replacement_bytes = b"second"
    replacement = replace(
        original,
        size_bytes=len(replacement_bytes),
        sha256=sha256(replacement_bytes).hexdigest(),
    )
    with pytest.raises(ArtifactConflictError, match=r"different metadata|different content"):
        store.put(replacement, replacement_bytes)

    assert store.read(original.artifact_id) == b"first"


@pytest.mark.parametrize("tamper", [b"short", b"forst"])
def test_read_fails_closed_on_size_or_hash_tampering(tmp_path: Path, tamper: bytes) -> None:
    store = FilesystemArtifactStore(tmp_path)
    artifact = store.store(
        b"first",
        kind=ArtifactKind.LOG,
        name="worker.log",
        media_type="text/plain",
        created_at=NOW,
    )
    (store.root / artifact.relative_path).write_bytes(tamper)

    with pytest.raises(ArtifactIntegrityError, match=r"size mismatch|SHA-256 mismatch"):
        store.read(artifact.artifact_id)


@pytest.mark.parametrize(
    "relative_path",
    ["../outside", "objects/../../outside", "/absolute/path", r"objects\escape"],
)
def test_artifact_metadata_rejects_unsafe_relative_paths(relative_path: str) -> None:
    with pytest.raises(ValueError, match="relative_path is unsafe"):
        Artifact(
            artifact_id=new_id(),
            kind=ArtifactKind.LOG,
            name="log",
            media_type="text/plain",
            size_bytes=0,
            sha256=sha256(b"").hexdigest(),
            relative_path=relative_path,
            created_at=NOW,
        )


def test_store_rejects_safe_path_not_owned_by_artifact_id(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    content = b"content"
    artifact = Artifact(
        artifact_id=new_id(),
        kind=ArtifactKind.LOG,
        name="log",
        media_type="text/plain",
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
        relative_path="objects/aa/not-the-artifact.blob",
        created_at=NOW,
    )

    with pytest.raises(ArtifactIntegrityError, match="expected"):
        store.put(artifact, content)


def test_tampered_metadata_cannot_escape_store_root(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    artifact = store.store(
        b"content",
        kind=ArtifactKind.EVIDENCE,
        name="evidence",
        media_type="text/plain",
        created_at=NOW,
    )
    metadata_path = (
        store.root / "metadata" / artifact.artifact_id[:2] / f"{artifact.artifact_id}.json"
    )
    outside_name = f"outside-{artifact.artifact_id}"
    document = metadata_path.read_text(encoding="utf-8")
    metadata_path.write_text(
        document.replace(artifact.relative_path, f"../{outside_name}"),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactIntegrityError, match="metadata failed validation"):
        store.get(artifact.artifact_id)
    assert not (store.root.parent / outside_name).exists()


def test_missing_artifact_has_optional_get_and_explicit_read_error(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    missing_id = new_id()

    assert store.get(missing_id) is None
    with pytest.raises(ArtifactNotFoundError, match=str(missing_id)):
        store.read(missing_id)


def test_list_for_run_is_stable_filtered_and_integrity_checked(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    run_id = new_id()
    older = store.store(
        b"older",
        kind=ArtifactKind.LOG,
        name="older.log",
        media_type="text/plain",
        run_id=run_id,
        created_at=NOW,
    )
    newer = store.store(
        b"newer",
        kind=ArtifactKind.CHECK_OUTPUT,
        name="newer.log",
        media_type="text/plain",
        run_id=run_id,
        created_at=NOW + timedelta(seconds=1),
    )
    store.store(
        b"other",
        kind=ArtifactKind.LOG,
        name="other.log",
        media_type="text/plain",
        run_id=new_id(),
        created_at=NOW,
    )

    assert store.list_for_run(run_id) == (older, newer)


def test_attempt_ownership_requires_run_and_node() -> None:
    artifact_id = new_id()
    common: dict[str, object] = {
        "artifact_id": artifact_id,
        "kind": ArtifactKind.CANDIDATE,
        "name": "candidate",
        "media_type": "text/plain",
        "size_bytes": 0,
        "sha256": sha256(b"").hexdigest(),
        "relative_path": f"objects/{artifact_id[:2]}/{artifact_id}.blob",
        "created_at": NOW,
    }

    with pytest.raises(ValueError, match="attempt_id requires"):
        Artifact(**common, attempt_id=new_id())  # type: ignore[arg-type]


def test_metadata_round_trip_is_strict_and_does_not_include_raw_bytes(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    run_id: ID = new_id()
    artifact = store.store(
        b"payload that belongs only in the object file",
        kind=ArtifactKind.PATCH,
        name="change.patch",
        media_type="text/x-diff",
        run_id=run_id,
        created_at=NOW,
    )

    restored = Artifact.from_dict(artifact.to_dict())
    metadata_path = (
        store.root / "metadata" / artifact.artifact_id[:2] / f"{artifact.artifact_id}.json"
    )
    metadata_text = metadata_path.read_text(encoding="utf-8")

    assert restored == artifact
    assert "payload that belongs only in the object file" not in metadata_text
