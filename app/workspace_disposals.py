from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ExecutionArtifactManifest,
    ExecutionAttempt,
    ExecutionStageRun,
    ExecutionStageVersion,
    ExecutionWorkspaceDisposal,
    ExecutionWorkspaceDisposalVersion,
)
from app.provenance import content_hash
from app.security import ensure_no_sensitive_data

if TYPE_CHECKING:
    from app.sandbox_worker.implementation import DisposableWorkspace


WORKSPACE_DISPOSAL_VERSION = "execution-workspace-disposal-v1"
MAX_WORKSPACE_DISPOSAL_ATTEMPTS = 3
_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_REASON = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


class WorkspaceDisposalStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkspaceDisposalError(RuntimeError):
    pass


class WorkspaceDestroyClient(Protocol):
    async def destroy(self, workspace: "DisposableWorkspace") -> None: ...


class ExecutionWorkspaceDisposalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def prepare(
        self,
        *,
        verify_stage_run_id: str,
        artifact_manifest_id: str,
        now: datetime | None = None,
        commit: bool = True,
    ) -> ExecutionWorkspaceDisposal:
        run = self.session.get(ExecutionStageRun, verify_stage_run_id)
        manifest = self.session.get(
            ExecutionArtifactManifest,
            artifact_manifest_id,
        )
        if run is None or manifest is None:
            raise WorkspaceDisposalError(
                "Workspace disposal provenance was not found"
            )
        attempt = self.session.get(
            ExecutionAttempt,
            run.execution_attempt_id,
        )
        running = self.session.scalar(
            select(ExecutionStageVersion)
            .where(
                ExecutionStageVersion.execution_attempt_id
                == run.execution_attempt_id,
                ExecutionStageVersion.stage == "verify",
                ExecutionStageVersion.status == "running",
                ExecutionStageVersion.previous_stage_state_hash
                == run.pending_stage_record_hash,
            )
            .order_by(ExecutionStageVersion.sequence.desc())
        )
        if (
            attempt is None
            or running is None
            or run.stage != "verify"
            or manifest.execution_stage_run_id != run.id
            or manifest.execution_attempt_id != attempt.id
            or manifest.stage != "verify"
            or running.workspace_id is None
            or running.workspace_ref is None
            or running.workspace_inventory_hash is None
        ):
            raise WorkspaceDisposalError(
                "Workspace disposal provenance does not match Verify"
            )
        payload = workspace_disposal_payload(
            execution_attempt_id=attempt.id,
            verify_stage_run_id=run.id,
            artifact_manifest_id=manifest.id,
            workspace_id=running.workspace_id,
            workspace_ref=running.workspace_ref,
            workspace_inventory_hash=running.workspace_inventory_hash,
            runner_image_digest=attempt.runner_image_digest,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
        )
        existing = self.session.scalar(
            select(ExecutionWorkspaceDisposal).where(
                ExecutionWorkspaceDisposal.execution_attempt_id == attempt.id
            )
        )
        if existing is not None:
            verified = self.get_verified(existing.id)
            if verified.record_hash != content_hash(payload):
                raise WorkspaceDisposalError(
                    "Workspace disposal already has different provenance"
                )
            return verified
        timestamp = _aware(now)
        disposal = ExecutionWorkspaceDisposal(
            id=str(uuid4()),
            execution_attempt_id=attempt.id,
            verify_stage_run_id=run.id,
            artifact_manifest_id=manifest.id,
            schema_version=WORKSPACE_DISPOSAL_VERSION,
            workspace_id=running.workspace_id,
            workspace_ref=running.workspace_ref,
            workspace_inventory_hash=running.workspace_inventory_hash,
            runner_image_digest=attempt.runner_image_digest,
            sandbox_policy_hash=attempt.sandbox_policy_hash,
            record_hash=content_hash(payload),
            created_at=timestamp,
        )
        initial = _version(
            disposal=disposal,
            sequence=1,
            status=WorkspaceDisposalStatus.PENDING,
            reason_code="artifacts_finalized",
            worker_id=None,
            previous_version_hash=None,
            now=timestamp,
        )
        ensure_no_sensitive_data(
            {
                "disposal": payload,
                "initial": workspace_disposal_version_payload(initial),
            },
            context="execution workspace disposal",
        )
        self.session.add_all((disposal, initial))
        if commit:
            self.session.commit()
        else:
            self.session.flush()
        return disposal

    def get_verified(
        self,
        disposal_id: str,
    ) -> ExecutionWorkspaceDisposal:
        disposal = self.session.get(
            ExecutionWorkspaceDisposal,
            disposal_id,
        )
        if disposal is None:
            raise WorkspaceDisposalError(
                "Execution workspace disposal was not found"
            )
        attempt = self.session.get(
            ExecutionAttempt,
            disposal.execution_attempt_id,
        )
        run = self.session.get(
            ExecutionStageRun,
            disposal.verify_stage_run_id,
        )
        manifest = self.session.get(
            ExecutionArtifactManifest,
            disposal.artifact_manifest_id,
        )
        running = self.session.scalar(
            select(ExecutionStageVersion).where(
                ExecutionStageVersion.execution_attempt_id
                == disposal.execution_attempt_id,
                ExecutionStageVersion.stage == "verify",
                ExecutionStageVersion.status == "running",
                ExecutionStageVersion.previous_stage_state_hash
                == (
                    run.pending_stage_record_hash
                    if run is not None
                    else ""
                ),
            )
        )
        payload = workspace_disposal_payload(
            execution_attempt_id=disposal.execution_attempt_id,
            verify_stage_run_id=disposal.verify_stage_run_id,
            artifact_manifest_id=disposal.artifact_manifest_id,
            workspace_id=disposal.workspace_id,
            workspace_ref=disposal.workspace_ref,
            workspace_inventory_hash=disposal.workspace_inventory_hash,
            runner_image_digest=disposal.runner_image_digest,
            sandbox_policy_hash=disposal.sandbox_policy_hash,
        )
        if (
            disposal.schema_version != WORKSPACE_DISPOSAL_VERSION
            or attempt is None
            or run is None
            or manifest is None
            or running is None
            or run.execution_attempt_id != attempt.id
            or run.stage != "verify"
            or manifest.execution_stage_run_id != run.id
            or manifest.execution_attempt_id != attempt.id
            or manifest.stage != "verify"
            or disposal.workspace_id != running.workspace_id
            or disposal.workspace_ref != running.workspace_ref
            or disposal.workspace_inventory_hash
            != running.workspace_inventory_hash
            or disposal.runner_image_digest != attempt.runner_image_digest
            or disposal.sandbox_policy_hash != attempt.sandbox_policy_hash
            or disposal.record_hash != content_hash(payload)
        ):
            raise WorkspaceDisposalError(
                "Execution workspace disposal provenance is invalid"
            )
        _identifier(disposal.workspace_id, name="workspace ID")
        _identifier(disposal.workspace_ref, name="workspace reference")
        _hash(disposal.workspace_inventory_hash)
        self.history(disposal.id, _verified_disposal=disposal)
        return disposal

    def history(
        self,
        disposal_id: str,
        *,
        _verified_disposal: ExecutionWorkspaceDisposal | None = None,
    ) -> tuple[ExecutionWorkspaceDisposalVersion, ...]:
        disposal = _verified_disposal or self.session.get(
            ExecutionWorkspaceDisposal,
            disposal_id,
        )
        if disposal is None:
            raise WorkspaceDisposalError(
                "Execution workspace disposal was not found"
            )
        versions = tuple(
            self.session.scalars(
                select(ExecutionWorkspaceDisposalVersion)
                .where(
                    ExecutionWorkspaceDisposalVersion.disposal_id
                    == disposal.id
                )
                .order_by(ExecutionWorkspaceDisposalVersion.sequence)
            )
        )
        previous: ExecutionWorkspaceDisposalVersion | None = None
        for sequence, version in enumerate(versions, start=1):
            expected = workspace_disposal_version_payload(version)
            if (
                version.sequence != sequence
                or version.disposal_record_hash != disposal.record_hash
                or version.previous_version_hash
                != (previous.record_hash if previous is not None else None)
                or version.record_hash != content_hash(expected)
                or not _legal(previous, version)
            ):
                raise WorkspaceDisposalError(
                    "Execution workspace disposal history is invalid"
                )
            previous = version
        if not versions:
            raise WorkspaceDisposalError(
                "Execution workspace disposal has no state"
            )
        return versions

    def current(
        self,
        disposal_id: str,
    ) -> ExecutionWorkspaceDisposalVersion:
        disposal = self.get_verified(disposal_id)
        return self.history(
            disposal.id,
            _verified_disposal=disposal,
        )[-1]

    def workspace(
        self,
        disposal_id: str,
    ) -> "DisposableWorkspace":
        from app.sandbox_worker.implementation import DisposableWorkspace

        disposal = self.get_verified(disposal_id)
        return DisposableWorkspace(
            workspace_id=disposal.workspace_id,
            volume_name=disposal.workspace_ref,
            runner_image_digest=disposal.runner_image_digest,
            sandbox_policy_hash=disposal.sandbox_policy_hash,
            inventory_hash=disposal.workspace_inventory_hash,
        )

    def claim(
        self,
        disposal_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> ExecutionWorkspaceDisposalVersion:
        worker = _identifier(worker_id, name="cleanup worker ID")
        disposal = self.get_verified(disposal_id)
        current = self.history(
            disposal.id,
            _verified_disposal=disposal,
        )[-1]
        if current.status == WorkspaceDisposalStatus.RUNNING.value:
            if current.worker_id == worker:
                return current
            raise WorkspaceDisposalError(
                "Workspace disposal is owned by another cleanup worker"
            )
        if current.status != WorkspaceDisposalStatus.PENDING.value:
            raise WorkspaceDisposalError(
                "Only a pending workspace disposal can be claimed"
            )
        attempts = sum(
            item.status == WorkspaceDisposalStatus.RUNNING.value
            for item in self.history(
                disposal.id,
                _verified_disposal=disposal,
            )
        )
        if attempts >= MAX_WORKSPACE_DISPOSAL_ATTEMPTS:
            raise WorkspaceDisposalError(
                "Workspace disposal exhausted its retry budget"
            )
        return self._append(
            disposal,
            current,
            status=WorkspaceDisposalStatus.RUNNING,
            reason_code="cleanup_started",
            worker_id=worker,
            now=now,
        )

    def succeed(
        self,
        disposal_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> ExecutionWorkspaceDisposalVersion:
        return self._finish(
            disposal_id,
            worker_id=worker_id,
            status=WorkspaceDisposalStatus.SUCCEEDED,
            reason_code="workspace_destroyed",
            now=now,
        )

    def fail(
        self,
        disposal_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> ExecutionWorkspaceDisposalVersion:
        return self._finish(
            disposal_id,
            worker_id=worker_id,
            status=WorkspaceDisposalStatus.FAILED,
            reason_code="workspace_destroy_failed",
            now=now,
        )

    def retry(
        self,
        disposal_id: str,
        *,
        now: datetime | None = None,
    ) -> ExecutionWorkspaceDisposalVersion:
        disposal = self.get_verified(disposal_id)
        current = self.history(
            disposal.id,
            _verified_disposal=disposal,
        )[-1]
        if current.status != WorkspaceDisposalStatus.FAILED.value:
            raise WorkspaceDisposalError(
                "Only a failed workspace disposal can retry"
            )
        attempts = sum(
            item.status == WorkspaceDisposalStatus.RUNNING.value
            for item in self.history(
                disposal.id,
                _verified_disposal=disposal,
            )
        )
        if attempts >= MAX_WORKSPACE_DISPOSAL_ATTEMPTS:
            raise WorkspaceDisposalError(
                "Workspace disposal exhausted its retry budget"
            )
        return self._append(
            disposal,
            current,
            status=WorkspaceDisposalStatus.PENDING,
            reason_code="cleanup_retry_scheduled",
            worker_id=None,
            now=now,
        )

    def mark_worker_lost(
        self,
        disposal_id: str,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> ExecutionWorkspaceDisposalVersion:
        worker = _identifier(worker_id, name="cleanup worker ID")
        disposal = self.get_verified(disposal_id)
        current = self.history(
            disposal.id,
            _verified_disposal=disposal,
        )[-1]
        if (
            current.status != WorkspaceDisposalStatus.RUNNING.value
            or current.worker_id != worker
        ):
            raise WorkspaceDisposalError(
                "Workspace disposal worker-loss evidence is stale"
            )
        return self._append(
            disposal,
            current,
            status=WorkspaceDisposalStatus.FAILED,
            reason_code="cleanup_worker_lost",
            worker_id=worker,
            now=now,
        )

    async def destroy(
        self,
        disposal_id: str,
        *,
        worker_id: str,
        client: WorkspaceDestroyClient,
        now: datetime | None = None,
    ) -> ExecutionWorkspaceDisposalVersion:
        current = self.current(disposal_id)
        if current.status == WorkspaceDisposalStatus.SUCCEEDED.value:
            return current
        if current.status == WorkspaceDisposalStatus.FAILED.value:
            self.retry(disposal_id, now=now)
        self.claim(disposal_id, worker_id=worker_id, now=now)
        workspace = self.workspace(disposal_id)
        try:
            await client.destroy(workspace)
        except Exception:
            self.fail(disposal_id, worker_id=worker_id, now=now)
            raise WorkspaceDisposalError(
                "Sandbox Worker could not confirm workspace destruction"
            ) from None
        return self.succeed(
            disposal_id,
            worker_id=worker_id,
            now=now,
        )

    def _finish(
        self,
        disposal_id: str,
        *,
        worker_id: str,
        status: WorkspaceDisposalStatus,
        reason_code: str,
        now: datetime | None,
    ) -> ExecutionWorkspaceDisposalVersion:
        worker = _identifier(worker_id, name="cleanup worker ID")
        disposal = self.get_verified(disposal_id)
        current = self.history(
            disposal.id,
            _verified_disposal=disposal,
        )[-1]
        if current.status == status.value and current.worker_id == worker:
            return current
        if (
            current.status != WorkspaceDisposalStatus.RUNNING.value
            or current.worker_id != worker
        ):
            raise WorkspaceDisposalError(
                "Only the owning cleanup worker can finish disposal"
            )
        return self._append(
            disposal,
            current,
            status=status,
            reason_code=reason_code,
            worker_id=worker,
            now=now,
        )

    def _append(
        self,
        disposal: ExecutionWorkspaceDisposal,
        current: ExecutionWorkspaceDisposalVersion,
        *,
        status: WorkspaceDisposalStatus,
        reason_code: str,
        worker_id: str | None,
        now: datetime | None,
    ) -> ExecutionWorkspaceDisposalVersion:
        version = _version(
            disposal=disposal,
            sequence=current.sequence + 1,
            status=status,
            reason_code=reason_code,
            worker_id=worker_id,
            previous_version_hash=current.record_hash,
            now=_aware(now),
        )
        self.session.add(version)
        self.session.commit()
        return version


def workspace_disposal_payload(
    *,
    execution_attempt_id: str,
    verify_stage_run_id: str,
    artifact_manifest_id: str,
    workspace_id: str,
    workspace_ref: str,
    workspace_inventory_hash: str,
    runner_image_digest: str,
    sandbox_policy_hash: str,
) -> dict[str, object]:
    return {
        "version": WORKSPACE_DISPOSAL_VERSION,
        "execution_attempt_id": execution_attempt_id,
        "verify_stage_run_id": verify_stage_run_id,
        "artifact_manifest_id": artifact_manifest_id,
        "workspace_id": workspace_id,
        "workspace_ref": workspace_ref,
        "workspace_inventory_hash": workspace_inventory_hash,
        "runner_image_digest": runner_image_digest,
        "sandbox_policy_hash": sandbox_policy_hash,
    }


def workspace_disposal_version_payload(
    version: ExecutionWorkspaceDisposalVersion,
) -> dict[str, object]:
    return {
        "disposal_id": version.disposal_id,
        "sequence": version.sequence,
        "status": version.status,
        "reason_code": version.reason_code,
        "worker_id": version.worker_id,
        "disposal_record_hash": version.disposal_record_hash,
        "previous_version_hash": version.previous_version_hash,
    }


def _version(
    *,
    disposal: ExecutionWorkspaceDisposal,
    sequence: int,
    status: WorkspaceDisposalStatus,
    reason_code: str,
    worker_id: str | None,
    previous_version_hash: str | None,
    now: datetime,
) -> ExecutionWorkspaceDisposalVersion:
    reason = _reason_code(reason_code)
    payload = {
        "disposal_id": disposal.id,
        "sequence": sequence,
        "status": status.value,
        "reason_code": reason,
        "worker_id": worker_id,
        "disposal_record_hash": disposal.record_hash,
        "previous_version_hash": previous_version_hash,
    }
    return ExecutionWorkspaceDisposalVersion(
        id=str(uuid4()),
        disposal_id=disposal.id,
        sequence=sequence,
        status=status.value,
        reason_code=reason,
        worker_id=worker_id,
        disposal_record_hash=disposal.record_hash,
        previous_version_hash=previous_version_hash,
        record_hash=content_hash(payload),
        created_at=now,
    )


def _legal(
    previous: ExecutionWorkspaceDisposalVersion | None,
    current: ExecutionWorkspaceDisposalVersion,
) -> bool:
    if previous is None:
        return (
            current.sequence == 1
            and current.status == WorkspaceDisposalStatus.PENDING.value
            and current.reason_code == "artifacts_finalized"
            and current.worker_id is None
        )
    edge = (previous.status, current.status)
    if edge == (
        WorkspaceDisposalStatus.PENDING.value,
        WorkspaceDisposalStatus.RUNNING.value,
    ):
        return (
            current.reason_code == "cleanup_started"
            and current.worker_id is not None
        )
    if edge == (
        WorkspaceDisposalStatus.RUNNING.value,
        WorkspaceDisposalStatus.SUCCEEDED.value,
    ):
        return (
            current.reason_code == "workspace_destroyed"
            and current.worker_id == previous.worker_id
        )
    if edge == (
        WorkspaceDisposalStatus.RUNNING.value,
        WorkspaceDisposalStatus.FAILED.value,
    ):
        return (
            current.reason_code
            in {"workspace_destroy_failed", "cleanup_worker_lost"}
            and current.worker_id == previous.worker_id
        )
    if edge == (
        WorkspaceDisposalStatus.FAILED.value,
        WorkspaceDisposalStatus.PENDING.value,
    ):
        return (
            current.reason_code == "cleanup_retry_scheduled"
            and current.worker_id is None
        )
    return False


def _identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise WorkspaceDisposalError(f"Execution {name} is invalid")
    ensure_no_sensitive_data(value, context=f"Execution {name}")
    return value


def _hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise WorkspaceDisposalError(
            "Execution workspace disposal hash is invalid"
        )
    return value


def _reason_code(value: object) -> str:
    if not isinstance(value, str) or not _REASON.fullmatch(value):
        raise WorkspaceDisposalError(
            "Execution workspace disposal reason is invalid"
        )
    return value


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
