"""Platform-verified implementation progress in the existing append-only Journal."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field

from app.agent_v2.contracts import Hash, StrictModel, ToolCall, ToolResult
from app.provenance import content_hash
from app.workbench import WorkbenchError

VERSION = "implementation-progress-v1"
POLICY = {"version": VERSION, "no_progress_turns_before_correction": 2,
    "no_progress_turns_after_correction": 1, "exact_read_reuse": True,
    "oversized_batches": "validated_read_file_8_in_sub_batches_4", "evidence_novelty": "returned_byte_range_and_outcome"}


class ProgressStep(StrictModel):
    step_id: Literal["baseline", "plan", "investigate", "implement", "verify", "review"]
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"
    source_hash: Hash
    evidence_refs: list[Hash] = Field(default_factory=list, max_length=256)
    attempts: int = Field(default=0, ge=0, le=256)
    blocker: str | None = None
    next_action: str


class ImplementationProgress(StrictModel):
    version: Literal["implementation-progress-v1"] = VERSION
    revision: int = Field(ge=1)
    phase: Literal["investigating", "implementing", "verifying", "repairing", "reviewing", "ready_for_human", "blocked"]
    source_hash: Hash
    steps: list[ProgressStep] = Field(min_length=6, max_length=6)
    seen_evidence: list[Hash] = Field(default_factory=list, max_length=256)
    no_progress_streak: int = Field(default=0, ge=0, le=256)
    batch_progress: bool = False
    correction_issued: bool = False
    blocker: str | None = None
    next_action: str


def enabled(journal: Any) -> bool:
    return journal.root().payload.get("harness_policy") == POLICY


def current_progress(journal: Any) -> ImplementationProgress | None:
    rows = [e for e in journal.events() if e.kind == "agent_v2_progress"]
    if not rows:
        return None
    previous = None
    for revision, event in enumerate(rows, 1):
        value = ImplementationProgress.model_validate(event.payload["progress"])
        if (event.actor_id != "orchestrator" or value.revision != revision
                or event.payload["expected_progress_hash"] != previous
                or journal.wb.read(event.payload["artifact_id"]) != value.model_dump()):
            raise WorkbenchError("实施进度证据无效。", code="agent_progress_invalid")
        previous = event.record_hash
    return value


def _append(journal: Any, value: ImplementationProgress, *, checkpoint: Any, key: str, job_id: str | None = None) -> None:
    journal.lock()
    if journal.checkpoint().revision != checkpoint.revision:
        raise WorkbenchError("实施进度已过期。", code="agent_checkpoint_stale")
    prior = next((e for e in reversed(journal.events()) if e.kind == "agent_v2_progress"), None)
    revision = 1 if prior is None else prior.payload["progress"]["revision"] + 1
    if value.revision != revision:
        raise WorkbenchError("实施进度修订已过期。", code="agent_progress_stale")
    journal.append("progress", {"progress": value.model_dump(), "artifact_id": journal.wb.artifact(value.model_dump()),
        "expected_progress_hash": prior.record_hash if prior else None,
        "checkpoint_hash": checkpoint.revision}, key=key, job_id=job_id)


def initialize(journal: Any, checkpoint: Any) -> None:
    if not enabled(journal) or current_progress(journal) is not None:
        return
    spec = journal.spec()
    steps = [ProgressStep(step_id=name, source_hash=checkpoint.source_hash, next_action=action)
        for name, action in (("baseline", "inspect_baseline"), ("plan", "inspect_approved_plan"),
            ("investigate", "collect_source_evidence"), ("implement", "apply_approved_patch"),
            ("verify", "run_approved_checks"), ("review", "independent_review"))]
    steps[0] = steps[0].model_copy(update={"status": "completed", "evidence_refs": [spec.binding.baseline_hash]})
    steps[1] = steps[1].model_copy(update={"status": "completed",
        "evidence_refs": [spec.binding.plan_hash, spec.binding.authorization_hash]})
    _append(journal, ImplementationProgress(revision=1, phase="investigating", source_hash=checkpoint.source_hash,
        steps=steps, next_action="collect_source_evidence"), checkpoint=checkpoint, key="progress-initial:" + journal.session_id)


def record_tool(journal: Any, checkpoint: Any, call: ToolCall, artifact: str, *, batch_complete: bool,
                job_id: str) -> ImplementationProgress | None:
    """Only a committed platform ToolResult can complete a step, never model prose."""
    old = current_progress(journal)
    if not enabled(journal) or old is None:
        return None
    result = ToolResult.model_validate(journal.wb.read(artifact))
    receipt_event = next((e for e in reversed(journal.events()) if e.kind == "agent_v2_tool_result"
        and e.payload.get("artifact_id") == artifact and e.job_id == job_id), None)
    if (receipt_event is None or receipt_event.actor_id != "orchestrator"
            or result.call_id != call.call_id or result.source_hash != checkpoint.source_hash):
        raise WorkbenchError("进度对应的工具证据无效。", code="agent_progress_evidence_invalid")
    data = result.data
    read = call.name in {"read_file", "list_paths", "search_code", "read_artifact", "inspect_failure", "read_diff"}
    evidence_args = dict(receipt_event.payload.get("request_arguments", call.arguments))
    if read:
        evidence_args.pop("max_bytes", None)
        evidence_args.pop("expected_hash", None)
    text = data.get("text")
    verification = data.get("verification")
    fingerprint = content_hash({"tool": call.name, "arguments": evidence_args,
        "source_hash": result.output_source_hash,
        "content_hash": data.get("content_hash"), "status": result.status,
        "page": {"offset": data.get("offset"), "text_hash": content_hash(text) if isinstance(text, str) else None},
        "outcome": data.get("outcome"),
        "tests_hash": verification.get("test_results_hash") if isinstance(verification, dict) else None})
    meaningful = result.status == "succeeded" and not data.get("reused_receipt") and fingerprint not in old.seen_evidence
    meaningful = meaningful and (read or call.name in {"apply_patch", "run_approved_checks"})
    steps = [s.model_dump() for s in old.steps]
    phase, action = old.phase, old.next_action
    index = 2 if read else 3 if call.name == "apply_patch" else 4 if call.name == "run_approved_checks" else None
    if index is not None:
        step = steps[index]
        step["attempts"] += 1
        step["source_hash"] = result.output_source_hash
        if result.status == "succeeded":
            step["evidence_refs"] = list(dict.fromkeys([*step["evidence_refs"], artifact]))
            # Investigation remains in progress until an approved patch proves action.
            step["status"] = "in_progress" if read else "completed"
            if call.name == "apply_patch":
                meaningful = result.output_source_hash != result.source_hash
                step["status"] = "completed" if meaningful else "pending"
                phase, action = ("repairing" if old.phase == "repairing" else "implementing"), "run_approved_checks"
                steps[4].update(status="pending", source_hash=result.output_source_hash, evidence_refs=[])
                steps[5].update(status="pending", source_hash=result.output_source_hash, evidence_refs=[])
                steps[2]["status"] = "completed" if steps[2]["evidence_refs"] else "pending"
            elif call.name == "run_approved_checks":
                has_patch = steps[3]["status"] == "completed"
                step["status"] = "completed" if data.get("outcome") == "passed" and has_patch else "blocked" if has_patch else "pending"
                if has_patch:
                    phase, action = ("reviewing", "request_finish_for_independent_review") if data.get("outcome") == "passed" else ("repairing", "repair_from_failed_check_evidence")
                else:
                    action = "apply_approved_patch"
                step["blocker"] = None if data.get("outcome") == "passed" else "approved_checks_failed"
            else:
                action = "use_existing_evidence_or_implement"
    seen = list(dict.fromkeys([*old.seen_evidence, fingerprint])) if meaningful else old.seen_evidence
    batch_progress = old.batch_progress or meaningful
    streak, correction = old.no_progress_streak, old.correction_issued
    blocker = old.blocker
    if batch_complete:
        streak = 0 if batch_progress else streak + 1
        if batch_progress:
            correction = False
        elif correction and streak >= POLICY["no_progress_turns_before_correction"] + POLICY["no_progress_turns_after_correction"]:
            phase, blocker, action = "blocked", "agent_no_progress", "inspect_progress_and_replan"
        elif streak >= POLICY["no_progress_turns_before_correction"]:
            correction, action = True, "use_existing_evidence_or_implement"
        batch_progress = False
    if result.status == "unknown" or (result.failure and result.failure.category in {
            "security", "environment", "baseline", "budget", "platform"}):
        phase = "blocked"
        blocker = result.failure.safe_reason_code if result.failure else "agent_step_outcome_unknown"
        action = result.failure.next_allowed_action if result.failure else "inspect_checkpoint"
        if index is not None:
            steps[index].update(status="blocked", blocker=blocker)
    value = ImplementationProgress(revision=old.revision + 1, phase=phase, source_hash=result.output_source_hash,
        steps=steps, seen_evidence=seen, no_progress_streak=streak, batch_progress=batch_progress,
        correction_issued=correction, blocker=blocker, next_action=action)
    _append(journal, value, checkpoint=checkpoint, key="progress-tool:" + job_id, job_id=job_id)
    return value


def prompt_progress(journal: Any) -> dict | None:
    """Bounded summary; the complete ledger remains an issued immutable artifact."""
    value = current_progress(journal)
    if value is None:
        return None
    event = next(e for e in reversed(journal.events()) if e.kind == "agent_v2_progress")
    return {"version": VERSION, "revision": value.revision, "phase": value.phase,
        "source_hash": value.source_hash, "artifact_id": event.payload["artifact_id"],
        "no_progress_streak": value.no_progress_streak, "correction_issued": value.correction_issued,
        "blocker": value.blocker, "next_action": value.next_action,
        "steps": [{"step_id": s.step_id, "status": s.status, "attempts": s.attempts,
            "latest_evidence": s.evidence_refs[-1] if s.evidence_refs else None} for s in value.steps],
        "instruction": "Reuse existing receipts. Follow visible next_offset for new bytes. When corrected, use existing evidence to apply an approved exact patch or report a concrete blocker. No scope or budget is added."}


def record_checkpoint_blocked(journal: Any, checkpoint: Any) -> None:
    """Mirror a terminal execution fact without granting a retry or new budget."""
    if checkpoint.state != "blocked" or not enabled(journal):
        return
    old = current_progress(journal)
    if old is None:
        return  # Never fabricate a ledger for a legacy session.
    event = next((e for e in reversed(journal.events()) if e.actor_id == "orchestrator"
        and isinstance(e.payload.get("failure"), dict)), None)
    from app.agent_v2.contracts import Failure
    failure = Failure.model_validate(event.payload["failure"]) if event else None
    blocker = failure.safe_reason_code if failure else "agent_session_blocked"
    action = failure.next_allowed_action if failure else "inspect_checkpoint"
    steps = [step.model_dump() for step in old.steps]
    index = {"investigating": 2, "implementing": 3, "verifying": 4,
        "repairing": 3, "reviewing": 5}.get(old.phase)
    if index is None:
        index = next((i for i in range(2, 6) if steps[i]["status"] != "completed"), 5)
    refs = list(steps[index]["evidence_refs"])
    if event:
        refs.append(event.record_hash)
        if event.payload.get("response_artifact"):
            refs.append(event.payload["response_artifact"])
    steps[index].update(status="blocked", blocker=blocker, next_action=action,
        source_hash=checkpoint.source_hash, evidence_refs=list(dict.fromkeys(refs))[-256:])
    if (old.phase == "blocked" and old.blocker == blocker and old.next_action == action
            and old.source_hash == checkpoint.source_hash and steps == [s.model_dump() for s in old.steps]):
        return
    value = ImplementationProgress.model_validate({**old.model_dump(), "revision": old.revision + 1,
        "phase": "blocked", "steps": steps, "source_hash": checkpoint.source_hash,
        "blocker": blocker, "next_action": action})
    _append(journal, value, checkpoint=checkpoint, key="progress-blocked:" + checkpoint.revision,
        job_id=event.job_id if event else None)


def record_review_ready(journal: Any, review: Any) -> None:
    """Only the independent, hash-verified ReviewRun can complete the review step."""
    if not enabled(journal):
        return
    from app.reviews import ReviewRunService
    from app.agent_v2.loop import validate_finish
    verified = ReviewRunService(journal.wb.session, journal.wb.artifacts).get_verified(review.id)
    cp, spec = journal.checkpoint(), journal.spec()
    if verified.execution_attempt_id != spec.binding.attempt_id or verified.verdict != "pass" or cp.state != "finished":
        raise WorkbenchError("独立审查与当前实施不匹配。", code="agent_review_binding_invalid")
    validate_finish(journal)
    old = current_progress(journal)
    if old is None:
        raise WorkbenchError("实施进度不存在。", code="agent_progress_invalid")
    if old.phase == "ready_for_human" and verified.record_hash in old.steps[5].evidence_refs:
        return
    steps = [step.model_dump() for step in old.steps]
    steps[5].update(status="completed", attempts=steps[5]["attempts"] + 1,
        source_hash=cp.source_hash, evidence_refs=[verified.record_hash], next_action="human_acceptance")
    value = ImplementationProgress.model_validate({**old.model_dump(), "revision": old.revision + 1,
        "phase": "ready_for_human", "steps": steps, "blocker": None, "next_action": "human_acceptance"})
    _append(journal, value, checkpoint=cp, key="progress-review:" + verified.id)
