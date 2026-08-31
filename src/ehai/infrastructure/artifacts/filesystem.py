"""Atomic, immutable filesystem storage for Artifact bytes and metadata."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from ehai import ID, json_dumps, json_loads, new_id, normalize_id, utc_now
from ehai.domain.artifacts import Artifact, ArtifactKind


class ArtifactStoreError(RuntimeError):
    """Base class for filesystem Artifact failures."""


class ArtifactNotFoundError(FileNotFoundError, ArtifactStoreError):
    """Raised when an Artifact ID has no stored metadata and bytes."""


class ArtifactConflictError(ArtifactStoreError):
    """Raised when an immutable Artifact ID is reused for different data."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when stored metadata, size, path, or digest cannot be trusted."""


class FilesystemArtifactStore:
    """Store immutable Artifact bytes beneath one controlled root directory.

    Sidecar metadata lets this adapter implement ``get`` and ``list_for_run``
    after a restart. It contains references and checksums only; raw Artifact
    bytes are kept exclusively in the object file.
    """

    def __init__(self, root: str | Path) -> None:
        requested_root = Path(root).expanduser()
        requested_root.mkdir(parents=True, exist_ok=True)
        if not requested_root.is_dir():
            raise ValueError(f"Artifact root is not a directory: {requested_root}")
        self._root = requested_root.resolve(strict=True)

    @property
    def root(self) -> Path:
        """Return the normalized storage root."""
        return self._root

    def store(
        self,
        content: bytes,
        *,
        kind: ArtifactKind | str,
        name: str,
        media_type: str,
        run_id: ID | None = None,
        plan_node_id: ID | None = None,
        attempt_id: ID | None = None,
        artifact_id: ID | None = None,
        created_at: datetime | None = None,
    ) -> Artifact:
        """Create metadata for bytes, persist both immutably, and return it."""
        if not isinstance(content, bytes):
            raise TypeError("Artifact content must be bytes")
        if created_at is not None and not isinstance(created_at, datetime):
            raise TypeError("created_at must be a datetime")

        resolved_id = new_id() if artifact_id is None else normalize_id(artifact_id)
        artifact = Artifact(
            artifact_id=resolved_id,
            kind=ArtifactKind(kind),
            name=name,
            media_type=media_type,
            size_bytes=len(content),
            sha256=sha256(content).hexdigest(),
            relative_path=self.relative_path_for(resolved_id),
            created_at=utc_now() if created_at is None else created_at,
            run_id=run_id,
            plan_node_id=plan_node_id,
            attempt_id=attempt_id,
        )
        self.put(artifact, content)
        return artifact

    def put(self, artifact: Artifact, content: bytes) -> None:
        """Persist one pre-built Artifact without overwriting an existing ID."""
        if not isinstance(artifact, Artifact):
            raise TypeError("artifact must be an Artifact")
        if not isinstance(content, bytes):
            raise TypeError("Artifact content must be bytes")
        self._verify_supplied_content(artifact, content)

        content_path = self._content_path(artifact)
        metadata_path = self._metadata_path(artifact.artifact_id)
        existing = self._load_metadata(artifact.artifact_id)
        if existing is not None:
            if existing != artifact:
                raise ArtifactConflictError(
                    f"Artifact {artifact.artifact_id} already has different metadata"
                )
            stored = self._read_content(existing)
            if stored != content:
                raise ArtifactConflictError(
                    f"Artifact {artifact.artifact_id} already has different content"
                )
            return

        created_content = self._atomic_create(content_path, content)
        if not created_content:
            self._verify_file_matches(artifact, content_path, conflict=True)

        metadata_bytes = json_dumps(artifact.to_dict()).encode("utf-8")
        created_metadata = self._atomic_create(metadata_path, metadata_bytes)
        if not created_metadata:
            raced = self._load_metadata(artifact.artifact_id)
            if raced is None or raced != artifact:
                raise ArtifactConflictError(
                    f"Artifact {artifact.artifact_id} already has different metadata"
                )

    def get(self, artifact_id: ID) -> Artifact | None:
        """Return validated metadata, or ``None`` when the ID is absent."""
        normalized_id = normalize_id(artifact_id)
        artifact = self._load_metadata(normalized_id)
        if artifact is None:
            return None
        self._read_content(artifact)
        return artifact

    def read(self, artifact_id: ID) -> bytes:
        """Read bytes only after validating path, size, and SHA-256 metadata."""
        normalized_id = normalize_id(artifact_id)
        artifact = self._load_metadata(normalized_id)
        if artifact is None:
            raise ArtifactNotFoundError(f"Artifact {normalized_id} was not found")
        return self._read_content(artifact)

    def list_for_run(self, run_id: ID) -> tuple[Artifact, ...]:
        """Return a stable, integrity-checked metadata snapshot for one Run."""
        normalized_run_id = normalize_id(run_id)
        metadata_root = self._root / "metadata"
        if not metadata_root.exists():
            return ()

        artifacts: list[Artifact] = []
        for metadata_path in metadata_root.glob("*/*.json"):
            artifact_id_text = metadata_path.stem
            try:
                artifact_id = normalize_id(artifact_id_text)
            except ValueError as error:
                raise ArtifactIntegrityError(
                    f"unexpected Artifact metadata filename: {metadata_path.name}"
                ) from error
            artifact = self.get(artifact_id)
            if artifact is not None and artifact.run_id == normalized_run_id:
                artifacts.append(artifact)
        return tuple(sorted(artifacts, key=lambda item: (item.created_at, item.artifact_id)))

    def relative_path_for(self, artifact_id: ID) -> str:
        """Return the only content path accepted for an Artifact ID."""
        normalized_id = normalize_id(artifact_id)
        return f"objects/{normalized_id[:2]}/{normalized_id}.blob"

    def _content_path(self, artifact: Artifact) -> Path:
        expected = self.relative_path_for(artifact.artifact_id)
        if artifact.relative_path != expected:
            raise ArtifactIntegrityError(
                f"Artifact {artifact.artifact_id} path is {artifact.relative_path!r}, "
                f"expected {expected!r}"
            )
        return self._resolve_under_root(artifact.relative_path)

    def _metadata_path(self, artifact_id: ID) -> Path:
        normalized_id = normalize_id(artifact_id)
        return self._resolve_under_root(f"metadata/{normalized_id[:2]}/{normalized_id}.json")

    def _resolve_under_root(self, relative_path: str) -> Path:
        candidate = self._root.joinpath(*relative_path.split("/"))
        current = candidate
        while current != self._root:
            if current.exists() and current.is_symlink():
                raise ArtifactIntegrityError(f"Artifact path uses a symlink: {current}")
            current = current.parent
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self._root):
            raise ArtifactIntegrityError(f"Artifact path escapes storage root: {relative_path}")
        return candidate

    def _load_metadata(self, artifact_id: ID) -> Artifact | None:
        metadata_path = self._metadata_path(artifact_id)
        if not metadata_path.exists():
            return None
        if not metadata_path.is_file():
            raise ArtifactIntegrityError(f"Artifact metadata is not a file: {metadata_path}")
        try:
            document = metadata_path.read_text(encoding="utf-8")
            decoded = json_loads(document)
            if not isinstance(decoded, dict):
                raise ValueError("metadata must contain a JSON object")
            artifact = Artifact.from_dict(decoded)
        except (OSError, UnicodeError, ValueError) as error:
            raise ArtifactIntegrityError(
                f"Artifact {artifact_id} metadata failed validation"
            ) from error
        if artifact.artifact_id != artifact_id:
            raise ArtifactIntegrityError(
                f"Artifact metadata ID {artifact.artifact_id} does not match {artifact_id}"
            )
        self._content_path(artifact)
        return artifact

    def _read_content(self, artifact: Artifact) -> bytes:
        path = self._content_path(artifact)
        if not path.exists():
            raise ArtifactIntegrityError(f"Artifact {artifact.artifact_id} content is missing")
        if not path.is_file():
            raise ArtifactIntegrityError(f"Artifact {artifact.artifact_id} content is not a file")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise ArtifactIntegrityError(
                f"Artifact {artifact.artifact_id} content could not be read"
            ) from error
        self._verify_supplied_content(artifact, content)
        return content

    def _verify_file_matches(
        self,
        artifact: Artifact,
        path: Path,
        *,
        conflict: bool,
    ) -> None:
        if not path.is_file():
            raise ArtifactIntegrityError(f"Artifact {artifact.artifact_id} path is not a file")
        content = path.read_bytes()
        try:
            self._verify_supplied_content(artifact, content)
        except ArtifactIntegrityError as error:
            if conflict:
                raise ArtifactConflictError(
                    f"Artifact {artifact.artifact_id} already has different content"
                ) from error
            raise

    @staticmethod
    def _verify_supplied_content(artifact: Artifact, content: bytes) -> None:
        if len(content) != artifact.size_bytes:
            raise ArtifactIntegrityError(
                f"Artifact {artifact.artifact_id} size mismatch: expected "
                f"{artifact.size_bytes}, got {len(content)}"
            )
        actual_hash = sha256(content).hexdigest()
        if actual_hash != artifact.sha256:
            raise ArtifactIntegrityError(
                f"Artifact {artifact.artifact_id} SHA-256 mismatch: expected "
                f"{artifact.sha256}, got {actual_hash}"
            )

    @staticmethod
    def _atomic_create(path: Path, content: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                return False
            return True
        finally:
            temporary_path.unlink(missing_ok=True)
