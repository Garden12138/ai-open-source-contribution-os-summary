"""Verified repair inputs and cumulative changes against the approved base."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artifacts import ArtifactStore
from app.changesets import ChangeSetStore
from app.executions import ExecutionAttemptService
from app.models import ExecutionAttempt, ContributionTaskStateVersion
from app.reviews import ReviewRunService
from app.sandbox_worker.coding_context import CodingContext, CodingContextEntry
from app.sandbox_worker.implementation import (
    ChangeOperation, ChangeOperationKind, ImplementationChangeSet,
)


def repair_change_set(
    session: Session, artifact_root: str, context: CodingContext,
) -> ImplementationChangeSet | None:
    """Load only the change set actually implemented and reviewed by the predecessor."""
    attempts = ExecutionAttemptService(session)
    attempt = attempts.get_verified(context.execution_attempt_id)
    state = session.get(ContributionTaskStateVersion, attempt.executing_state_version_id)
    if state.reason_code != "repair_started":
        return None
    previous = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.task_id == attempt.task_id,
        ExecutionAttempt.plan_approval_id == attempt.plan_approval_id,
        ExecutionAttempt.attempt_number == attempt.attempt_number - 1,
    ))
    if previous is None:
        raise ValueError("Repair predecessor is missing")
    previous = attempts.get_verified(previous.id)
    for name in ("plan_version_id", "plan_content_hash", "plan_record_hash",
                 "base_commit_sha", "repository_archive_hash", "sandbox_policy_hash",
                 "runner_image_digest"):
        if getattr(previous, name) != getattr(attempt, name):
            raise ValueError("Repair predecessor inputs changed")
    store = ArtifactStore(session, artifact_root)
    reviews = ReviewRunService(session, store).list_for_attempt(previous.id)
    if not reviews or reviews[-1].verdict != "block":
        raise ValueError("Repair requires a verified blocking review")
    review = reviews[-1]
    history = attempts.history(previous.id)
    implement = next((s for s in reversed(history) if s.stage == "implement"), None)
    verify = next((s for s in reversed(history) if s.stage == "verify"), None)
    if (implement is None or verify is None or implement.status != "succeeded"
            or verify.status != "succeeded" or implement.id != review.implement_stage_version_id
            or verify.id != review.verify_stage_version_id or verify.result_hash != review.verify_result_hash):
        raise ValueError("Repair review stage binding changed")
    result = json.loads(store.read_bytes(implement.result_hash))
    change_set = ChangeSetStore(artifact_root).lookup(result["change_set_hash"])
    if (change_set.plan_version_id, change_set.plan_content_hash, change_set.plan_record_hash) != (
        context.plan_version_id, context.plan_content_hash, context.plan_record_hash,
    ):
        raise ValueError("Repair change set plan binding changed")
    return change_set


def repair_context(
    base: CodingContext, previous: ImplementationChangeSet | None, *, allowed_paths: Sequence[str],
) -> CodingContext:
    """Create an ephemeral editing view; the stored base context remains immutable."""
    if previous is None:
        return base
    if (previous.plan_version_id, previous.plan_content_hash, previous.plan_record_hash) != (
        base.plan_version_id, base.plan_content_hash, base.plan_record_hash,
    ):
        raise ValueError("Repair change set plan binding changed")
    entries = {entry.path: entry for entry in base.entries}
    for operation in previous.operations:
        entry = entries.get(operation.path)
        if (operation.path not in allowed_paths or entry is None
                or operation.expected_prior_hash != entry.prior_hash):
            raise ValueError("Repair operation does not match the approved base")
        if operation.kind is ChangeOperationKind.DELETE:
            if entry.content is None:
                raise ValueError("Repair deletes a missing base file")
            entries[entry.path] = CodingContextEntry(entry.path, None, None, None)
        else:
            content = operation.content
            entries[entry.path] = CodingContextEntry(
                entry.path, hashlib.sha256(content.encode("utf-8")).hexdigest(),
                content, operation.executable,
            )
    # dataclasses.replace cannot update a context without also recomputing its hash.
    payload = base.hash_payload()
    payload.pop("version")
    payload["entries"] = tuple(entries.values())
    return CodingContext.create(**payload)


def cumulative_operations(
    base: CodingContext, view: CodingContext, previous: ImplementationChangeSet | None,
    operations: tuple[ChangeOperation, ...], *, allowed_paths: Sequence[str],
) -> tuple[ChangeOperation, ...]:
    """Validate edits against the view, retain previous changes, rebase onto the original."""
    if previous is None:
        return operations
    base_entries = {entry.path: entry for entry in base.entries}
    view_entries = {entry.path: entry for entry in view.entries}
    cumulative = {operation.path: operation for operation in previous.operations}
    for operation in operations:
        entry = view_entries.get(operation.path)
        if (operation.path not in allowed_paths or entry is None
                or operation.expected_prior_hash != entry.prior_hash):
            raise ValueError("Repair edit does not match the readable repair context")
        original = base_entries[operation.path]
        if operation.kind is ChangeOperationKind.DELETE:
            if entry.content is None:
                raise ValueError("Repair deletes a missing file")
            if original.content is None:
                cumulative.pop(operation.path, None)
                continue
        cumulative[operation.path] = replace(operation, expected_prior_hash=original.prior_hash)
    return tuple(cumulative[path] for path in sorted(cumulative))
