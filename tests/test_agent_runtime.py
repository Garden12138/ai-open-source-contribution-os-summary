"""Runtime boundaries: interrupted dialogue, actual effects and request budgets."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.agent_context import INPUT_LIMIT, admit_prompt, input_upper_bound
from app.agent_runtime import activate, blocking_messages, enabled, focus, observe, record_focus, recovery
from app.conversation import (ConversationIntentWorker, advance_conversations, conversation_view,
                              receive_message, require_settled_messages)
from app.conversation_protocol import RUNTIME_REASONING_VERSION
from app.jobs import JobService
from app.task_states import ContributionTaskStateService
from app.workbench import Workbench, WorkbenchError
from tests.test_conversation_reasoning import run
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import seeded


def runtime_fixture(tmp_path):
    db, settings, task, *rest = seeded(tmp_path)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert activate(wb, task)
        session.commit()
    return db, settings, task, *rest


def send(db, settings, task, text, key="one", **kwargs):
    with db.session() as session:
        return receive_message(Workbench(session, settings.artifact_root), task,
            text=text, key=key, settings=settings, **kwargs)


def test_progress_interlude_uses_no_model_and_preserves_focus_and_domain_revision(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        source = w.append(task, "conversation_result", {"version":"conversation-v1", "text":"第一项是读取，第二项是测试。"}, key="details")
        record_focus(w, task, source, [{"name":"repository_file", "source_hash":"a"*64}])
        session.commit()
        before = observe(w, task)
    send(db, s, task, "现在进行到哪了？")
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        after = observe(w, task)
        assert after["focus"] == before["focus"] and after["revision"] == before["revision"]
        assert not w.jobs(task)
        assert conversation_view(w, task)["pending_count"] == 0


def test_multiple_reads_keep_actual_task_and_allow_foreground_during_background(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    send(db, s, task, "先看进度细节，再解释刚才的读取结果")
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        background = w.enqueue(task, "test_background", {}, key="background")
        background_id = background.id
        state_hash = ContributionTaskStateService(session).current(task).record_hash
    advance_conversations(db, s)
    job, runner = run(db, s,
        {"effect":"observe", "reads":[{"name":"progress"}]},
        {"effect":"observe", "reads":[{"name":"repository_file", "path":"app/plans.py"}]},
        {"effect":"observe", "reply":"任务还在运行；读取到的源码内容为 content。", "references":["repository_file"]})
    assert job.state == "succeeded", (job.error_code, job.error_message)
    assert len(runner.invocations) == 3
    assert all(json.loads(i.prompt)["version"] == RUNTIME_REASONING_VERSION for i in runner.invocations)
    assert all(input_upper_bound(i.prompt, i.output_schema) <= INPUT_LIMIT for i in runner.invocations)
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert JobService(session).get(background_id).state == "queued"
        assert ContributionTaskStateService(session).current(task).record_hash == state_hash
        assert not blocking_messages(w, task)
        assert focus(w, task)["references"][0]["name"] == "repository_file"
        assert not w.latest(task, "execution_authorized")


def test_observe_step_releases_barrier_but_cannot_escalate_to_command(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    send(db, s, task, "解释当前方案")
    advance_conversations(db, s)
    runner = ScriptedRunner(*[json.dumps(x) for x in (
        {"effect":"observe","reply":"","action":"respond","reads":[{"name":"task"}],"references":[]},
        {"effect":"command","reply":"改方案","action":"discuss_plan","reads":[],"references":[]},
    )])
    original = runner.complete
    async def complete(invocation):
        if runner.invocations:
            with db.session() as session:
                require_settled_messages(Workbench(session, s.artifact_root), task)
        return await original(invocation)
    runner.complete = complete
    worker = ConversationIntentWorker(db, s, runner, _identity(s.implementation_model), worker_id="observe-test")
    job = asyncio.run(worker.run_once())
    assert job.state == "failed"
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert not w.latest(task, "conversation_dispatched")
        assert not w.latest(task, "execution_authorized")


def test_reply_binding_survives_reopen_and_rejects_forged_sources(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        source = w.append(task, "conversation_result", {"version":"conversation-v1", "text":"第二项检查现有行为。"}, key="source")
        source_id, source_hash = source.id, source.record_hash
    send(db, s, task, "第二项什么意思？", reply_to_message_id=source_id, reply_to_message_hash=source_hash)
    advance_conversations(db, s)
    job, runner = run(db, s, {"effect":"observe", "reply":"第二项用于检查现有行为。", "references":["history"]})
    assert job.state == "succeeded", job.error_message
    prompt = json.loads(runner.invocations[0].prompt)
    assert prompt["reply_to"]["source_id"] == source_id
    assert prompt["tool_results"][0]["data"]["historical"] is True
    advance_conversations(db, s)
    db.close()
    from app.database import Database
    db = Database(s.database_url)
    with db.session() as session:
        assert focus(Workbench(session, s.artifact_root), task)
    with pytest.raises(WorkbenchError, match="不属于"):
        send(db, s, task, "执行这个", key="forged", reply_to_message_id=source_id, reply_to_message_hash="0"*64)


def test_admission_removes_long_history_before_call_and_denies_pinned_oversize(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        job = w.enqueue(task, "conversation_intent", {}, key="budget-job")
        body = {"message":"只改本 issue", "rules":["Preserve scope"], "task_memory":{"old":"历史"*20000}}
        prompt, receipt = admit_prompt(w, task, json.dumps(body), {"type":"object"}, job_id=job.id)
        assert receipt["admitted"] and input_upper_bound(prompt, {"type":"object"}) <= INPUT_LIMIT
        assert json.loads(prompt)["message"] == body["message"]
        with pytest.raises(WorkbenchError) as exc:
            admit_prompt(w, task, json.dumps({"message":"原文"*20000}), {"type":"object"}, job_id=job.id)
        assert exc.value.code == "conversation_input_budget"
        assert not w.latest(task, "model_call")


def test_upgrade_keeps_active_legacy_job_and_no_approval_is_created(tmp_path):
    db, s, task, *_ = seeded(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        job = w.enqueue(task, "planning_turn", {}, key="legacy")
        before = [(e.id, e.record_hash) for e in w.history(task)]
        assert not activate(w, task)
        assert not enabled(w, task)
        JobService(session).request_cancel(job.id)
        assert activate(w, task)
        session.commit()
        assert before == [(e.id, e.record_hash) for e in w.history(task)[:len(before)]]
        assert not w.latest(task, "execution_authorized")


@pytest.mark.parametrize("code,category,action", [
    ("environment_preparation_failed","environment","restore_environment"),
    ("lease_expired","interrupted","reconcile_execution"),
    ("conversation_input_budget","budget","resume_with_budget"),
    ("pr_ready_outcome_unknown","external_unknown","reconcile"),
    ("edit_anchor_missing","protocol","correct"),
])
def test_failure_recovery_does_not_confuse_environment_and_code(code, category, action):
    value = recovery(code, remaining=True)
    assert (value["category"], value["action"]) == (category, action)
    assert value["uses_code_repair"] is False


def test_frontend_contract_validates_reply_binding_and_returns_typed_observation(tmp_path):
    from app.api import create_app
    db, settings, task, *_ = runtime_fixture(tmp_path)
    s = replace(settings, local_access_token="runtime-contract-local")
    with TestClient(create_app(s)) as client:
        headers = {"X-ContribOS-Token":s.local_access_token, "Idempotency-Key":"reference"}
        url = f"/api/v1/tasks/{task}/workbench/conversation/messages"
        assert client.post(url, headers={**headers,"Origin":"https://evil.example"}, json={"text":"查看详情"}).status_code == 403
        assert client.post(url, headers=headers, json={"text":"查看详情","reply_to_message_id":"missing"}).status_code == 422
        response = client.get(f"/api/v1/tasks/{task}/workbench")
        assert response.status_code == 200, response.text
        assert response.json()["conversation"]["observation"]["version"] == "agent-runtime-v1"


def test_provider_foreground_is_not_blocked_by_long_background_call():
    from app.worker import ContribOSProviderWorker
    async def scenario():
        background_started, foreground_ran, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Background:
            async def run_once(self):
                background_started.set()
                await release.wait()
                return None
        class Foreground:
            async def run_once(self):
                await background_started.wait()
                foreground_ran.set()
                await release.wait()
                return None
        worker = object.__new__(ContribOSProviderWorker)
        for name in ("model_test","publication_document","planning","analysis","coding","review","execution_chat","conversation_intent"):
            setattr(worker, name, None)
        worker.coding, worker.conversation_intent = Background(), Foreground()
        task = asyncio.create_task(worker.run_forever(poll_interval_seconds=0.1))
        try:
            await asyncio.wait_for(foreground_ran.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_direct_coding_reaches_real_domain_review_without_narrative_turn(tmp_path, monkeypatch):
    from tests import test_workbench as fixtures
    from app.models import CodingTurn
    from sqlalchemy import select
    original = fixtures.seeded
    def seed(path):
        result = original(path)
        db, s, task = result[:3]
        with db.session() as session:
            assert activate(Workbench(session, s.artifact_root), task)
            session.commit()
        return result
    monkeypatch.setattr(fixtures, "seeded", seed)
    db, s, task, workers, code, review = fixtures.automatic_fixture(tmp_path)
    fixtures.populate_round(code, review)
    code.contents.pop(0)  # A direct proposal needs no invented assistant design.
    fixtures.drive_round(db, s, task, workers)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert w.latest(task, "awaiting_acceptance")
        assert len(code.invocations) == 1
        assert list(session.scalars(select(CodingTurn))) == []
        assert not w.latest(task, "publication_confirmed")


def test_admitted_model_prompt_matches_its_checkpoint(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    for i in range(15):
        with db.session() as session:
            Workbench(session, s.artifact_root).append(task, "conversation_result",
                {"version":"conversation-v1", "text":"旧的说明"*250}, key="old:"+str(i))
    send(db, s, task, "只解释当前结果，不要执行")
    advance_conversations(db, s)
    job, runner = run(db, s, {"effect":"observe", "reply":"目前没有实施结果。"})
    assert job.state == "succeeded", job.error_message
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        event = w.latest(task, "model_call")
        checkpoint = w.read(event.payload["context_checkpoint_hash"])
        from app.provenance import canonical_json, content_hash
        assert canonical_json(checkpoint["prompt"]) == runner.invocations[-1].prompt
        assert checkpoint["prompt_hash"] == content_hash(runner.invocations[-1].prompt)


def test_continue_resumes_original_failed_request_after_progress_and_detail(tmp_path):
    from app.agent_runtime import request_text
    db, s, task, *_ = runtime_fixture(tmp_path)
    send(db, s, task, "只修改本 issue，保留其他测试要求")
    advance_conversations(db, s)
    job, _ = run(db, s, "invalid json", "still invalid json")
    assert job.state == "failed"
    advance_conversations(db, s)
    send(db, s, task, "当前进度", key="progress")
    advance_conversations(db, s)
    send(db, s, task, "详细解释失败原因", key="detail")
    advance_conversations(db, s)
    detail, _ = run(db, s, {"effect":"observe", "reply":"本轮格式校正仍未通过；原请求和成果保留。"})
    assert detail.state == "succeeded", detail.error_message
    advance_conversations(db, s)
    send(db, s, task, "继续", key="continue")
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        receipt = w.latest(task, "agent_turn_resumed")
        assert receipt and receipt.payload["failed_job_id"] == job.id
        message = next(e for e in w.history(task) if e.id == receipt.payload["message_id"])
        assert request_text(w, task, message) == "只修改本 issue，保留其他测试要求"
    resumed, runner = run(db, s, {"effect":"command", "action":"discuss_plan", "reply":"按原 issue 范围修订方案。"})
    assert resumed.state == "succeeded", resumed.error_message
    assert "只修改本 issue" in runner.invocations[0].prompt
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert w.latest(task, "conversation_dispatched").payload["intent"] == "plan"
        assert not w.latest(task, "execution_authorized")


def test_pending_command_does_not_block_later_detail_or_progress(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        Workbench(session, s.artifact_root).enqueue(task, "test_background", {}, key="busy")
    send(db, s, task, "先准备新的方案")
    advance_conversations(db, s)
    job, _ = run(db, s, {"effect":"command", "action":"discuss_plan", "reply":"等待现有工作完成后准备。"})
    assert job.state == "succeeded", job.error_message
    advance_conversations(db, s)
    send(db, s, task, "为什么需要等待？", key="detail")
    send(db, s, task, "当前进度", key="progress")
    advance_conversations(db, s)
    with db.session() as session:
        assert "当前进度" not in [m.payload["text"] for m in blocking_messages(Workbench(session, s.artifact_root), task)]
    advance_conversations(db, s)
    detail, _ = run(db, s, {"effect":"observe", "reply":"当前已有作业，方案修改会依次处理。"})
    assert detail.state == "succeeded", detail.error_message
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert len(blocking_messages(w, task)) == 1
        assert not w.latest(task, "conversation_dispatched")


def test_stale_command_reobserves_within_original_call_budget(tmp_path):
    db, s, task, *_ = runtime_fixture(tmp_path)
    send(db, s, task, "讨论下一步方案")
    advance_conversations(db, s)
    first, _ = run(db, s, {"effect":"command", "action":"discuss_plan", "reply":"准备方案。"})
    assert first.state == "succeeded"
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        w.append(task, "stop_requested", {"reason":"state changed before dispatch"}, key="stale-stop")
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert w.latest(task, "agent_decision_superseded")
        assert not w.latest(task, "conversation_dispatched")
    second, _ = run(db, s, {"effect":"observe", "reply":"当前执行已被停止，需要确认后再继续。"})
    assert second.state == "succeeded", second.error_message
    assert second.payload["consumed_calls"] == 1
    advance_conversations(db, s)
    with db.session() as session:
        assert not blocking_messages(Workbench(session, s.artifact_root), task)


def test_clarification_answer_remains_bound_after_progress_interlude(tmp_path):
    from app.planner import PlanningService
    from tests.test_workbench import run_turn
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        PlanningService(session, s.artifact_root).request(task, text="规划", parent_id=None, expected_hash=None, key="questions")
    completed, _ = run_turn(db, s, json.dumps({"reply":"需要确认兼容范围", "questions":[
        {"id":"compat", "prompt":"保留接口吗？", "options":["保留", "允许变更"]}], "read_paths":[], "plan":None}))
    assert completed.state == "succeeded", completed.error_message
    with db.session() as session:
        source = Workbench(session, s.artifact_root).latest(task, "assistant_message")
        source_id, source_hash = source.id, source.record_hash
    send(db, s, task, "当前进度", key="progress")
    advance_conversations(db, s)
    send(db, s, task, "compat：保留", key="answer", question_source_id=source_id, question_source_hash=source_hash)
    advance_conversations(db, s)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        assert w.latest(task, "conversation_dispatched").payload["intent"] == "plan"
        assert not w.latest(task, "execution_authorized")


def test_direct_coding_source_read_has_durable_receipt_and_no_fabricated_turn(tmp_path, monkeypatch):
    from tests import test_workbench as fixtures
    original = fixtures.seeded
    def seed(path):
        result = original(path)
        with result[0].session() as session:
            activate(Workbench(session, result[1].artifact_root), result[2])
            session.commit()
        return result
    monkeypatch.setattr(fixtures, "seeded", seed)
    db, s, task, workers, code, review = fixtures.automatic_fixture(tmp_path)
    fixtures.populate_round(code, review)
    code.contents[0] = json.dumps({"summary":"", "operations":[], "read_paths":["app/plans.py"]})
    fixtures.drive_round(db, s, task, workers)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        receipt = w.latest(task, "agent_code_tool_result")
        assert receipt, [(j.kind,j.state,j.error_code) for j in w.jobs(task)]
        assert w.read(receipt.payload["artifact_id"])["entries"][0]["path"] == "app/plans.py"
        assert w.latest(task, "awaiting_acceptance")
        assert len(code.invocations) == 2
        assert "CONTRIBOS_CODING_TOOL_RESULT=" in code.invocations[1].prompt


def test_large_tool_evidence_is_paged_and_exact_original_remains_retrievable(tmp_path):
    from app.context_compaction import ContextRead, compact_prompt, read_context
    db, s, task, *_ = runtime_fixture(tmp_path)
    value = {"content":"原始证据" * 3000}
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        job = w.enqueue(task, "conversation_intent", {}, key="large-evidence")
        prompt, _ = compact_prompt(w, task, {"message":"逐段解释证据", "rules":["Preserve scope"],
            "tool_results":[{"reference":{"name":"repository_file"}, "data":value}]},
            scope="conversation", job_id=job.id)
        admitted, receipt = admit_prompt(w, task, prompt, {}, job_id=job.id)
        body = json.loads(admitted)
        assert receipt["admitted"] and body["tool_results"][0]["data"]["partial"]
        reference = body["tool_results"][0]["data"]["context_reference"]
        checkpoint = w.latest(task, "context_checkpoint").payload["artifact_id"]
        page = read_context(w, task, checkpoint, [ContextRead(reference_id=reference, offset=0, max_bytes=2000)])[0]
        assert page["partial"] and page["next_offset"]
        assert w.read(reference)["data"] == value


def test_exact_unchanged_baseline_failures_do_not_hide_new_failures():
    from app.execution_outcome import unchanged_baseline_failures, classify_verification
    failure = {"command_id":"unit", "command_hash":"a"*64, "exit_code":1,
        "status":"failed", "stdout_hash":"b"*64, "stderr_hash":"c"*64}
    baseline = {"inspection":{"command_evidence":[failure]}}
    assert unchanged_baseline_failures(baseline, {"command_evidence":[failure]})
    assert not unchanged_baseline_failures(baseline, {"command_evidence":[{**failure,"stdout_hash":"d"*64}]})
    assert not unchanged_baseline_failures(baseline, {"command_evidence":[{"exit_code":1}]})
    outcome = classify_verification({"command_evidence":[{**failure,
        "stderr_log":"ImportError: Start directory is not importable: 'tests'"}]})
    assert outcome["status"] == "environment_blocked"


def test_maintenance_receipts_are_visible_and_do_not_count_as_autonomy(tmp_path):
    from app.agent_runtime import mark_maintenance
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        receipt = w.append(task, "proposal_corrected", {"source":"local_correction"}, key="operator")
        mark_maintenance(w, task, receipt, reason="local_proposal_correction")
        mark_maintenance(w, task, receipt, reason="local_proposal_correction")
        result = observe(w, task)
        assert result["maintenance_count"] == 1 and not result["autonomous_eligible"]


@pytest.mark.parametrize("path", ["../secret.txt", "/tmp/requirements.txt", "a/../../b.txt", "a\\b.txt", "requirements.py", "", "a//b.txt"])
def test_dynamic_local_dependency_discovery_rejects_path_escape(path):
    from app.execution_preflight import python_dynamic_requirement_files
    with pytest.raises(ValueError):
        python_dynamic_requirement_files({"tool":{"setuptools":{"dynamic":{"dependencies":{"file":[path]}}}}},
            "python/pkg/pyproject.toml")


def test_dynamic_local_dependency_file_discovery_is_data_only():
    from app.execution_preflight import python_dynamic_requirement_files
    metadata = {"tool":{"setuptools":{"dynamic":{"dependencies":{"file":["requirements-dynamic.txt"]}}}}}
    assert python_dynamic_requirement_files(metadata, "python/pkg/pyproject.toml") == ["python/pkg/requirements-dynamic.txt"]
    assert python_dynamic_requirement_files({"tool":{"setuptools":{"dynamic":{"dependencies":{"attr":"untrusted.compute"}}}}},
        "python/pkg/pyproject.toml") == []


def test_runtime_normalizes_only_empty_read_presentation_and_clears_correction(tmp_path):
    from app.conversation_protocol import parse_step
    raw = {"effect":"observe", "action":"respond", "reads":[{"name":"progress"}],
           "reply":"读取中", "references":""}
    decision, normalized = parse_step(json.dumps(raw), version=RUNTIME_REASONING_VERSION,
        actions=["respond"], known=set(), allow_reads=True)
    assert normalized and decision.references == [] and decision.reply == ""
    with pytest.raises(ValueError):
        parse_step(json.dumps({**raw,"reads":[],"reply":"final"}), version=RUNTIME_REASONING_VERSION,
            actions=["respond"], known=set(), allow_reads=True)
    db, s, task, *_ = runtime_fixture(tmp_path)
    send(db, s, task, "读取源码并解释当前行为")
    advance_conversations(db, s)
    job, runner = run(db, s, "invalid", {"effect":"observe", "reads":[{"name":"repository_file","path":"app/plans.py"}]},
        {"effect":"observe", "reply":"当前读取到的内容是 content，尚无实施结果。", "references":["repository_file"]})
    assert job.state == "succeeded", job.error_message
    assert json.loads(runner.invocations[2].prompt)["correction"] == {}


def test_progress_label_describes_background_work_during_a_foreground_question(tmp_path):
    from app.conversation import user_status
    db, s, task, *_ = runtime_fixture(tmp_path)
    with db.session() as session:
        w = Workbench(session, s.artifact_root)
        w.enqueue(task, "coding_turn", {}, key="background-coding")
        w.enqueue(task, "conversation_intent", {}, key="foreground-question")
        status = user_status(w, task)
        assert status["label"] == "正在实现修改" and status["busy"] is True
        assert len(observe(w, task)["foreground"]) == len(observe(w, task)["background"]) == 1


def test_actual_final_observation_without_reads_preserves_answer_and_audit(tmp_path):
    db,s,task,*_=runtime_fixture(tmp_path)
    send(db,s,task,'解释已经保存的任务信息')
    advance_conversations(db,s)
    raw=json.dumps({'action':'respond','references':['task'],
        'reply':'当前仍是方案阶段，尚未开始实施。'})
    job,runner=run(db,s,{'effect':'observe','reads':[{'name':'task'}]},raw)
    assert job.state=='succeeded',job.error_message
    assert len(runner.invocations)==2
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        event=w.latest(task,'conversation_observation_normalized')
        assert w.read(event.payload['artifact_id'])['raw_output']==raw
        assert event.payload['workflow_dispatched'] is False
        assert not w.latest(task,'conversation_decision_rejected')
        assert w.latest(task,'conversation_result').payload['text']=='当前仍是方案阶段，尚未开始实施。'
        assert not w.latest(task,'execution_authorized')
        assert not w.latest(task,'publication_confirmed')


@pytest.mark.parametrize('bad',[
    {'action':'respond','reply':'','references':[]},
    {'action':'respond','reply':'done','reads':None,'references':[]},
    {'action':'respond','reply':'done','references':['review']},
    {'action':'discuss_plan','reply':'start','references':[]},
    {'reply':'start','references':[]},
])
def test_empty_read_default_cannot_infer_actions_answers_or_evidence(bad):
    from app.conversation_protocol import parse_step
    with pytest.raises(ValueError):
        parse_step(json.dumps(bad),version=RUNTIME_REASONING_VERSION,
            actions=['respond','discuss_plan'],known=set(),allow_reads=True)


def test_read_presentation_citations_are_ignored_only_before_tools(tmp_path):
    db,s,task,*_=runtime_fixture(tmp_path)
    send(db,s,task,'读取并解释任务事实');advance_conversations(db,s)
    job,_=run(db,s,{'effect':'observe','reads':[{'name':'task'}],
        'reply':'尚未核实的说明','references':['publication_preview']},
        {'reply':'当前任务仍在方案阶段。','references':['task']})
    assert job.state=='succeeded',job.error_message
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,'conversation_read_requested')
        assert not w.latest(task,'conversation_decision_rejected')
        result=w.latest(task,'conversation_result')
        assert [r['name'] for r in result.payload['references']]==['task']
        assert '尚未核实' not in result.payload['text']
        assert not w.latest(task,'publication_confirmed')


@pytest.mark.parametrize('text',['结果呢','有结果了吗？','完成了吗','做完了吗？'])
def test_bare_result_question_reads_facts_without_model_or_background_changes(tmp_path,text):
    db,s,task,*_=runtime_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        background=w.enqueue(task,'coding_turn',{},key='background-result-question')
        source=w.append(task,'conversation_result',{'version':'conversation-v1','text':'已有源码说明'},key='focus-result')
        record_focus(w,task,source,[{'name':'repository_file','source_hash':'a'*64}])
        session.commit();before=observe(w,task)
    send(db,s,task,text);advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);after=observe(w,task)
        assert before['focus']==after['focus'] and before['revision']==after['revision']
        assert [j.id for j in w.jobs(task)]==[background.id]
        assert w.latest(task,'conversation_finished').payload['status']=='completed'
        assert not w.latest(task,'execution_authorized')


def test_failed_planning_status_is_not_reported_as_running_and_new_success_supersedes_it(tmp_path):
    from app.conversation import user_status
    db,s,task,*_=runtime_fixture(tmp_path)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);jobs=JobService(session)
        failed=w.enqueue(task,'planning_turn',{},key='failed-status')
        leased=jobs.lease_next(worker_id='status-test',kinds=('planning_turn',));jobs.start(leased.id,worker_id='status-test')
        jobs.fail(failed.id,worker_id='status-test',error_code='model_gateway_upstream_failure',error_message='safe failure')
    send(db,s,task,'结果呢');advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);status=user_status(w,task)
        assert not status['busy'] and '没有后台任务' in status['label']
        assert '上次模型服务请求失败' in w.latest(task,'conversation_result').payload['text']
        assert len(w.jobs(task))==1
        fresh=w.enqueue(task,'planning_turn',{},key='new-success-status');jobs=JobService(session)
        leased=jobs.lease_next(worker_id='status-test',kinds=('planning_turn',));jobs.start(leased.id,worker_id='status-test')
        jobs.succeed(fresh.id,worker_id='status-test')
        assert '失败' not in user_status(w,task)['next_step']


def test_compound_result_request_does_not_bypass_requested_work():
    from app.conversation import progress_only
    assert not progress_only('结果呢，继续修改代码')
    assert not progress_only('测试结果呢，请解释这个失败')
