from __future__ import annotations

import hashlib
import json

import pytest

from app.coding import _parse_change_set_proposal
from app.execution_outcome import classify_verification
from app.planner import PlanningService
from app.sandbox_worker.coding_context import CodingContext, CodingContextEntry
from tests.test_workbench import seeded, run_turn, result


def context_for(text: str, path: str = "CHANGELOG.md") -> CodingContext:
    return CodingContext.create(
        execution_attempt_id="attempt", plan_version_id="plan",
        plan_content_hash="a" * 64, plan_record_hash="b" * 64,
        base_commit_sha="c" * 40, repository_archive_hash="d" * 64,
        explore_result_hash="e" * 64,
        entries=(CodingContextEntry(path, hashlib.sha256(text.encode()).hexdigest(), text, False),),
    )


def edit_proposal(context: CodingContext, old: str, new: str) -> dict:
    return {"summary": "Add one entry", "operations": [{
        "kind": "edit", "path": context.entries[0].path,
        "expected_prior_hash": context.entries[0].prior_hash,
        "content": None, "executable": None,
        "edits": [{"old_text": old, "new_text": new}],
    }]}


def test_exact_edit_preserves_changelog_history_and_materializes_signed_write():
    history = "## 0.1.65\r\n\r\n* Historical URL /compare/v1...v2 日期 2024-05-21\r\n"
    context = context_for("# Changelog\r\n\r\n" + history)
    proposal = edit_proposal(context, "# Changelog\r\n", "# Changelog\r\n\r\n## Unreleased\r\n* Fix streaming\r\n")
    _, operations = _parse_change_set_proposal(json.dumps(proposal), context)
    assert operations[0].kind.value == "write"
    assert operations[0].expected_prior_hash == context.entries[0].prior_hash
    assert operations[0].content.endswith(history)
    assert operations[0].content == context.entries[0].content.replace(
        "# Changelog\r\n", "# Changelog\r\n\r\n## Unreleased\r\n* Fix streaming\r\n", 1)


@pytest.mark.parametrize("change", ["missing", "ambiguous", "wrong_hash", "unreadable", "content", "escape", "secret"])
def test_exact_edit_rejects_unproven_or_sensitive_replacements(change):
    context = context_for("# Changelog\nrepeat\nrepeat\n")
    proposal = edit_proposal(context, "# Changelog", "# Changelog\nNew entry")
    operation = proposal["operations"][0]
    if change == "missing":
        operation["edits"][0]["old_text"] = "missing"
    elif change == "ambiguous":
        operation["edits"][0]["old_text"] = "repeat"
    elif change == "wrong_hash":
        operation["expected_prior_hash"] = "f" * 64
    elif change == "unreadable":
        operation["path"] = "not-in-context.md"
    elif change == "content":
        operation["content"] = "replacement file"
    elif change == "escape":
        operation["path"] = "../CHANGELOG.md"
    else:
        operation["edits"][0]["new_text"] = "ghp_EXECUTIONREPAIRCANARY123456789"
    with pytest.raises(ValueError):
        _parse_change_set_proposal(json.dumps(proposal), context)


def test_stdout_diagnostics_are_visible_redacted_and_classified():
    value = {"succeeded": False, "test_results": [{"outcome": "failed", "exit_code": 1}],
             "command_evidence": [{"command_id": "pytest", "status": "failed", "exit_code": 1,
                                   "stderr_log": "", "stdout_log": "StopIteration\napi_key=repaircanary123456"}]}
    outcome = classify_verification(value)
    assert outcome["status"] == "failed"
    failure = outcome["failures"][0]
    assert "StopIteration" in failure["output"]
    assert failure["stderr"] == ""
    assert "repaircanary123456" not in json.dumps(outcome)
    value["command_evidence"][0]["stdout_log"] = "ModuleNotFoundError: No module named 'missing_package'"
    assert classify_verification(value)["missing_dependencies"] == ["missing_package"]


def test_bad_clarification_options_schedule_bounded_repair_then_save_plan(tmp_path):
    db, settings, task_id, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id, text="修订当前方案", parent_id=None, expected_hash=None, key="repair-options")
    malformed = json.dumps({"reply": "确认方向", "plan": None, "read_paths": [],
                            "questions": [{"id": "scope", "prompt": "范围", "options": [{"label": "保持范围"}]}]})
    first, _ = run_turn(db, settings, malformed)
    assert first.state == "succeeded"
    assert first.result_data["repair_scheduled"] is True
    second, runner = run_turn(db, settings, result(content))
    assert second.state == "succeeded", second.error_message
    assert second.result_data["plan_status"] == "saved"
    assert json.loads(runner.invocations[0].prompt)["plan_repair"]["constraint"] == "clarification_options_plain_strings"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest_plan(task_id) is not None
        assert wb.latest(task_id, "execution_authorized") is None
        assert not any(j.kind == "contribution_start" for j in wb.jobs(task_id))


def test_bad_clarification_options_stop_after_two_repairs(tmp_path):
    db, settings, task_id, _, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id, text="修订", parent_id=None, expected_hash=None, key="bad-options")
    malformed = json.dumps({"reply": "确认", "questions": [{"id": "scope", "prompt": "范围", "options": [{}]}]})
    for number in range(3):
        job, _ = run_turn(db, settings, malformed)
        if number < 2:
            assert job.result_data["repair_scheduled"] is True
        else:
            assert job.state == "failed"
    with db.session() as session:
        assert PlanningService(session, settings.artifact_root).latest_plan(task_id) is None


def test_exact_edit_flows_through_existing_worker_and_verified_change_set(tmp_path):
    from app.coding import CodingContextService, CodingConversationService
    from app.artifacts import ArtifactStore
    from tests.test_workbench import automatic_fixture, populate_round, drive_round
    from app.models import ExecutionAttempt
    from sqlalchemy import select

    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    source = "original content for app/plans.py\n"
    context = context_for(source, "app/plans.py")
    code.contents[1] = json.dumps(edit_proposal(context, "app/plans.py", "app/plans.py (updated)"))
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.task_id == task_id))
        coding = CodingContextService(session).get_session(attempt.id)
        proposal = CodingConversationService(session, artifact_root=settings.artifact_root).proposals(coding.id)[-1]
        materialized = json.loads(ArtifactStore(session, settings.artifact_root).read_bytes(proposal.change_set_artifact_id))
        assert materialized["operations"][0]["kind"] == "write"
        assert materialized["operations"][0]["content"] == "original content for app/plans.py (updated)\n"
        assert materialized["operations"][0]["expected_prior_hash"] == hashlib.sha256(source.encode()).hexdigest()


def test_user_refresh_rereads_plan_paths_on_new_snapshot_without_merging_old_content(tmp_path):
    import asyncio
    from app.planner import PlanningArchiveWorker, PlanningContextWorker
    from tests.test_execution_pipeline import ARCHIVE_BYTES
    from tests.test_workbench import ReadOnlyGitHub

    db, settings, task_id, content, _ = seeded(tmp_path)
    with db.session() as session:
        PlanningService(session, settings.artifact_root).request(
            task_id, text="规划", parent_id=None, expected_hash=None, key="first")
    assert run_turn(db, settings, result(content))[0].state == "succeeded"
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        plan = wb.latest_plan(task_id)
        job = wb.request(task_id, text="重新读取", parent_id=plan.id,
                         expected_hash=plan.record_hash, key="refresh", refresh=True)
        expected = set(plan.files_to_inspect) | set(plan.files_likely_to_change)
        assert expected <= set(job.payload["paths"])

    class GitHub(ReadOnlyGitHub):
        async def resolve_repository_head(self, repository):
            return "main", "c" * 40

        async def download_repository_archive(self, repository, sha, *, destination, max_bytes):
            destination.write_bytes(ARCHIVE_BYTES)

    class Runtime:
        async def inspect(self, **kwargs):
            assert expected <= set(kwargs["paths"])
            return {"inventory": sorted(expected), "entries": [
                {"path": path, "content": "fresh source", "prior_hash": hashlib.sha256(b"fresh source").hexdigest(), "executable": False}
                for path in kwargs["paths"]], "truncated": False}

    archive = asyncio.run(PlanningArchiveWorker(db, settings, GitHub, worker_id="refresh").run_once())
    assert archive.state == "succeeded", archive.error_message
    inspected = asyncio.run(PlanningContextWorker(db, settings, Runtime(), worker_id="read").run_once())
    assert inspected.state == "succeeded", inspected.error_message
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        context = wb.latest(task_id, "context_ready")
        assert context.payload["base_sha"] == "c" * 40
        assert all(e["content"] == "fresh source" for e in wb.read(context.payload["artifact_id"])["entries"])
        with pytest.raises(Exception, match="过期"):
            wb.binding(task_id, plan.id)


def repair_fixture():
    from dataclasses import replace
    from app.sandbox_worker.implementation import ChangeOperation, ChangeOperationKind, ImplementationChangeSet
    base = context_for("original source\n", "source.py")
    base = CodingContext.create(**{
        k: v for k, v in base.hash_payload().items() if k not in {"version", "entries"}
    }, entries=(CodingContextEntry("new_test.py", None, None, None), *base.entries))
    previous = ImplementationChangeSet(
        change_set_id="previous", plan_version_id=base.plan_version_id,
        plan_content_hash=base.plan_content_hash, plan_record_hash=base.plan_record_hash,
        operations=(
            ChangeOperation("new_test.py", ChangeOperationKind.WRITE, None,
                            "import pytest\n\ndef test_behavior():\n    assert True\n", False),
            ChangeOperation("source.py", ChangeOperationKind.WRITE, base.entries[1].prior_hash,
                            "fixed source\n", False),
        ),
    )
    return base, previous


def test_repair_edits_previous_new_file_and_preserves_other_cumulative_changes():
    from app.coding_repair import repair_context, cumulative_operations
    from app.sandbox_worker.implementation import ImplementationChangeSet
    from app.coding import _validate_proposed_operations
    base, previous = repair_fixture()
    paths = previous.paths
    view = repair_context(base, previous, allowed_paths=paths)
    assert base.entries[0].content is None
    assert view.entries[0].content.startswith("import pytest\n")
    proposal = edit_proposal(view, "import pytest\n\n", "")
    _, operations = _parse_change_set_proposal(json.dumps(proposal), view, strict_prior_hash=True)
    merged = cumulative_operations(base, view, previous, operations, allowed_paths=paths)
    cumulative = ImplementationChangeSet(
        change_set_id="repaired", plan_version_id=base.plan_version_id,
        plan_content_hash=base.plan_content_hash, plan_record_hash=base.plan_record_hash,
        operations=merged,
    )
    validated = _validate_proposed_operations(base, cumulative, allowed_paths=paths)
    assert validated.operations[0].expected_prior_hash is None
    assert validated.operations[0].content == "def test_behavior():\n    assert True\n"
    assert validated.operations[1] == previous.operations[1]
    # A second repair uses the cumulative output, rather than resurrecting the import.
    second_view = repair_context(base, validated, allowed_paths=paths)
    assert second_view.entries[0].content == validated.operations[0].content
    assert "pytest" not in second_view.entries[0].content


@pytest.mark.parametrize("failure", ["hash", "scope", "plan", "edit_hash", "write_hash", "unreadable", "secret"])
def test_repair_overlay_and_edits_reject_unproven_inputs(failure):
    from dataclasses import replace
    from app.coding_repair import repair_context
    base, previous = repair_fixture()
    paths = previous.paths
    if failure == "hash":
        previous = replace(previous, operations=(replace(previous.operations[1], expected_prior_hash="f" * 64),))
    elif failure == "scope":
        paths = ("new_test.py",)
    elif failure == "plan":
        previous = replace(previous, plan_record_hash="f" * 64)
    if failure in {"hash", "scope", "plan"}:
        with pytest.raises(ValueError):
            repair_context(base, previous, allowed_paths=paths)
        return
    view = repair_context(base, previous, allowed_paths=paths)
    proposal = edit_proposal(view, "import pytest\n\n", "")
    operation = proposal["operations"][0]
    if failure in {"edit_hash", "write_hash"}:
        operation["expected_prior_hash"] = None
        if failure == "write_hash":
            operation.update(kind="write", content="replaced", edits=None)
    elif failure == "unreadable":
        operation["path"] = "unknown.py"
    else:
        operation["edits"][0]["new_text"] = "ghp_EXECUTIONREPAIRCANARY123456789"
    with pytest.raises(ValueError):
        _parse_change_set_proposal(json.dumps(proposal), view, strict_prior_hash=True)


def test_repair_can_delete_previous_new_file_without_deleting_missing_base_file():
    from app.coding_repair import repair_context, cumulative_operations
    from app.sandbox_worker.implementation import ChangeOperation, ChangeOperationKind
    base, previous = repair_fixture()
    view = repair_context(base, previous, allowed_paths=previous.paths)
    operations = (ChangeOperation("new_test.py", ChangeOperationKind.DELETE, view.entries[0].prior_hash),)
    merged = cumulative_operations(base, view, previous, operations, allowed_paths=previous.paths)
    assert merged == (previous.operations[1],)


@pytest.mark.parametrize("new_file", [False, True])
def test_repair_context_is_used_by_real_worker_boundary(tmp_path, new_file):
    import asyncio
    from sqlalchemy import select
    from app.contribution_workflow import advance_workflows
    from app.models import ExecutionAttempt
    from app.coding import CodingContextService, CodingConversationService
    from app.artifacts import ArtifactStore
    from tests.test_workbench import automatic_fixture, populate_round
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review, verdict="block")
    populate_round(code, review, verdict="pass")
    # The repair must consume the previously implemented source, not the base.
    updated = context_for("def preserved_api():\n    return True\n", "app/plans.py")
    code.contents[3] = json.dumps(edit_proposal(updated, "return True", "return 42"))
    if new_file:
        original_capture = workers[1].runtime.capture

        async def capture(**kwargs):
            entries = await original_capture(**kwargs)
            return tuple(CodingContextEntry(e.path, None, None, None)
                         if e.path == "tests/test_planning.py" else e for e in entries)

        workers[1].runtime.capture = capture
        first = json.loads(code.contents[1])
        first["operations"].append({"path": "tests/test_planning.py", "kind": "write",
                                    "expected_prior_hash": None, "executable": False,
                                    "content": "import pytest\n\ndef test_behavior():\n    assert True\n"})
        code.contents[1] = json.dumps(first)
        new_test = context_for(first["operations"][1]["content"], "tests/test_planning.py")
        code.contents[3] = json.dumps(edit_proposal(new_test, "import pytest\n\n", ""))
    for _ in range(24):
        for worker in workers:
            job = asyncio.run(worker.run_once())
            if job:
                assert job.state == "succeeded", (job.kind, job.error_message)
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        attempts = list(session.scalars(select(ExecutionAttempt).order_by(ExecutionAttempt.attempt_number)))
        assert len(attempts) == 2
        coding = CodingContextService(session).get_session(attempts[-1].id)
        proposal = CodingConversationService(session, artifact_root=settings.artifact_root).proposals(coding.id)[-1]
        stored = json.loads(ArtifactStore(session, settings.artifact_root).read_bytes(proposal.change_set_artifact_id))
        assert stored["operations"][0]["content"] == (
            "def preserved_api():\n    return True\n" if new_file else "def preserved_api():\n    return 42\n")
        assert stored["operations"][0]["expected_prior_hash"] == hashlib.sha256(b"original content for app/plans.py\n").hexdigest()
        if new_file:
            assert stored["operations"][1]["expected_prior_hash"] is None
            assert stored["operations"][1]["content"] == "def test_behavior():\n    assert True\n"
        assert "CONTRIBOS_REPAIR_EDITING_RULES=" in code.invocations[-1].prompt


def failed_coding_fixture(tmp_path):
    import asyncio
    from app.contribution_workflow import advance_workflows
    from tests.test_workbench import automatic_fixture, populate_round
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    valid = code.contents[1]
    bad = edit_proposal(context_for("original content for app/plans.py\n", "app/plans.py"), "absent", "replacement")
    # Scope violations stop immediately; recoverable anchor mistakes now have
    # their own automatic, budget-bound correction coverage.
    bad['operations'][0]['path']='outside-approved.py'
    code.contents[1] = json.dumps(bad)
    for _ in range(8):
        for worker in workers:
            asyncio.run(worker.run_once())
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.active_blocker(task_id)
        job = next(j for j in wb.jobs(task_id) if j.kind == "change_set_proposal")
        assert job.state == "failed" and job.attempt_count == 1
        job_id = job.id
    return db, settings, task_id, workers, code, review, job_id, valid, json.dumps(bad)


def test_api_retry_resumes_same_authorization_and_attempt_idempotently(tmp_path):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.models import ExecutionAttempt
    from sqlalchemy import select
    from tests.test_workbench import drive_round
    db, settings, task_id, workers, code, review, job_id, valid, _ = failed_coding_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        authorization = wb.latest(task_id, "execution_authorized").record_hash
        blocker_hash = wb.active_blocker(task_id).record_hash
    with TestClient(create_app(settings)) as client:
        resumed = client.post(f"/api/v1/jobs/{job_id}/retry")
        assert resumed.status_code == 200, resumed.text
        replay = client.post(f"/api/v1/jobs/{job_id}/retry")
        assert replay.status_code == 200 and replay.json()["attempt_count"] == 1
        projection = client.get(f"/api/v1/tasks/{task_id}/workbench").json()
        assert projection["execution"]["blocked"] is None
        assert any(e["kind"] == "automation_resumed" for e in projection["events"])
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.active_blocker(task_id) is None
        assert wb.latest(task_id, "automation_resumed").payload["blocked_hash"] == blocker_hash
        assert wb.latest(task_id, "automation_blocked").record_hash == blocker_hash
    code.contents.append(valid)
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        assert wb.latest(task_id, "execution_authorized").record_hash == authorization
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 1


@pytest.mark.parametrize("reason", ["stop", "exhausted", "conversation"])
def test_retry_cannot_resume_stopped_run_or_extend_job_budget(tmp_path, reason):
    from app.contribution_workflow import retry_execution_job
    from app.jobs import JobService, JobTransitionError
    db, settings, task_id, workers, code, review, job_id, _, _ = failed_coding_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        if reason == "stop":
            wb.append(task_id, "stop_requested", {}, key="stop-test")
        elif reason == "exhausted":
            job = JobService(session).get(job_id)
            job.attempt_count = job.max_attempts
            session.commit()
        else:
            from app.coding import CodingConversationService
            job = JobService(session).get(job_id)
            CodingConversationService(session, artifact_root=settings.artifact_root).send_message(
                job.payload["execution_attempt_id"], content="补充修改方向", idempotency_key="stale-conversation")
        with pytest.raises(JobTransitionError):
            retry_execution_job(wb, job_id, settings)
        session.rollback()
        assert wb.latest(task_id, "automation_blocked")
        assert bool(wb.active_blocker(task_id)) == (reason != "stop")
        assert wb.latest(task_id, "automation_resumed") is None
        assert JobService(session).get(job_id).state == "failed"


def test_repeated_failure_reblocks_durably_and_remains_bounded(tmp_path):
    import asyncio
    from app.contribution_workflow import retry_execution_job, advance_workflows
    from app.jobs import JobService, JobTransitionError
    db, settings, task_id, workers, code, review, job_id, _, bad = failed_coding_fixture(tmp_path)
    for count in (2, 3):
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            retry_execution_job(wb, job_id, settings)
        code.contents.append(bad)
        job = asyncio.run(workers[2].run_once())
        assert job.state == "failed" and job.attempt_count == count
        advance_workflows(db, settings)
        with db.session() as session:
            wb = PlanningService(session, settings.artifact_root)
            assert wb.active_blocker(task_id)
            assert len([e for e in wb.history(task_id) if e.kind == "automation_blocked"]) == count
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        with pytest.raises(JobTransitionError, match="exhausted"):
            retry_execution_job(wb, job_id, settings)
        session.rollback()
        assert wb.active_blocker(task_id)


def test_resume_reprepares_disposed_environment_and_reruns_baseline(tmp_path):
    import asyncio
    from app.contribution_workflow import retry_execution_job, advance_workflows
    from app.sandbox_worker.workspace_jobs import preparation_receipt, preparation_needs_restore
    from app.sandbox_worker.dependency_jobs import load_dependency
    from app.workbench import WorkbenchError
    from tests.test_workbench import drive_round
    db, settings, task_id, workers, code, review, job_id, valid, _ = failed_coding_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        attempt_id = next(j for j in wb.jobs(task_id) if j.id == job_id).payload["execution_attempt_id"]
        original = preparation_receipt(wb, attempt_id)
        # Record a previously completed dependency allocation and its cleanup,
        # without requiring Docker/network in the deterministic test suite.
        dependency_hash = wb.artifact({"fixture_dependency": True})
        workspace = {"fixture_volume": "disposed"}
        allocated = wb.append(task_id, "dependency_workspace", {
            "attempt_id": attempt_id, "workspace": workspace}, key="test-allocation", job_id=original.job_id)
        wb.append(task_id, "dependencies_ready", {"attempt_id": attempt_id,
            "result_hash": dependency_hash, "workspace": workspace}, key="test-dependencies", job_id=original.job_id)
        baseline = wb.read(original.payload["result_hash"])
        baseline["dependency_result_hash"] = dependency_hash
        old = wb.append(task_id, "execution_baseline_ready", {"attempt_id": attempt_id,
            "result_hash": wb.artifact(baseline), "outcome": baseline["outcome"]},
            key="test-baseline-dependency", job_id=original.job_id)
        assert not preparation_needs_restore(wb, old)
        wb.append(task_id, "dependency_workspace_disposed", {"allocation_hash": allocated.record_hash}, key="test-disposal")
        assert preparation_needs_restore(wb, old)
        with pytest.raises(WorkbenchError, match="已回收"):
            load_dependency(wb, attempt_id)
        retry_execution_job(wb, job_id, settings)
        old_hash = old.record_hash
    code.contents.append(valid)
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        latest = preparation_receipt(wb, attempt_id)
        assert latest.record_hash != old_hash
        assert wb.read(latest.payload["result_hash"])["replaces_baseline_hash"] == old_hash
        assert not preparation_needs_restore(wb, latest)
        jobs = [j for j in wb.jobs(task_id) if j.kind == "execution_prepare"]
        assert len(jobs) == 2 and all(j.state == "succeeded" for j in jobs)


def test_failed_review_resumes_same_verified_evidence_without_new_execution(tmp_path):
    import asyncio
    from sqlalchemy import select
    from app.models import ExecutionAttempt
    from app.contribution_workflow import retry_execution_job, advance_workflows
    from tests.test_workbench import automatic_fixture, populate_round, drive_round
    db, settings, task_id, workers, code, review = automatic_fixture(tmp_path)
    populate_round(code, review)
    good = review.contents[0]
    review.contents[0] = json.dumps({"verdict": "pass", "reason_code": "tests_passed",
                                   "findings": [{"severity": "critical"}]})
    for _ in range(10):
        for worker in workers:
            asyncio.run(worker.run_once())
        advance_workflows(db, settings)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        job = next(j for j in wb.jobs(task_id) if j.kind == "provider_review")
        assert job.state == "failed" and job.error_code == "review_output_invalid"
        assert "severity" in job.error_message
        frozen_hash = job.payload_hash
        retry_execution_job(wb, job.id, settings)
        assert wb.active_blocker(task_id) is None
        assert job.payload_hash == frozen_hash
    review.contents.append(good)
    drive_round(db, settings, task_id, workers)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert wb.latest(task_id, "awaiting_acceptance")
        assert len(list(session.scalars(select(ExecutionAttempt)))) == 1
        assert "CONTRIBOS_REVIEW_OUTPUT_RULES=" in review.invocations[-1].prompt
        assert "implementation_steps" in review.invocations[-1].prompt


def test_dependency_cleanup_does_not_treat_resolved_blocker_as_active(tmp_path):
    import asyncio
    from app.contribution_workflow import retry_execution_job
    from app.sandbox_worker.dependency_jobs import DependencyJobWorker
    db, settings, task_id, workers, code, review, job_id, _, _ = failed_coding_fixture(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        prepare = next(j for j in wb.jobs(task_id) if j.kind == "execution_prepare")
        allocated = wb.append(task_id, "dependency_workspace", {
            "attempt_id": prepare.payload["execution_attempt_id"], "workspace": {}},
            key="cleanup-allocation", job_id=prepare.id)
        wb.append(task_id, "automation_blocked", {"reason": "retryable coding failure"}, key="cleanup-blocker")
        retry_execution_job(wb, job_id, settings)
    class Runtime:
        async def destroy(self, *args, **kwargs):
            pytest.fail("A resolved blocker must not dispose the active environment")
    worker = DependencyJobWorker(db, settings, Runtime(), None, worker_id="cleanup")
    asyncio.run(worker.cleanup())
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        assert not any(e.kind == "dependency_workspace_disposed" and
                       e.payload["allocation_hash"] == allocated.record_hash for e in wb.history(task_id))
