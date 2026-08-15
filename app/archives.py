from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.approvals import PlanApprovalService
from app.config import Settings
from app.database import Database
from app.jobs import JobService, JobTransitionError
from app.models import Job, OpportunitySnapshot
from app.plan_locks import PlanLockService
from app.planning import ContributionTaskService
from app.plans import PlanVersionService
from app.security import contains_sensitive_text, ensure_no_sensitive_data


REPOSITORY_ARCHIVE_JOB_KIND = "repository_archive"
REPOSITORY_ARCHIVE_JOB_VERSION = "repository-archive-job-v1"
# Must match app.sandbox_worker.explore.MAX_REPOSITORY_ARCHIVE_BYTES.
MAX_REPOSITORY_ARCHIVE_BYTES = 512 * 1024 * 1024
_HASH = re.compile(r"^[0-9a-f]{64}$")
_BASE_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)


class RepositoryArchiveError(RuntimeError):
    pass


class RepositoryArchiveConflictError(RepositoryArchiveError):
    pass


class RepositoryArchiveStore:
    def __init__(self, root: Path | str) -> None:
        self.root = (Path(root) / "repository-archives").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, archive_hash: str) -> Path:
        if not _HASH.fullmatch(archive_hash):
            raise ValueError("Repository archive hash is invalid")
        return self.root / archive_hash[:2] / archive_hash

    def put_file(self, source: Path) -> str:
        incoming = Path(source)
        if incoming.is_symlink() or not incoming.is_file():
            raise ValueError("Repository archive source must be a regular file")
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".archive-",
            suffix=".tmp",
            dir=self.root,
        )
        temporary = Path(temporary_name)
        hasher = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as stream, incoming.open("rb") as body:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_REPOSITORY_ARCHIVE_BYTES:
                        raise ValueError("Repository archive size is invalid")
                    hasher.update(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if size < 1:
                raise ValueError("Repository archive size is invalid")
            digest = hasher.hexdigest()
            target = self.path_for(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if (
                    target.is_symlink()
                    or not target.is_file()
                    or target.stat().st_size != size
                    or _file_hash(target) != digest
                ):
                    raise RepositoryArchiveError(
                        "Repository archive hash collision"
                    )
                return digest
            os.replace(temporary, target)
            directory_descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            return digest
        finally:
            temporary.unlink(missing_ok=True)

    def lookup(
        self,
        archive_hash: str,
        *,
        repository_full_name: str,
        base_commit_sha: str,
    ) -> Any | None:
        if not _HASH.fullmatch(archive_hash):
            raise ValueError("Repository archive hash is invalid")
        path = self.path_for(archive_hash)
        if not path.is_file():
            return None
        from app.sandbox_worker.explore import RepositoryArchive

        captured = RepositoryArchive.capture(
            repository_full_name=repository_full_name,
            base_commit_sha=base_commit_sha,
            path=path,
            allowed_root=self.root,
        )
        if captured.archive_hash != archive_hash:
            raise RepositoryArchiveError(
                "Stored repository archive hash mismatch"
            )
        return captured


class RepositoryArchiveService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        plan_version_id: str,
        plan_approval_id: str,
        repository_full_name: str,
        base_commit_sha: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[Job, bool]:
        if not _REPOSITORY.fullmatch(repository_full_name):
            raise ValueError("Repository full name is invalid")
        if not _BASE_SHA.fullmatch(base_commit_sha):
            raise ValueError("Repository commit SHA is invalid")
        payload = {
            "schema_version": REPOSITORY_ARCHIVE_JOB_VERSION,
            "plan_version_id": plan_version_id,
            "plan_approval_id": plan_approval_id,
            "repository_full_name": repository_full_name,
            "base_commit_sha": base_commit_sha,
        }
        ensure_no_sensitive_data(payload, context="repository archive job")
        return JobService(self.session).enqueue(
            kind=REPOSITORY_ARCHIVE_JOB_KIND,
            idempotency_key=idempotency_key,
            payload=payload,
            timeout_seconds=600,
            now=now,
        )

    def enqueue_for_approved_plan(
        self,
        *,
        plan_version_id: str,
        approval_id: str,
        base_commit_sha: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[Job, bool]:
        plan = PlanVersionService(self.session).get_verified(plan_version_id)
        approval = PlanApprovalService(self.session).get_verified(approval_id)
        if approval.plan_version_id != plan.id:
            raise RepositoryArchiveConflictError(
                "PlanApproval belongs to another PlanVersion"
            )
        lock = PlanLockService(self.session).get_verified(approval.plan_lock_id)
        if lock.base_commit_sha != base_commit_sha:
            raise RepositoryArchiveConflictError(
                "Archive base commit does not match the approved lock"
            )
        task = ContributionTaskService(self.session).get_verified(plan.task_id)
        snapshot = self.session.get(OpportunitySnapshot, task.snapshot_id)
        repository_full_name = _repository_from_snapshot(snapshot)
        return self.enqueue(
            plan_version_id=plan.id,
            plan_approval_id=approval.id,
            repository_full_name=repository_full_name,
            base_commit_sha=base_commit_sha,
            idempotency_key=idempotency_key,
            now=now,
        )


class RepositoryArchiveJobWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        github_client_factory: Callable[[], AbstractAsyncContextManager[Any]],
        *,
        worker_id: str,
        heartbeat_interval_seconds: float = 15,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        self.database = database
        self.settings = settings
        self.github_client_factory = github_client_factory
        self.worker_id = worker_id
        self.heartbeat_interval_seconds = max(0.1, heartbeat_interval_seconds)

    async def run_once(self, *, now: datetime | None = None) -> Job | None:
        started_at = _aware(now)
        with self.database.session() as session:
            service = JobService(session)
            leased = service.lease_next(
                worker_id=self.worker_id,
                kinds=(REPOSITORY_ARCHIVE_JOB_KIND,),
                lease_seconds=60,
                now=started_at,
            )
            if leased is None:
                return None
            running = service.start(
                leased.id,
                worker_id=self.worker_id,
                now=started_at,
            )
            running = service.heartbeat(
                running.id,
                worker_id=self.worker_id,
                lease_seconds=running.timeout_seconds + 30,
                now=started_at,
            )
            job_id = running.id
            timeout_seconds = running.timeout_seconds
            payload = dict(running.payload)

        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                job_id,
                heartbeat_stop,
                lease_seconds=timeout_seconds + 30,
            )
        )
        try:
            repository, commit_sha = _archive_parameters(payload)
            store = RepositoryArchiveStore(self.settings.artifact_root)
            async with asyncio.timeout(timeout_seconds):
                with tempfile.TemporaryDirectory(
                    prefix="contribos-archive-"
                ) as temporary:
                    destination = Path(temporary) / "repository.tar.gz"
                    async with self.github_client_factory() as github:
                        await github.download_repository_archive(
                            repository,
                            commit_sha,
                            destination,
                        )
                    archive_hash = store.put_file(destination)
                    archive = store.lookup(
                        archive_hash,
                        repository_full_name=repository,
                        base_commit_sha=commit_sha,
                    )
                    if archive is None:
                        raise RepositoryArchiveError(
                            "Captured repository archive disappeared"
                        )
                    result = {
                        "archive_hash": archive.archive_hash,
                        "size_bytes": archive.size_bytes,
                        "repository_full_name": archive.repository_full_name,
                        "base_commit_sha": archive.base_commit_sha,
                    }
            with self.database.session() as session:
                service = JobService(session)
                current = service.get(job_id)
                if current.cancel_requested_at is not None:
                    return service.cancel(
                        job_id,
                        worker_id=self.worker_id,
                        now=_aware(None),
                    )
                return service.succeed(
                    job_id,
                    worker_id=self.worker_id,
                    result_data=result,
                    now=_aware(None),
                )
        except TimeoutError:
            with self.database.session() as session:
                return JobService(session).time_out(
                    job_id,
                    worker_id=self.worker_id,
                    now=_aware(None),
                )
        except Exception as exc:
            with self.database.session() as session:
                service = JobService(session)
                current = service.get(job_id)
                if current.cancel_requested_at is not None:
                    return service.cancel(
                        job_id,
                        worker_id=self.worker_id,
                        now=_aware(None),
                    )
                return service.fail(
                    job_id,
                    worker_id=self.worker_id,
                    error_code=_error_code(exc),
                    error_message="Repository archive capture failed",
                    now=_aware(None),
                )
        finally:
            heartbeat_stop.set()
            with suppress(JobTransitionError):
                await heartbeat_task

    async def _heartbeat_loop(
        self,
        job_id: str,
        stop: asyncio.Event,
        *,
        lease_seconds: int,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.heartbeat_interval_seconds,
                )
                return
            except TimeoutError:
                with self.database.session() as session:
                    JobService(session).heartbeat(
                        job_id,
                        worker_id=self.worker_id,
                        lease_seconds=lease_seconds,
                    )


def _archive_parameters(payload: dict[str, Any]) -> tuple[str, str]:
    repository = payload.get("repository_full_name")
    commit_sha = payload.get("base_commit_sha")
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise ValueError("Repository archive job repository is invalid")
    if not isinstance(commit_sha, str) or not _BASE_SHA.fullmatch(commit_sha):
        raise ValueError("Repository archive job commit SHA is invalid")
    return repository, commit_sha


def _repository_from_snapshot(snapshot: OpportunitySnapshot | None) -> str:
    repository = snapshot.repository_data if snapshot is not None else None
    value = repository.get("full_name") if isinstance(repository, dict) else None
    if (
        not isinstance(value, str)
        or not _REPOSITORY.fullmatch(value)
        or contains_sensitive_text(value)
    ):
        raise RepositoryArchiveConflictError(
            "Archive repository snapshot is invalid"
        )
    return value


def _file_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _error_code(error: Exception) -> str:
    name = type(error).__name__.lower()
    return f"archive_{name}"[:80]


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)
