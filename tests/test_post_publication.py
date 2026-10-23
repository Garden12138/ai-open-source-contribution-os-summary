"""Ordinary coordinator -> isolated read-only worker -> exact consent boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.conversation import VERSION, advance_conversations, conversation_view, receive_message
from app.jobs import JobService
from app.models import Job
from app.post_publication import attribute_maintenance_origin
from app.pr_lifecycle import PREPARE, FOLLOWUP, WRITE, lifecycle_view, request_lifecycle
from app.publisher_accounts import append_account
from app.workbench import Workbench, WorkbenchError
from tests.test_pr_lifecycle import lifecycle, flow, published, preview, run  # noqa: F401


@pytest.fixture
def automatic(lifecycle):
    db, settings, task, *_ = lifecycle
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        # Fixture-only conversation enrollment, no simulated user authorization.
        wb.append(task, "conversation_started", {"version": VERSION}, key="native-fixture")
    return lifecycle


def tick(f):
    advance_conversations(f[0], f[1])


def state(f):
    db, settings, task, *_ = f
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        return wb.history(task), wb.jobs(task), conversation_view(wb, task), lifecycle_view(wb, task)


def auto_jobs(jobs):
    return [j for j in jobs if j.payload.get("trigger", {}).get("kind") == "workflow"]


def test_publication_continues_without_another_message_and_never_marks_ready(automatic):
    f = automatic
    before, _, _, _ = state(f)
    tick(f)
    events, jobs, view, _ = state(f)
    job, = auto_jobs(jobs)
    publication = next(e for e in before if e.kind == "publication_completed")
    assert job.kind == PREPARE and job.max_attempts == 2
    assert job.payload["trigger"] == {"kind": "workflow", "source_id": publication.id, "source_hash": publication.record_hash}
    assert next(e for e in events if e.kind == "job_queued" and e.job_id == job.id).actor_id == "system"
    assert view["user_state"]["busy"]
    assert run(f).state == "succeeded"
    for _ in range(3):
        tick(f)
    events, jobs, view, lifecycle_view_ = state(f)
    assert len(auto_jobs(jobs)) == 1
    assert view["pending_action"]["kind"] == "ready"
    assert "单独确认" in view["user_state"]["next_step"]
    assert lifecycle_view_["snapshot"]["ci"] == "awaiting_approval"
    assert not lifecycle_view_["preview"]["consumed"]
    assert f[4].ready_calls == 0 and f[4].prs[0]["draft"] is True
    assert not any(j.kind == WRITE for j in jobs)
    assert not any(e.kind in {"pr_ready_confirmed", "conversation_received"} for e in events)
    assert len([e for e in events if e.kind == "post_publication_finished"]) == 1
    assert all(r["origin"]["kind"] == "workflow" for r in view["results"])
    assert [(e.id, e.record_hash, e.payload) for e in events[:len(before)]] == [(e.id, e.record_hash, e.payload) for e in before]


def test_coordinators_and_restarts_do_not_duplicate_the_handoff(automatic):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(automatic), range(2)))
    assert len(auto_jobs(state(automatic)[1])) == 1
    # A new coordinator call has no in-memory state and adopts the persisted job.
    tick(automatic)
    assert run(automatic).state == "succeeded"
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: tick(automatic), range(2)))
    events, jobs, _, _ = state(automatic)
    assert len(auto_jobs(jobs)) == 1
    assert len([e for e in events if e.kind == "pr_ready_prepared"]) == 1
    assert len([e for e in events if e.kind == "post_publication_finished"]) == 1


@pytest.mark.parametrize("recover", [False, True])
def test_read_failures_retry_within_one_persistent_budget(automatic, monkeypatch, recover):
    monkeypatch.setattr("app.post_publication.RETRY_DELAY_SECONDS", 0)
    f = automatic
    original = f[4].api
    async def unavailable(*args, **kwargs):
        raise WorkbenchError("临时无法读取远端", code="pr_read_unavailable")
    f[4].api = unavailable
    tick(f)
    assert run(f).state == "failed"
    tick(f)
    if recover:
        f[4].api = original
    result = run(f)
    assert result.state == ("succeeded" if recover else "failed")
    for _ in range(3):
        tick(f)
    events, jobs, view, _ = state(f)
    job, = auto_jobs(jobs)
    assert job.attempt_count == job.max_attempts == 2
    assert len([e for e in events if e.kind == "post_publication_retrying"]) == 1
    assert f[4].ready_calls == 0
    finished = next(e for e in events if e.kind == "post_publication_finished")
    assert finished.payload["status"] == ("awaiting_confirmation" if recover else "blocked")
    if not recover:
        assert "临时无法读取远端" in view["user_state"]["next_step"]


@pytest.mark.parametrize("damage", ["head", "closed", "account", "partial", "already_ready"])
def test_remote_and_account_changes_never_authorize_writes(automatic, damage):
    f = automatic
    tick(f)
    if damage == "head": f[4].prs[0]["head"]["sha"] = "e" * 40
    if damage == "closed": f[4].prs[0]["state"] = "closed"
    if damage == "partial": f[4].partial = True
    if damage == "already_ready": f[4].prs[0]["draft"] = False
    result = run(f)
    if damage == "account":
        with f[0].session() as session:
            append_account(session, {"state": "connected", "login": "other", "user_id": 456,
                "scopes": ["public_repo"], "credential_ref": "other-reference"}, correlation_id="changed")
            session.commit()
    tick(f)
    events, jobs, view, _ = state(f)
    assert len(auto_jobs(jobs)) == 1 and f[4].ready_calls == 0
    finished = next(e for e in events if e.kind == "post_publication_finished")
    assert finished.payload["status"] in {"blocked", "not_eligible", "already_ready", "awaiting_confirmation"}
    assert not any(e.kind == "pr_ready_confirmed" for e in events)
    if damage in {"head", "closed"}:
        assert result.error_code == "pr_ready_target_stale"
    if damage == "already_ready":
        assert view["pending_action"] is None
    if damage == "partial":
        assert finished.payload["status"] == "awaiting_confirmation"
        assert state(f)[3]["snapshot"]["complete"] is False


@pytest.mark.parametrize("guard", ["paused", "archived", "stop"])
def test_explicit_stop_or_archive_prevents_automatic_queue(automatic, guard):
    db, settings, task, *_ = automatic
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        if guard == "archived":
            from app.task_visibility import TaskVisibilityService
            TaskVisibilityService(session).change(task, target="archived", expected_sequence=0)
        else:
            wb.append(task, "conversation_paused" if guard == "paused" else "stop_requested", {"version": VERSION}, key="stop-fixture")
    tick(automatic)
    with db.session() as session:
        assert not auto_jobs(list(session.scalars(select(Job))))


def test_existing_fresh_manual_preview_is_preserved_and_correctly_attributed(automatic):
    f = automatic
    manual = preview(f)
    original, jobs, _, _ = state(f)
    manual_job = next(j for j in jobs if j.kind == PREPARE)
    original_payload = manual_job.payload
    tick(f)
    assert auto_jobs(state(f)[1])[0].kind == FOLLOWUP
    assert run(f).state == "succeeded"
    tick(f)
    events, _, view, current = state(f)
    assert current["preview"]["record_hash"] == manual["record_hash"]
    manual_result = next(r for r in view["results"] if r.get("source_kind") == "pr_ready_prepared")
    assert manual_result["origin"]["kind"] == "request"
    with f[0].session() as session:
        wb = Workbench(session, f[1].artifact_root)
        attribute_maintenance_origin(wb, f[2], manual_job.id, reason="Actual caller verified as maintenance, not the native user.")
    events, jobs, corrected, _ = state(f)
    result = next(r for r in corrected["results"] if r.get("source_kind") == "pr_ready_prepared")
    assert result["origin"] == {"kind": "maintenance", "label": "维护操作触发"}
    assert result["text"] == manual_result["text"]
    assert next(j for j in jobs if j.id == manual_job.id).payload == original_payload
    assert [(e.id, e.record_hash, e.actor_id, e.payload) for e in events[:len(original)]] == [(e.id, e.record_hash, e.actor_id, e.payload) for e in original]
    assert f[4].ready_calls == 0


def test_expired_preview_is_replaced_by_a_new_source_bound_version(automatic):
    with patch("app.pr_lifecycle.datetime") as clock:
        clock.now.return_value = datetime.now(timezone.utc) - timedelta(hours=1)
        old = preview(automatic)
    tick(automatic)
    assert auto_jobs(state(automatic)[1])[0].kind == PREPARE
    assert run(automatic).state == "succeeded"
    tick(automatic)
    events, _, view, current = state(automatic)
    assert current["preview"]["record_hash"] != old["record_hash"]
    assert any(e.record_hash == old["record_hash"] for e in events)
    assert view["pending_action"]["source_id"] == next(e.id for e in events if e.record_hash == current["preview"]["record_hash"])


def test_cancelled_read_is_not_automatically_restarted(automatic):
    tick(automatic)
    job, = auto_jobs(state(automatic)[1])
    with automatic[0].session() as session:
        JobService(session).request_cancel(job.id)
    tick(automatic)
    assert len(auto_jobs(state(automatic)[1])) == 1
    assert next(e for e in state(automatic)[0] if e.kind == "post_publication_finished").payload["status"] == "stopped"


def test_coordinator_does_not_need_publisher_management_credentials(automatic):
    advance_conversations(automatic[0], replace(automatic[1], publisher_management_key=None, publisher_mode="none"))
    assert len(auto_jobs(state(automatic)[1])) == 1


def test_upgrade_recovers_only_the_exact_old_capability_failure(automatic):
    f = automatic
    with f[0].session() as session:
        wb = Workbench(session, f[1].artifact_root)
        old = wb.append(f[2], "post_publication_finished", {"version": "post-publication-workflow-v1",
            "publication_hash": wb.latest(f[2], "publication_completed").record_hash, "status": "blocked",
            "message": "尚未启用独立 Publisher，请在设置中核对。", "preview_hash": None}, key="legacy-capability")
    tick(f)
    tick(f)
    assert run(f).state == "succeeded"
    tick(f)
    tick(f)
    events, jobs, view, _ = state(f)
    assert len(auto_jobs(jobs)) == 1
    assert len([e for e in events if e.kind == "post_publication_recovered"]) == 1
    assert next(e for e in events if e.id == old.id).record_hash == old.record_hash
    assert view["pending_action"]["kind"] == "ready" and f[4].ready_calls == 0


def test_queue_and_provenance_are_atomic_on_failure(automatic, monkeypatch):
    original = Workbench.append
    def interrupted(self, task_id, kind, *args, **kwargs):
        if kind == "post_publication_step":
            raise WorkbenchError("跟进记录写入中断")
        return original(self, task_id, kind, *args, **kwargs)
    monkeypatch.setattr(Workbench, "append", interrupted)
    tick(automatic)
    events, jobs, _, _ = state(automatic)
    assert not auto_jobs(jobs)
    assert not any(e.kind == "pr_lifecycle_triggered" for e in events)
    assert next(e for e in events if e.kind == "post_publication_finished").payload["status"] == "blocked"


def test_explicit_saved_only_request_prevents_an_interposed_remote_read(automatic):
    db, settings, task, *_ = automatic
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        message = wb.append(task, "conversation_received", {"version": VERSION,
            "text": "不要刷新PR，只解释保存的状态"}, key="saved-only", actor="local-user")
        wb.append(task, "conversation_finished", {"version": VERSION, "message_id": message.id,
            "status": "completed"}, key="saved-only-finished")
    tick(automatic)
    assert not auto_jobs(state(automatic)[1])


def test_user_can_resume_failed_handoff_in_the_original_conversation(automatic):
    f = automatic
    original = f[4].api
    async def unavailable(*args, **kwargs):
        raise WorkbenchError("暂时无法读取", code="pr_ready_target_stale")
    f[4].api = unavailable
    tick(f)
    assert run(f).state == "failed"
    tick(f)
    f[4].api = original
    with f[0].session() as session:
        receive_message(Workbench(session, f[1].artifact_root), f[2], text="继续", key="resume-native", settings=f[1])
    result = run(f)
    assert result.kind == PREPARE and result.state == "succeeded"
    tick(f)
    _, _, view, _ = state(f)
    assert view["pending_action"]["kind"] == "ready"
    assert "审核预览已就绪" in view["user_state"]["next_step"]
    assert f[4].ready_calls == 0


def test_workflow_origin_requires_verified_publication_source(automatic):
    db, settings, task, account, *_ = automatic
    with db.session() as session:
        with pytest.raises(WorkbenchError):
            request_lifecycle(Workbench(session, settings.artifact_root), task, kind=PREPARE,
                account_hash=account, key="spoofed", origin="workflow")


def test_api_returns_origins_and_confirmation_action_without_another_user_message(automatic):
    tick(automatic)
    assert run(automatic).state == "succeeded"
    tick(automatic)
    app = create_app(automatic[1])
    with TestClient(app) as client:
        response = client.get(f"/api/v1/tasks/{automatic[2]}/workbench",
            headers={"X-ContribOS-Token": automatic[1].local_access_token})
    assert response.status_code == 200, response.text
    conversation = response.json()["conversation"]
    assert conversation["pending_action"]["kind"] == "ready"
    assert any(r.get("origin", {}).get("kind") == "workflow" for r in conversation["results"])
    assert not conversation["messages"] and automatic[4].ready_calls == 0
