"""Conversation journal, recovery, routing and authorization boundary contracts."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import create_app
from app.conversation import (ConversationIntentWorker, advance_conversations, conversation_view,
                              fast_intent, pending_messages, receive_message, require_settled_messages)
from app.contribution_workflow import authorize_execution, advance_workflows
from app.jobs import JobService
from app.models import Job, PublishIntent
from app.schemas import PlanVersionCreateRequest
from app.planner import PlanningService
from app.workbench import Workbench, WorkbenchError
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import seeded, result, run_turn, automatic_fixture, populate_round, drive_round
from tests.test_pr_lifecycle import lifecycle, flow, published  # noqa: F401


def send(db, settings, task, text, key="message"):
    with db.session() as session:
        return receive_message(Workbench(session, settings.artifact_root), task, text=text, key=key, settings=settings)


def classify(db, settings, intent="plan", *extra):
    from app.conversation_reasoning import WORKFLOWS
    action = next((name for name, value in WORKFLOWS.items() if value == intent), "respond")
    runner = ScriptedRunner(*extra, json.dumps({"action":action,
        "reply":"还没有开始执行，所以目前没有测试结果。" if intent == "question" else "我会根据当前要求接续处理。",
        "reads":[], "references":[]}))
    worker = ConversationIntentWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="intent-test")
    job = asyncio.run(worker.run_once())
    assert job.state == "succeeded", (job.error_code, job.error_message)
    return runner


def view(db, settings, task):
    with db.session() as session:
        return conversation_view(Workbench(session, settings.artifact_root), task)


@pytest.mark.parametrize('existing_receipt',[False,True])
def test_publication_success_returns_clickable_url_and_enriches_old_receipt_without_mutation(tmp_path,existing_receipt):
    from app.conversation import _results
    db,s,task,*_=seeded(tmp_path)
    with db.session() as session:
        wb=Workbench(session,s.artifact_root)
        wb.append(task,'conversation_started',{'version':'conversation-v1'},key='started')
        published=wb.append(task,'publication_completed',{'html_url':'https://github.com/fixture/repo/pull/7','number':7},key='published')
        old=None
        if existing_receipt:
            old=wb.append(task,'conversation_result',{'version':'conversation-v1','source_id':published.id,
                'source_kind':'publication_completed','text':'草稿 PR 已创建。你可以让我跟进项目检查与维护者反馈。'},key='conversation-source:'+published.id)
        _results(wb,task);session.commit()
        before=len(wb.history(task))
        _results(wb,task);session.commit()
        assert len(wb.history(task))==before
        response=conversation_view(wb,task)['results'][-1]['text']
        assert response.count('[查看 PR #7](https://github.com/fixture/repo/pull/7)')==1
        if old:
            persisted=next(e for e in wb.history(task) if e.id==old.id)
            assert persisted.record_hash==old.record_hash
            assert 'https://' not in persisted.payload['text']
        assert not wb.jobs(task)


@pytest.mark.parametrize('url,number',[
    ('https://github.com/fixture/repo/pull/7',8),
    ('https://github.com.evil.test/fixture/repo/pull/7',7),
    ('https://github.com@evil.test/fixture/repo/pull/7',7),
    ('javascript:alert(7)',7),('https://github.com/fixture/repo/pull/7?token=secret',7),
    (None,7),('https://github.com/fixture/repo/pull/1',True),
])
def test_publication_success_link_rejects_malformed_or_mismatched_target(url,number):
    from app.conversation import publication_link
    assert publication_link({'html_url':url,'number':number}) is None


def test_received_messages_survive_upgrade_and_replay_without_jobs_or_writes(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    first = send(db, s, task, "请只修改这个包", "one")
    assert first["status"] == "queued"
    db.create_schema()  # Forward upgrade of an existing journal does not rebuild tables.
    assert send(db, s, task, "请只修改这个包", "one") == first
    with pytest.raises(WorkbenchError, match="内容不同"):
        send(db, s, task, "另一条内容", "one")
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        assert len(pending_messages(wb, task)) == 1
        assert not wb.jobs(task)
        assert session.scalar(select(PublishIntent)) is None
    assert view(db, s, task)["messages"][0]["text"] == "请只修改这个包"


def test_fifo_append_progress_question_and_restart_preserve_inputs(tmp_path):
    db, s, task, content, *_ = seeded(tmp_path)
    send(db, s, task, "先看看怎么改", "one")
    send(db, s, task, "还要考虑空字符串", "two")
    advance_conversations(db, s)
    classify(db, s)
    advance_conversations(db, s)
    send(db, s, task, "现在到哪了", "progress")
    advance_conversations(db, s)
    classify(db, s, "question")
    advance_conversations(db, s)
    snap = view(db, s, task)
    assert snap["messages"][2]["receipt"]["status"] == "completed"
    assert snap["messages"][1]["receipt"]["status"] == "queued"
    job, runner = run_turn(db, s, result(content))
    assert job.state == "succeeded"
    advance_conversations(db, s)
    classify(db, s)
    advance_conversations(db, s)
    with db.session() as session:
        jobs = [j for j in Workbench(session, s.artifact_root).jobs(task) if j.kind == "planning_turn"]
        assert [j.payload["text"] for j in jobs] == ["先看看怎么改", "还要考虑空字符串"]
    job, _ = run_turn(db, s, result(content))
    assert job.state == "succeeded"
    advance_conversations(db, s)
    assert view(db, s, task)["pending_count"] == 0
    assert not any("哈希" in r["text"] for r in view(db, s, task)["results"])


def test_stop_retains_queued_messages_until_explicit_continue(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    send(db, s, task, "还要考虑空字符串", "one")
    send(db, s, task, "停止", "stop")
    for _ in range(3):
        advance_conversations(db, s)
    snap = view(db, s, task)
    assert snap["paused"] and snap["pending_count"] == 1
    assert snap["messages"][0]["receipt"]["status"] == "paused"
    assert any("安全停止" in r["text"] for r in snap["results"])
    send(db, s, task, "继续", "resume")
    advance_conversations(db, s)
    assert not view(db, s, task)["paused"]
    with db.session() as session:
        jobs = Workbench(session, s.artifact_root).jobs(task)
        assert len(jobs) == 1 and jobs[0].kind == "conversation_intent"


def test_queue_capacity_allows_stop_and_receive_transaction_rolls_back(tmp_path, monkeypatch):
    db, s, task, *_ = seeded(tmp_path)
    for i in range(20):
        send(db, s, task, f"补充信息 {i}", str(i))
    with pytest.raises(WorkbenchError, match="20 条"):
        send(db, s, task, "再补充", "overflow")
    original = Workbench.append
    def fail(self, task_id, kind, *args, **kwargs):
        if kind == "conversation_paused":
            raise WorkbenchError("injected crash")
        return original(self, task_id, kind, *args, **kwargs)
    monkeypatch.setattr(Workbench, "append", fail)
    with pytest.raises(WorkbenchError, match="injected"):
        send(db, s, task, "停止", "stop")
    assert len(view(db, s, task)["messages"]) == 20
    assert not view(db, s, task)["paused"]
    monkeypatch.setattr(Workbench, "append", original)
    send(db, s, task, "停止", "stop")
    assert view(db, s, task)["paused"]


def test_model_format_correction_is_counted_and_never_authorizes(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    send(db, s, task, "好，可以", "agreement")
    advance_conversations(db, s)
    runner = classify(db, s, "clarify", "not json")
    assert len(runner.invocations) == 2
    advance_conversations(db, s)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        assert len([e for e in wb.history(task) if e.kind == "model_call"]) == 2
        assert sum(e.payload["usage"]["input_tokens"] for e in wb.history(task) if e.kind == "conversation_usage") == 200
        assert not wb.latest(task, "execution_authorized")
        assert not wb.latest(task, "publication_confirmed")
    assert view(db, s, task)["pending_count"] == 0


def test_explanation_uses_evidence_question_worker_without_stopping(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    send(db, s, task, "为什么目前的测试还没完成", "explain")
    advance_conversations(db, s)
    classify(db, s, "question")
    advance_conversations(db, s)
    assert any("没有测试结果" in r["text"] for r in view(db, s, task)["results"])
    assert not view(db, s, task)["paused"]


def test_goal_change_stops_old_work_and_creates_child_plan_without_approval(tmp_path):
    db, s, task, workers, code, review = automatic_fixture(tmp_path)
    with db.session() as session:
        parent = Workbench(session, s.artifact_root).latest_plan(task)
    send(db, s, task, "只改当前模块，并考虑空字符串", "change")
    advance_conversations(db, s)
    classify(db, s, "replan")
    advance_conversations(db, s)
    advance_workflows(db, s)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        assert wb.latest(task, "execution_change_requested")
        assert wb.latest(task, "stop_requested")
        new_jobs = [j for j in wb.jobs(task) if j.kind == "planning_turn"]
        assert new_jobs[-1].payload["text"] == "只改当前模块，并考虑空字符串"
        assert wb.latest_plan(task).record_hash == parent.record_hash
        assert len([e for e in wb.history(task) if e.kind == "execution_authorized"]) == 1


def test_confirmations_reject_pending_messages_and_old_tasks_are_read_only(tmp_path):
    db, s, task, content, context = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, s.artifact_root)
        plan = wb.save(task, PlanVersionCreateRequest.model_validate(content.hash_payload()), context_hash=context.record_hash, key="plan")
        session.commit()
        before = [(e.id, e.record_hash) for e in wb.history(task)]
    advance_conversations(db, s)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        assert before == [(e.id, e.record_hash) for e in wb.history(task)]
    send(db, s, task, "只改一个文件", "changed")
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        with pytest.raises(WorkbenchError, match="补充信息"):
            authorize_execution(wb, task, plan_id=plan.id, plan_hash=plan.record_hash, key="approve", settings=s)
        assert not wb.latest(task, "execution_authorized")


def test_result_receipts_are_durable_source_bound_and_exactly_once(tmp_path):
    db, s, task, workers, code, review = automatic_fixture(tmp_path)
    send(db, s, task, "现在到哪了", "progress")
    advance_conversations(db, s)
    populate_round(code, review)
    drive_round(db, s, task, workers)
    for _ in range(3):
        advance_conversations(db, s)
    snap = view(db, s, task)
    results = [r for r in snap["results"] if r.get("source_kind") == "awaiting_acceptance"]
    assert len(results) == 1 and "条验证命令通过" in results[0]["text"]
    with db.session() as session:
        source = Workbench(session, s.artifact_root).latest(task, "awaiting_acceptance")
        assert results[0]["source_id"] == source.id
        assert results[0]["source_sequence"] == source.sequence


def test_conversation_api_access_idempotency_sensitive_data_and_contract(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    s = replace(s, local_access_token="local-conversation-test")
    url = f"/api/v1/tasks/{task}/workbench"
    headers = {"X-ContribOS-Token":s.local_access_token, "Idempotency-Key":"api-message"}
    with TestClient(create_app(s)) as client:
        assert client.post(url + "/conversation/messages", json={"text":"现在到哪了"}).status_code == 401
        assert client.post(url + "/conversation/messages", json={"text":"现在到哪了"}, headers={**headers,"Origin":"https://foreign.invalid"}).status_code == 403
        response = client.post(url + "/conversation/messages", json={"text":"现在到哪了"}, headers=headers)
        assert response.status_code == 202, response.text
        assert set(response.json()) == {"message_id","status","text"}
        assert client.post(url + "/conversation/messages", json={"text":"现在到哪了"}, headers=headers).json() == response.json()
        canary = "ghp_CONVERSATIONCANARY12345678901234567890"
        rejected = client.post(url + "/conversation/messages", json={"text":canary}, headers={**headers,"Idempotency-Key":"canary"})
        assert rejected.status_code == 422 and canary not in rejected.text
        assert client.post(url + "/conversation/messages", json={"text":"now","mode":"question"}, headers=headers).status_code == 422
        advance_conversations(db, s)
        with db.session() as session:
            assert not Workbench(session, s.artifact_root).latest(task, "model_call")
        advance_conversations(db, s)
        assert client.get(url).json()["conversation"]["pending_count"] == 0
        assert client.get(url + "/timeline").json()["conversation"]["version"] == "conversation-v1"
        assert canary not in json.dumps(client.get(url).json())


def test_published_status_and_followup_are_consistent_and_chat_cannot_write(lifecycle):
    from app.dashboard import ContributionDashboardService
    db, s, task, account, transport, worker = lifecycle
    s = replace(s, implementation_provider="nvidia_nim")
    transport.prs[0]["draft"] = False
    send(db, s, task, "跟进一下 PR", "follow")
    advance_conversations(db, s)
    with db.session() as session:
        assert not any(j.kind == "conversation_intent" for j in Workbench(session, s.artifact_root).jobs(task))
    advance_conversations(db, s)
    job = asyncio.run(worker.run_once())
    assert job.state == "succeeded", (job.error_code, job.error_message)
    advance_conversations(db, s)
    assert view(db, s, task)["user_state"]["label"] == "自动检查等待维护者批准"
    with db.session() as session:
        summary = next(t for t in ContributionDashboardService(session).task_summaries() if t["id"] == task)
        assert summary["friendly_state"] == view(db, s, task)["user_state"]["label"]
    send(db, s, task, "没问题，准备发布", "published")
    advance_conversations(db, s)
    classify(db, s, "followup")
    advance_conversations(db, s)
    with db.session() as session:
        jobs = Workbench(session, s.artifact_root).jobs(task)
        assert jobs[-1].kind == "pr_followup"
    assert transport.ready_calls == 0 and transport.posts == 0


@pytest.mark.parametrize("text", ["怎么准备发布？", "不要准备发布", "是否可以签署 CLA", "好", "可以"])
def test_ambiguous_or_negative_text_has_no_fast_action(text):
    assert fast_intent(text) is None


@pytest.mark.parametrize("coordinator_has_management_key", [False, True])
def test_acceptance_prepares_document_and_preview_with_no_third_party_writes(tmp_path, coordinator_has_management_key):
    from app.execution_acceptance import accept_execution, acceptance_view
    from app.publication_documents import PublicationDocumentWorker
    from app.publisher_accounts import append_account
    from tests.test_publication_workbench import ready
    db, s, task, transport, publisher = ready(tmp_path)
    s = replace(s, publisher_management_key=bytes.fromhex("ac" * 32), implementation_provider="nvidia_nim")
    publisher.settings = s
    coordinator = replace(s, publisher_management_key=s.publisher_management_key if coordinator_has_management_key else None,
                          publisher_mode="none")
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        for job in wb.jobs(task):
            if job.kind == "publication_prepare":
                JobService(session).request_cancel(job.id)
        append_account(session, {"state":"connected", "login":"contributor", "user_id":123,
            "scopes":["public_repo"], "credential_ref":"private-reference"}, correlation_id="account")
        session.commit()
        binding = acceptance_view(wb, task)["binding_hash"]
        accept_execution(wb, task, binding_hash=binding, text="确认结果", key="accepted")
    send(db, coordinator, task, "没问题，准备发布", "prepare")
    advance_conversations(db, coordinator)
    classify(db, coordinator, "publication")
    advance_conversations(db, coordinator)
    assert asyncio.run(publisher.run_once()).state == "succeeded"
    advance_conversations(db, coordinator)
    assert view(db, s, task)["pending_count"] == 1  # Follow the durable child job.
    runner = ScriptedRunner(json.dumps({"title":"fix: preserve streamed content", "body":"## Summary\nPreserve streamed content.\n\n## Testing\nLocal sandbox verification completed.\n", "notes":[]}))
    model = PublicationDocumentWorker(db, coordinator, runner, _identity(s.implementation_model), worker_id="doc-test")
    job = asyncio.run(model.run_once())
    assert job.state == "succeeded", (job.error_code, job.error_message)
    assert job.result_data["next_job_id"]
    assert json.loads(runner.invocations[0].prompt)["evidence"]["user_request"] == "没问题，准备发布"
    job = asyncio.run(publisher.run_once())
    assert job.state == "succeeded", (job.error_code, job.error_message)
    advance_conversations(db, coordinator)
    assert view(db, s, task)["pending_count"] == 0
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        assert wb.latest(task, "publication_prepared")
        assert not wb.latest(task, "publication_confirmed")
        assert session.scalar(select(PublishIntent)) is None
    assert all(method == "GET" for method, _, _ in transport.calls)


@pytest.mark.parametrize("account_state", [None, "pending", "verification_failed", "disconnected"])
def test_unverified_publisher_account_preserves_acceptance_without_dispatch(tmp_path, account_state):
    from app.execution_acceptance import accept_execution, acceptance_view
    from app.publisher_accounts import append_account
    from tests.test_publication_workbench import ready
    db, s, task, transport, _ = ready(tmp_path)
    s = replace(s, publisher_management_key=None, publisher_mode="none", implementation_provider="nvidia_nim")
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        for job in wb.jobs(task):
            if job.kind == "publication_prepare":
                JobService(session).request_cancel(job.id)
        if account_state:
            append_account(session, {"state": account_state}, correlation_id="account")
            session.commit()
        binding = acceptance_view(wb, task)["binding_hash"]
        accept_execution(wb, task, binding_hash=binding, text="确认结果", key="accepted")
    send(db, s, task, "准备发布", "prepare")
    advance_conversations(db, s)
    classify(db, s, "clarify")
    advance_conversations(db, s)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        assert acceptance_view(wb, task)["accepted"]
        assert wb.latest(task, "conversation_finished").payload["status"] == "completed"
        from app.conversation_reasoning import observation
        assert "prepare_publication" not in observation(wb,task)["available_actions"]
        assert not any(j.kind == "publication_requirements" for j in wb.jobs(task))
        assert session.scalar(select(PublishIntent)) is None
        assert not wb.latest(task, "publication_confirmed")
    assert not transport.calls


@pytest.mark.parametrize("kind", ["publication", "cla", "update", "ready"])
def test_pending_message_blocks_every_remote_confirmation(tmp_path, kind):
    from app.publication import confirm_publication
    from app.cla import confirm_cla
    from app.pr_updates import confirm_update
    from app.pr_lifecycle import confirm_ready
    db, s, task, *_ = seeded(tmp_path)
    send(db, s, task, "还有一个要求", "pending")
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        with pytest.raises(WorkbenchError, match="补充信息"):
            if kind == "publication":
                confirm_publication(wb, task, intent_hash="a" * 64, nonce="b" * 64)
            elif kind == "cla":
                confirm_cla(wb, task, preview_hash="a" * 64, nonce="b" * 64,
                    agreement_hash="c" * 64, acknowledgement="确认", read_and_agree=True)
            else:
                (confirm_ready if kind == "ready" else confirm_update)(wb, task, preview_hash="a" * 64,
                    nonce="b" * 64, text="确认", approved=True)
        assert not wb.jobs(task)


def test_cancelled_intent_is_resumed_with_original_budget_and_input(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    send(db, s, task, "请只改这个包", "original")
    advance_conversations(db, s)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        job = next(j for j in wb.jobs(task) if j.kind == "conversation_intent")
        original_limit = job.max_attempts
    send(db, s, task, "停止", "stop")
    assert view(db, s, task)["pending_count"] == 1
    send(db, s, task, "继续", "continue")
    advance_conversations(db, s)
    classify(db, s, "plan")
    advance_conversations(db, s)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        intent_job = session.get(Job, job.id)
        assert intent_job.max_attempts == original_limit
        assert len([j for j in wb.jobs(task) if j.kind == "conversation_intent"]) == 1
        assert next(j for j in wb.jobs(task) if j.kind == "planning_turn").payload["text"] == "请只改这个包"


def test_intent_budget_is_shared_with_planning(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = Workbench(session, s.artifact_root)
        for i in range(32):
            wb.append(task, "model_call", {"purpose":"planning"}, key=f"call-{i}", commit=False)
        session.commit()
    send(db, s, task, "调整一下方案", "request")
    advance_conversations(db, s)
    runner = ScriptedRunner('{"intent":"plan"}')
    worker = ConversationIntentWorker(db, s, runner, _identity(s.implementation_model), worker_id="budget-test")
    job = asyncio.run(worker.run_once())
    assert job.state == "failed" and not runner.invocations
    advance_conversations(db, s)
    assert view(db, s, task)["pending_count"] == 0
    assert not any(e.get("source_kind") == "publication_completed" for e in view(db, s, task)["results"])
