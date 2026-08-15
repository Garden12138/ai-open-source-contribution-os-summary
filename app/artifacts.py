from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Artifact,
    ExecutionArtifactEntry,
    ExecutionArtifactManifest,
    ExecutionStageRun,
    ExecutionStageVersion,
    Job,
    JobArtifact,
)
from app.provenance import canonical_json, content_hash
from app.sandbox_worker.specs import SandboxStage
from app.security import (
    ensure_no_sensitive_bytes,
    ensure_no_sensitive_data,
)


class ArtifactError(RuntimeError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


class ArtifactNotFoundError(ArtifactError):
    pass


EXECUTION_ARTIFACT_MANIFEST_VERSION = "execution-artifact-manifest-v1"
MAX_EXECUTION_ARTIFACT_BUNDLE_BYTES = 96_000_000
_EXECUTION_ROLES = {
    SandboxStage.EXPLORE: ("stage-result",),
    SandboxStage.IMPLEMENT: (
        "stage-result",
        "file-inventory",
        "unified-diff",
    ),
    SandboxStage.VERIFY: (
        "stage-result",
        "normalized-test-results",
    ),
}


@dataclass(frozen=True, slots=True)
class ExecutionArtifactEntryView:
    position: int
    role: str
    artifact_id: str
    algorithm: str
    size_bytes: int
    media_type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionArtifactManifestView:
    id: str
    job_id: str
    execution_stage_run_id: str
    execution_attempt_id: str
    schema_version: str
    stage: str
    job_spec_hash: str
    result_hash: str
    entry_count: int
    manifest_hash: str
    created_at: datetime
    entries: tuple[ExecutionArtifactEntryView, ...]


@dataclass(frozen=True, slots=True)
class ExecutionArtifactBlob:
    manifest_id: str
    stage: str
    role: str
    artifact_id: str
    size_bytes: int
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ExecutionArtifactContent:
    role: str
    data: bytes
    media_type: str
    artifact_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.role, str)
            or not self.role
            or len(self.role) > 80
        ):
            raise ValueError("Execution artifact role is invalid")
        if not isinstance(self.data, bytes):
            raise ValueError("Execution artifact content is invalid")
        if (
            not isinstance(self.media_type, str)
            or not self.media_type
            or len(self.media_type) > 255
        ):
            raise ValueError("Execution artifact media type is invalid")
        if (
            not isinstance(self.artifact_id, str)
            or len(self.artifact_id) != 64
            or any(character not in "0123456789abcdef" for character in self.artifact_id)
            or hashlib.sha256(self.data).hexdigest() != self.artifact_id
        ):
            raise ArtifactIntegrityError(
                "Execution artifact content hash does not match"
            )
        ensure_no_sensitive_bytes(
            self.data,
            context=f"execution artifact {self.role}",
        )
        ensure_no_sensitive_data(
            {
                "role": self.role,
                "media_type": self.media_type,
                "artifact_id": self.artifact_id,
            },
            context="execution artifact metadata",
        )

    @classmethod
    def create(
        cls,
        *,
        role: str,
        data: bytes,
        media_type: str,
        expected_hash: str | None = None,
    ) -> "ExecutionArtifactContent":
        actual = hashlib.sha256(data).hexdigest()
        if expected_hash is not None and actual != expected_hash:
            raise ArtifactIntegrityError(
                f"Execution artifact {role} does not match its bound hash"
            )
        return cls(
            role=role,
            data=data,
            media_type=media_type,
            artifact_id=actual,
        )


@dataclass(frozen=True, slots=True)
class ExecutionArtifactBundle:
    stage: SandboxStage
    result_hash: str
    contents: tuple[ExecutionArtifactContent, ...]

    def __post_init__(self) -> None:
        try:
            stage = SandboxStage(self.stage)
        except (TypeError, ValueError) as exc:
            raise ValueError("Execution artifact stage is invalid") from exc
        object.__setattr__(self, "stage", stage)
        contents = tuple(self.contents)
        expected_roles = _EXECUTION_ROLES[stage]
        if (
            any(
                not isinstance(item, ExecutionArtifactContent)
                for item in contents
            )
            or sum(len(item.data) for item in contents)
            > MAX_EXECUTION_ARTIFACT_BUNDLE_BYTES
        ):
            raise ValueError("Execution artifact bundle is invalid")
        if tuple(item.role for item in contents) != expected_roles:
            raise ValueError("Execution artifact bundle is invalid")
        object.__setattr__(self, "contents", contents)
        stage_result = contents[0]
        if stage_result.artifact_id != self.result_hash:
            raise ArtifactIntegrityError(
                "Execution stage result artifact does not match result hash"
            )

    @classmethod
    def from_result(cls, result: object) -> "ExecutionArtifactBundle":
        from app.sandbox_worker.explore import ExploreResult
        from app.sandbox_worker.implementation import ImplementResult
        from app.sandbox_worker.verification import VerifyResult

        if isinstance(result, ExploreResult):
            stage = SandboxStage.EXPLORE
        elif isinstance(result, ImplementResult):
            stage = SandboxStage.IMPLEMENT
        elif isinstance(result, VerifyResult):
            stage = SandboxStage.VERIFY
        else:
            raise ValueError("Execution stage result type is unsupported")
        payload = result.hash_payload()
        if result.result_hash != content_hash(payload):
            raise ArtifactIntegrityError(
                "Execution stage result provenance is invalid"
            )
        contents: list[ExecutionArtifactContent] = [
            ExecutionArtifactContent.create(
                role="stage-result",
                data=canonical_json(payload).encode("utf-8"),
                media_type=(
                    "application/vnd.contribos.stage-result+json"
                ),
                expected_hash=result.result_hash,
            )
        ]
        if isinstance(result, ImplementResult):
            inventory_payload = {
                "version": "execution-file-inventory-v1",
                "baseline_inventory_hash": result.baseline_inventory_hash,
                "result_inventory_hash": result.result_inventory_hash,
                "baseline_file_count": result.baseline_file_count,
                "result_file_count": result.result_file_count,
                "baseline_total_bytes": result.baseline_total_bytes,
                "result_total_bytes": result.result_total_bytes,
                "changed_paths": list(result.changed_paths),
                "baseline_inventory": [
                    item.to_wire() for item in result.baseline_inventory
                ],
                "result_inventory": [
                    item.to_wire() for item in result.result_inventory
                ],
            }
            contents.extend(
                (
                    ExecutionArtifactContent.create(
                        role="file-inventory",
                        data=canonical_json(inventory_payload).encode("utf-8"),
                        media_type=(
                            "application/vnd.contribos.file-inventory+json"
                        ),
                    ),
                    ExecutionArtifactContent.create(
                        role="unified-diff",
                        data=result.unified_diff.encode("utf-8"),
                        media_type="text/x-diff; charset=utf-8",
                        expected_hash=result.diff_hash,
                    ),
                )
            )
        elif isinstance(result, VerifyResult):
            tests_payload = [
                item.to_wire() for item in result.test_results
            ]
            contents.append(
                ExecutionArtifactContent.create(
                    role="normalized-test-results",
                    data=canonical_json(tests_payload).encode("utf-8"),
                    media_type=(
                        "application/vnd.contribos.test-results+json"
                    ),
                    expected_hash=result.test_results_hash,
                )
            )
        return cls(
            stage=stage,
            result_hash=result.result_hash,
            contents=tuple(contents),
        )


class ArtifactStore:
    def __init__(
        self,
        session: Session,
        root: str | Path,
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> None:
        self.session = session
        self.root = Path(root).expanduser().resolve()
        self.secrets = secrets

    def store_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        now: datetime | None = None,
        commit: bool = True,
    ) -> Artifact:
        now = self._aware(now)
        ensure_no_sensitive_bytes(
            data,
            secrets=self.secrets,
            context="artifact content",
        )
        ensure_no_sensitive_data(
            media_type,
            secrets=self.secrets,
            context="artifact media type",
        )
        digest = hashlib.sha256(data).hexdigest()
        storage_key = self._storage_key(digest)
        target = self._path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            self._verify_file(target, digest, len(data))
        else:
            self._atomic_write(target, data)
            self._verify_file(target, digest, len(data))

        artifact = self.session.get(Artifact, digest)
        if artifact is not None:
            if (
                artifact.algorithm != "sha256"
                or artifact.size_bytes != len(data)
                or artifact.storage_key != storage_key
                or artifact.media_type != media_type
            ):
                raise ArtifactIntegrityError(
                    f"Artifact metadata does not match content {digest}"
                )
            return artifact

        artifact = Artifact(
            id=digest,
            algorithm="sha256",
            size_bytes=len(data),
            media_type=media_type[:255],
            storage_key=storage_key,
            created_at=now,
        )
        self.session.add(artifact)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return artifact

    def attach(
        self,
        *,
        job_id: str,
        artifact_id: str,
        role: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> JobArtifact:
        now = self._aware(now)
        role = role.strip()
        if not role or len(role) > 80:
            raise ValueError("Artifact role must contain between 1 and 80 characters")
        ensure_no_sensitive_data(
            role,
            secrets=self.secrets,
            context="artifact role",
        )
        if self.session.get(Job, job_id) is None:
            raise ArtifactNotFoundError(f"Job {job_id} was not found")
        if self.session.get(Artifact, artifact_id) is None:
            raise ArtifactNotFoundError(f"Artifact {artifact_id} was not found")
        existing = self.session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == job_id,
                JobArtifact.artifact_id == artifact_id,
                JobArtifact.role == role,
            )
        )
        if existing is not None:
            return existing

        link = JobArtifact(
            id=str(uuid4()),
            job_id=job_id,
            artifact_id=artifact_id,
            role=role,
            created_at=now,
        )
        self.session.add(link)
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return link

    def finalize_execution_bundle(
        self,
        *,
        stage_run_id: str,
        bundle: ExecutionArtifactBundle,
        now: datetime | None = None,
        commit: bool = True,
    ) -> ExecutionArtifactManifest:
        if not isinstance(bundle, ExecutionArtifactBundle):
            raise ValueError("Execution artifact bundle is invalid")
        timestamp = self._aware(now)
        run = self.session.get(ExecutionStageRun, stage_run_id)
        if run is None:
            raise ArtifactNotFoundError(
                "Execution stage run for artifact finalization was not found"
            )
        if run.stage != bundle.stage.value:
            raise ArtifactIntegrityError(
                "Execution artifact stage does not match its stage run"
            )
        existing = self.session.scalar(
            select(ExecutionArtifactManifest).where(
                ExecutionArtifactManifest.execution_stage_run_id
                == stage_run_id
            )
        )
        if existing is not None:
            self._verify_execution_manifest(existing, bundle)
            return existing
        if run.job.state != "running":
            raise ArtifactIntegrityError(
                "Only a running stage can finalize execution artifacts"
            )

        artifacts: list[Artifact] = []
        for content in bundle.contents:
            artifact = self.store_bytes(
                content.data,
                media_type=content.media_type,
                now=timestamp,
                commit=False,
            )
            if artifact.id != content.artifact_id:
                raise ArtifactIntegrityError(
                    "Finalized execution artifact identity changed"
                )
            artifacts.append(artifact)

        manifest_id = str(uuid4())
        payload = self._execution_manifest_payload(
            manifest_id=manifest_id,
            run=run,
            bundle=bundle,
            artifacts=tuple(artifacts),
        )
        manifest = ExecutionArtifactManifest(
            id=manifest_id,
            job_id=run.job_id,
            execution_stage_run_id=run.id,
            execution_attempt_id=run.execution_attempt_id,
            schema_version=EXECUTION_ARTIFACT_MANIFEST_VERSION,
            stage=run.stage,
            job_spec_hash=run.job_spec_hash,
            result_hash=bundle.result_hash,
            entry_count=len(bundle.contents),
            manifest_hash=content_hash(payload),
            created_at=timestamp,
        )
        self.session.add(manifest)
        self.session.flush()
        for position, (content, artifact) in enumerate(
            zip(bundle.contents, artifacts, strict=True)
        ):
            self.session.add(
                ExecutionArtifactEntry(
                    id=str(uuid4()),
                    manifest_id=manifest.id,
                    position=position,
                    role=content.role,
                    artifact_id=artifact.id,
                    created_at=timestamp,
                )
            )
            self.attach(
                job_id=run.job_id,
                artifact_id=artifact.id,
                role=content.role,
                now=timestamp,
                commit=False,
            )
        self.session.flush()
        self._verify_execution_manifest(manifest, bundle)
        if commit:
            self.session.commit()
        return manifest

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.session.get(Artifact, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(f"Artifact {artifact_id} was not found")
        path = self._path(artifact.storage_key)
        self._verify_file(path, artifact.id, artifact.size_bytes)
        data = path.read_bytes()
        if (
            len(data) != artifact.size_bytes
            or hashlib.sha256(data).hexdigest() != artifact.id
        ):
            raise ArtifactIntegrityError(
                f"Artifact content changed during read: {artifact.id}"
            )
        ensure_no_sensitive_bytes(
            data,
            secrets=self.secrets,
            context="artifact content",
        )
        return data

    def verify(self, artifact_id: str) -> None:
        artifact = self.session.get(Artifact, artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError(f"Artifact {artifact_id} was not found")
        self._verify_file(
            self._path(artifact.storage_key),
            artifact.id,
            artifact.size_bytes,
        )

    def execution_manifests(
        self,
        execution_attempt_id: str,
    ) -> tuple[ExecutionArtifactManifestView, ...]:
        manifests = tuple(
            self.session.scalars(
                select(ExecutionArtifactManifest)
                .where(
                    ExecutionArtifactManifest.execution_attempt_id
                    == execution_attempt_id
                )
                .order_by(
                    ExecutionArtifactManifest.created_at,
                    ExecutionArtifactManifest.id,
                )
            )
        )
        return tuple(
            self._verify_execution_manifest_records(
                manifest,
                require_terminal=True,
            )
            for manifest in manifests
        )

    def read_execution_artifact(
        self,
        *,
        execution_attempt_id: str,
        artifact_id: str,
    ) -> ExecutionArtifactBlob:
        manifest = self.session.scalar(
            select(ExecutionArtifactManifest)
            .join(
                ExecutionArtifactEntry,
                ExecutionArtifactEntry.manifest_id
                == ExecutionArtifactManifest.id,
            )
            .where(
                ExecutionArtifactManifest.execution_attempt_id
                == execution_attempt_id,
                ExecutionArtifactEntry.artifact_id == artifact_id,
            )
            .order_by(
                ExecutionArtifactManifest.created_at,
                ExecutionArtifactManifest.id,
            )
            .limit(1)
        )
        if manifest is None:
            raise ArtifactNotFoundError(
                "Execution artifact was not found"
            )
        verified = self._verify_execution_manifest_records(
            manifest,
            require_terminal=True,
        )
        entry = next(
            (
                item
                for item in verified.entries
                if item.artifact_id == artifact_id
            ),
            None,
        )
        if entry is None:
            raise ArtifactIntegrityError(
                "Execution artifact manifest entry is missing"
            )
        data = self.read_bytes(entry.artifact_id)
        if len(data) != entry.size_bytes:
            raise ArtifactIntegrityError(
                "Execution artifact size changed during read"
            )
        return ExecutionArtifactBlob(
            manifest_id=verified.id,
            stage=verified.stage,
            role=entry.role,
            artifact_id=entry.artifact_id,
            size_bytes=entry.size_bytes,
            media_type=entry.media_type,
            data=data,
        )

    def _verify_execution_manifest(
        self,
        manifest: ExecutionArtifactManifest,
        bundle: ExecutionArtifactBundle,
    ) -> None:
        verified = self._verify_execution_manifest_records(
            manifest,
            require_terminal=False,
        )
        if (
            verified.stage != bundle.stage.value
            or verified.result_hash != bundle.result_hash
            or len(verified.entries) != len(bundle.contents)
            or any(
                entry.position != position
                or entry.role != content.role
                or entry.artifact_id != content.artifact_id
                or entry.size_bytes != len(content.data)
                or entry.media_type != content.media_type
                for position, (entry, content) in enumerate(
                    zip(
                        verified.entries,
                        bundle.contents,
                        strict=True,
                    )
                )
            )
        ):
            raise ArtifactIntegrityError(
                "Execution artifact manifest entry does not match"
            )

    def _verify_execution_manifest_records(
        self,
        manifest: ExecutionArtifactManifest,
        *,
        require_terminal: bool,
    ) -> ExecutionArtifactManifestView:
        run = self.session.get(
            ExecutionStageRun,
            manifest.execution_stage_run_id,
        )
        if run is None:
            raise ArtifactIntegrityError(
                "Execution artifact stage run is missing"
            )
        job = self.session.get(Job, manifest.job_id)
        if job is None:
            raise ArtifactIntegrityError(
                "Execution artifact Job is missing"
            )
        try:
            stage = SandboxStage(manifest.stage)
        except ValueError as exc:
            raise ArtifactIntegrityError(
                "Execution artifact stage is invalid"
            ) from exc
        entries = tuple(
            self.session.scalars(
                select(ExecutionArtifactEntry)
                .where(ExecutionArtifactEntry.manifest_id == manifest.id)
                .order_by(ExecutionArtifactEntry.position)
            )
        )
        expected_roles = _EXECUTION_ROLES[stage]
        artifacts: list[Artifact] = []
        views: list[ExecutionArtifactEntryView] = []
        for position, entry in enumerate(entries):
            artifact = self.session.get(Artifact, entry.artifact_id)
            if artifact is None:
                raise ArtifactIntegrityError(
                    "Execution artifact metadata is missing"
                )
            if (
                position >= len(expected_roles)
                or entry.position != position
                or entry.role != expected_roles[position]
                or artifact.algorithm != "sha256"
                or artifact.id != entry.artifact_id
                or artifact.storage_key != self._storage_key(artifact.id)
                or artifact.size_bytes < 0
                or not artifact.media_type
                or len(artifact.media_type) > 255
            ):
                raise ArtifactIntegrityError(
                    "Execution artifact manifest entry does not match"
                )
            link = self.session.scalar(
                select(JobArtifact).where(
                    JobArtifact.job_id == manifest.job_id,
                    JobArtifact.artifact_id == artifact.id,
                    JobArtifact.role == entry.role,
                )
            )
            if link is None:
                raise ArtifactIntegrityError(
                    "Execution artifact Job link is missing"
                )
            ensure_no_sensitive_data(
                {
                    "role": entry.role,
                    "artifact_id": artifact.id,
                    "media_type": artifact.media_type,
                },
                secrets=self.secrets,
                context="execution artifact metadata",
            )
            self._verify_file(
                self._path(artifact.storage_key),
                artifact.id,
                artifact.size_bytes,
            )
            artifacts.append(artifact)
            views.append(
                ExecutionArtifactEntryView(
                    position=entry.position,
                    role=entry.role,
                    artifact_id=artifact.id,
                    algorithm=artifact.algorithm,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                    created_at=self._aware(entry.created_at),
                )
            )
        payload = {
            "version": EXECUTION_ARTIFACT_MANIFEST_VERSION,
            "manifest_id": manifest.id,
            "job_id": run.job_id,
            "execution_stage_run_id": run.id,
            "execution_attempt_id": run.execution_attempt_id,
            "stage": stage.value,
            "job_spec_hash": run.job_spec_hash,
            "result_hash": manifest.result_hash,
            "entries": [
                {
                    "position": entry.position,
                    "role": entry.role,
                    "artifact_id": artifact.id,
                    "size_bytes": artifact.size_bytes,
                    "media_type": artifact.media_type,
                }
                for entry, artifact in zip(
                    entries,
                    artifacts,
                    strict=True,
                )
            ],
        }
        if (
            len(entries) != len(expected_roles)
            or manifest.schema_version
            != EXECUTION_ARTIFACT_MANIFEST_VERSION
            or manifest.job_id != run.job_id
            or manifest.execution_attempt_id != run.execution_attempt_id
            or run.stage != stage.value
            or manifest.job_spec_hash != run.job_spec_hash
            or manifest.result_hash != entries[0].artifact_id
            or manifest.entry_count != len(expected_roles)
            or manifest.manifest_hash != content_hash(payload)
        ):
            raise ArtifactIntegrityError(
                "Execution artifact manifest does not match its content"
            )
        if require_terminal:
            terminal = self.session.scalar(
                select(ExecutionStageVersion)
                .where(
                    ExecutionStageVersion.execution_attempt_id
                    == manifest.execution_attempt_id,
                    ExecutionStageVersion.stage == stage.value,
                    ExecutionStageVersion.status == "succeeded",
                    ExecutionStageVersion.job_spec_hash
                    == manifest.job_spec_hash,
                    ExecutionStageVersion.result_hash
                    == manifest.result_hash,
                )
                .order_by(ExecutionStageVersion.sequence.desc())
                .limit(1)
            )
            expected_result = {
                "result_hash": manifest.result_hash,
                "artifact_manifest_hash": manifest.manifest_hash,
            }
            if (
                job.id != run.job_id
                or job.kind != "sandbox_stage"
                or job.state != "succeeded"
                or job.result_data != expected_result
                or terminal is None
            ):
                raise ArtifactIntegrityError(
                    "Execution artifact is not bound to a successful stage"
                )
        return ExecutionArtifactManifestView(
            id=manifest.id,
            job_id=manifest.job_id,
            execution_stage_run_id=manifest.execution_stage_run_id,
            execution_attempt_id=manifest.execution_attempt_id,
            schema_version=manifest.schema_version,
            stage=stage.value,
            job_spec_hash=manifest.job_spec_hash,
            result_hash=manifest.result_hash,
            entry_count=manifest.entry_count,
            manifest_hash=manifest.manifest_hash,
            created_at=self._aware(manifest.created_at),
            entries=tuple(views),
        )

    @staticmethod
    def _execution_manifest_payload(
        *,
        manifest_id: str,
        run: ExecutionStageRun,
        bundle: ExecutionArtifactBundle,
        artifacts: tuple[Artifact, ...],
    ) -> dict[str, object]:
        if len(artifacts) != len(bundle.contents):
            raise ArtifactIntegrityError(
                "Execution artifact manifest content is incomplete"
            )
        return {
            "version": EXECUTION_ARTIFACT_MANIFEST_VERSION,
            "manifest_id": manifest_id,
            "job_id": run.job_id,
            "execution_stage_run_id": run.id,
            "execution_attempt_id": run.execution_attempt_id,
            "stage": bundle.stage.value,
            "job_spec_hash": run.job_spec_hash,
            "result_hash": bundle.result_hash,
            "entries": [
                {
                    "position": position,
                    "role": content.role,
                    "artifact_id": artifact.id,
                    "size_bytes": artifact.size_bytes,
                    "media_type": artifact.media_type,
                }
                for position, (content, artifact) in enumerate(
                    zip(bundle.contents, artifacts, strict=True)
                )
            ],
        }

    def _path(self, storage_key: str) -> Path:
        path = (self.root / storage_key).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactIntegrityError("Artifact path escapes the storage root") from exc
        return path

    @staticmethod
    def _storage_key(digest: str) -> str:
        return f"sha256/{digest[:2]}/{digest}"

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify_file(path: Path, digest: str, size_bytes: int) -> None:
        if not path.is_file():
            raise ArtifactIntegrityError(f"Artifact content is missing: {digest}")
        hasher = hashlib.sha256()
        actual_size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual_size += len(chunk)
                hasher.update(chunk)
        if actual_size != size_bytes or hasher.hexdigest() != digest:
            raise ArtifactIntegrityError(f"Artifact content hash mismatch: {digest}")

    @staticmethod
    def _aware(value: datetime | None) -> datetime:
        current = value or datetime.now(timezone.utc)
        return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
