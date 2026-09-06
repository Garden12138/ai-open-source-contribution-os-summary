from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.execution_control import ExecutionStageControlService
from app.executions import (
    ExecutionAttemptConflictError,
    ExecutionAttemptService,
    ExecutionStageStatus,
)
from app.plans import PlanVersionService
from app.sandbox_worker.specs import (
    JobSpecSigner,
    SandboxCommand,
    SandboxPolicy,
)
from app.security import ensure_no_sensitive_data


_HASH = re.compile(r"^[0-9a-f]{64}$")


class ChangeSetError(RuntimeError):
    pass


class ChangeSetConflictError(ChangeSetError):
    pass


class ChangeSetStore:
    def __init__(self, root: Path | str) -> None:
        self.root = (Path(root) / "change-sets").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, change_set_hash: str) -> Path:
        if not _HASH.fullmatch(change_set_hash):
            raise ValueError("ChangeSet hash is invalid")
        return self.root / change_set_hash[:2] / change_set_hash

    def put(self, change_set: Any) -> str:
        data = change_set.to_bytes()
        digest = change_set.change_set_hash
        if hashlib.sha256(data).hexdigest() != digest:
            # to_bytes adds a trailing newline; hash is of to_wire()
            digest = change_set.change_set_hash
        target = self.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = self.lookup(digest)
            if existing.change_set_hash != digest:
                raise ChangeSetError("ChangeSet hash collision")
            return digest
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".changeset-",
            suffix=".tmp",
            dir=self.root,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def lookup(self, change_set_hash: str) -> Any:
        from app.sandbox_worker.implementation import ImplementationChangeSet

        path = self.path_for(change_set_hash)
        if not path.is_file():
            raise ChangeSetError("ChangeSet was not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        change_set = ImplementationChangeSet.from_wire(payload)
        if change_set.change_set_hash != change_set_hash:
            raise ChangeSetError("Stored ChangeSet hash mismatch")
        return change_set


class ChangeSetService:
    def __init__(
        self,
        session: Session,
        store: ChangeSetStore,
        signer: JobSpecSigner,
    ) -> None:
        self.session = session
        self.store = store
        self.signer = signer

    def accept(
        self,
        execution_attempt_id: str,
        *,
        source: str,
        operations: list[dict[str, Any]] | None,
        idempotency_key: str,
    ) -> Any:
        attempts = ExecutionAttemptService(self.session)
        attempt = attempts.get_verified(execution_attempt_id)
        current = attempts.current(attempt.id)
        plan = PlanVersionService(self.session).get_verified(
            attempt.plan_version_id
        )
        from app.sandbox_worker.implementation import ImplementationChangeSet

        if source == "fake":
            change_set = _fake_change_set(attempt, plan)
        elif source == "user":
            change_set = ImplementationChangeSet(
                change_set_id=f"changeset-{uuid4().hex[:12]}",
                plan_version_id=plan.id,
                plan_content_hash=plan.content_hash,
                plan_record_hash=plan.record_hash,
                operations=_operations(operations or []),
            )
        else:
            raise ValueError("ChangeSet source is invalid")
        return self.accept_existing(
            execution_attempt_id,
            change_set=change_set,
            idempotency_key=idempotency_key,
        )

    def accept_existing(
        self,
        execution_attempt_id: str,
        *,
        change_set: Any,
        idempotency_key: str,
    ) -> Any:
        """Accept one already-materialized immutable ChangeSet.

        Provider proposals use this path so the hash shown to the user is the
        exact hash consumed by the Implement stage.
        """
        attempts = ExecutionAttemptService(self.session)
        attempt = attempts.get_verified(execution_attempt_id)
        current = attempts.current(attempt.id)
        plan = PlanVersionService(self.session).get_verified(
            attempt.plan_version_id
        )
        if (
            change_set.plan_version_id != plan.id
            or change_set.plan_content_hash != plan.content_hash
            or change_set.plan_record_hash != plan.record_hash
        ):
            raise ChangeSetConflictError(
                "ChangeSet does not match the approved PlanVersion"
            )
        allowed = tuple(plan.files_likely_to_change)
        if not set(change_set.paths).issubset(set(allowed)):
            raise ChangeSetConflictError(
                "ChangeSet contains a path outside the approved plan"
            )
        ensure_no_sensitive_data(
            change_set.to_wire(),
            context="accepted ChangeSet",
        )
        change_set_hash = self.store.put(change_set)
        if (
            current.stage == "implement"
            and len(tuple(current.input_hashes)) == 2
            and current.input_hashes[1] == change_set_hash
        ):
            return change_set
        if (
            current.stage != "explore"
            or current.status != ExecutionStageStatus.SUCCEEDED.value
            or current.result_hash is None
        ):
            raise ChangeSetConflictError(
                "ChangeSet requires a succeeded Explore stage"
            )
        pending = attempts.advance_stage(
            attempt.id,
            expected_sequence=current.sequence,
            expected_record_hash=current.record_hash,
            input_hashes=(current.result_hash, change_set_hash),
            reason_code="changeset_accepted",
            idempotency_key=idempotency_key,
        )
        spec = attempts.build_current_job_spec(
            attempt.id,
            allowed_change_paths=allowed,
        )
        ExecutionStageControlService(self.session).schedule_current(
            attempt.id,
            signed_job_spec=self.signer.sign(spec),
            signer=self.signer,
            sandbox_policy=SandboxPolicy(),
            idempotency_key=f"schedule:{attempt.id}:{pending.stage}:{pending.sequence}",
        )
        return change_set


def plan_sandbox_commands(plan: Any) -> tuple[SandboxCommand, ...]:
    commands = []
    for item in getattr(plan, "commands_to_run", ()) or ():
        if hasattr(item, "command_id"):
            commands.append(
                SandboxCommand(
                    command_id=item.command_id,
                    argv=tuple(item.argv),
                    working_directory=item.working_directory,
                )
            )
            continue
        if isinstance(item, dict):
            commands.append(
                SandboxCommand(
                    command_id=str(item["command_id"]),
                    argv=tuple(item["argv"]),
                    working_directory=str(item.get("working_directory") or "."),
                )
            )
    return tuple(commands)


def _fake_change_set(attempt: Any, plan: Any) -> Any:
    from app.sandbox_worker.implementation import (
        ChangeOperation,
        ChangeOperationKind,
        ImplementationChangeSet,
    )

    path = next(iter(plan.files_likely_to_change), None)
    if not isinstance(path, str) or not path:
        raise ChangeSetConflictError("Approved plan has no changeable files")
    return ImplementationChangeSet(
        change_set_id=f"changeset-fake-{attempt.id[:8]}",
        plan_version_id=plan.id,
        plan_content_hash=plan.content_hash,
        plan_record_hash=plan.record_hash,
        operations=(
            ChangeOperation(
                path=path,
                kind=ChangeOperationKind.WRITE,
                expected_prior_hash=None,
                content=(
                    f"# contribos fake change for {plan.id}\n"
                    f"# {plan.goal}\n"
                ),
                executable=False,
            ),
        ),
    )


def _operations(raw: list[dict[str, Any]]) -> tuple[Any, ...]:
    from app.sandbox_worker.implementation import (
        ChangeOperation,
        ChangeOperationKind,
    )

    operations = []
    for item in raw:
        operations.append(
            ChangeOperation(
                path=str(item.get("path") or ""),
                kind=ChangeOperationKind(item.get("kind")),
                expected_prior_hash=item.get("expected_prior_hash"),
                content=item.get("content"),
                executable=item.get("executable"),
            )
        )
    return tuple(operations)
