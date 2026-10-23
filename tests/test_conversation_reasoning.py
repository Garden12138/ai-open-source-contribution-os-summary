"""Conversation reasoning reads evidence, preserves context and calls safe tools."""
import asyncio
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.conversation import ConversationIntentWorker, advance_conversations, receive_message, conversation_view
from app.conversation_reasoning import REASONING_VERSION, EvidenceRead, read_evidence
from app.jobs import JobService
from app.models import PublishIntent
from app.workbench import Workbench
from tests.test_nvidia_workflows import ScriptedRunner, _identity
from tests.test_workbench import seeded


def request(db, settings, task, text, key="one"):
    with db.session() as session:
        receipt = receive_message(Workbench(session, settings.artifact_root), task, text=text, key=key, settings=settings)
    advance_conversations(db, settings)
    return receipt


def run(db, settings, *responses):
    runner = ScriptedRunner(*(json.dumps(r if 'intent' in r else {'reply':'','action':'respond','reads':[],'references':[],**r})
                             if isinstance(r, dict) else r for r in responses))
    worker = ConversationIntentWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="reasoning-test")
    job = asyncio.run(worker.run_once())
    return job, runner


def preview_fixture(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        preview = wb.append(task, "publication_prepared", {
            "title": "Fix directory docstring typo", "body": "## Summary\nCorrect will crete to will create.\n\n## Testing\n8 passed.",
            "repository": "fixture/repo", "fork": "contributor/repo", "branch": "contribution-branch",
            "expires_at": "2000-01-01T00:00:00+00:00", "nonce": "a"*64}, key="preview")
        wb.append(task, "conversation_result", {"version":"conversation-v1",
            "text":"英文文案和发布预览已准备好，可以查看。"}, key="prepared-reply")
    return db, settings, task, preview


@pytest.mark.parametrize('has_ready_preview',[False,True])
def test_post_publication_context_uses_current_pr_or_ready_preview_never_creation_preview(tmp_path,has_ready_preview):
    db,settings,task,old=preview_fixture(tmp_path)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        published=wb.append(task,'publication_completed',{'html_url':'https://github.com/fixture/repo/pull/7','number':7},key='published')
        if has_ready_preview:
            ready=wb.append(task,'pr_ready_prepared',{
                'target':{'pr_url':published.payload['html_url']},
                'before':{'head_sha':'b'*40,'title':'Current PR title','body':'Current public body','draft':True},
                'login':'contributor','actions':['mark_pull_request_ready_for_review'],
                'expires_at':'2099-01-01T00:00:00+00:00','nonce':'c'*64},key='ready-preview')
    request(db,settings,task,'给我看当前审核预览' if has_ready_preview else '当前 PR 链接是什么？')
    name='ready_preview' if has_ready_preview else 'pull_request'
    job,runner=run(db,settings,{'reply':'这是当前审核预览。' if has_ready_preview else '这是已创建的 PR 链接。',
        'action':'respond','reads':[],'references':[name]})
    assert job.state=='succeeded',job.error_message
    prompt=json.loads(runner.invocations[0].prompt)
    assert prompt['tool_results'][0]['reference']['name']==name
    assert 'publication_preview' not in prompt['reference_names_available_now']
    data=prompt['tool_results'][0]['data']
    if has_ready_preview:
        assert data['before']['title']=='Current PR title'
        assert data['target']['pr_url']==published.payload['html_url']
        assert 'nonce' not in data and 'acknowledgement' not in data
    else:
        assert data['pr_url']==published.payload['html_url'] and data['observation'] is None
    advance_conversations(db,settings)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert wb.latest(task,'conversation_result').payload['references'][0]['source_hash']==(ready.record_hash if has_ready_preview else published.record_hash)
        assert all(j.kind=='conversation_intent' for j in wb.jobs(task))


def test_actual_two_requests_resolve_prepared_preview_and_assistant_history_without_actions(tmp_path):
    db, settings, task, preview = preview_fixture(tmp_path)
    for index, text in enumerate(["发给我看下，我检查一下发布预览", "把你准备好的发我看下"]):
        request(db, settings, task, text, str(index))
        response = {"reply":"这是已准备好的发布预览，正文如下。预览已过期，但内容仍可查看，尚未发布。",
                    "action":"respond", "references":["publication_preview"]}
        job, runner = run(db, settings, response)
        assert job.state == "succeeded", job.error_message
        prompt = json.loads(runner.invocations[0].prompt)
        assert prompt["message"] == text
        result = prompt["tool_results"][0]
        assert result["data"]["title"] == preview.payload["title"]
        assert result["data"]["body"] == preview.payload["body"]
        assert result["data"]["expired"] is True
        assert "prepare_ready_for_review" not in prompt["observation"]["available_actions"]
        if index:
            assert any(t["role"] == "assistant" and "正文如下" in t["text"] for t in prompt["recent_dialogue"])
        advance_conversations(db, settings)
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            answer = wb.latest(task, "conversation_result").payload
            assert answer["text"] == response["reply"]
            assert answer["references"][0]["source_hash"] == preview.record_hash
            assert not any(j.kind != "conversation_intent" for j in wb.jobs(task))
            assert session.scalar(select(PublishIntent)) is None
            assert not wb.latest(task, "execution_authorized")
            assert not wb.latest(task, "publication_confirmed")


def test_read_tool_result_is_persisted_and_used_in_next_model_call(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "app/plans.py 原文是什么？")
    job, runner = run(db, settings,
        {"reads":[{"name":"repository_file", "path":"app/plans.py", "query":"content"}]},
        {"reply":"当前缓存的 app/plans.py 原文片段是 content。", "references":["repository_file"]})
    assert job.state == "succeeded", job.error_message
    prompt = json.loads(runner.invocations[1].prompt)
    assert prompt["remaining_calls"] == 1
    assert prompt["tool_results"][0]["data"]["content"] == "content"
    advance_conversations(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        read = wb.latest(task, "conversation_tool_result")
        assert wb.read(read.payload["artifact_id"])["data"]["partial"] is True
        assert len([e for e in wb.history(task) if e.kind == "model_call"]) == 2
        assert conversation_view(wb, task)["pending_count"] == 0


def test_compound_answer_and_workflow_use_original_request_and_model_reply(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    text = "解释目前的范围，再把空字符串也考虑进去，先给我方案"
    request(db, settings, task, text)
    reply = "目前还在方案阶段。我会把空字符串边界加入方案，完成后供你检查。"
    job, _ = run(db, settings, {"reply":reply, "action":"discuss_plan"})
    assert job.state == "succeeded", job.error_message
    advance_conversations(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert next(j for j in wb.jobs(task) if j.kind == "planning_turn").payload["text"] == text
        assert wb.latest(task, "conversation_result").payload["text"] == reply
        assert not wb.latest(task, "execution_authorized")


@pytest.mark.parametrize("action", ["publish", "execute", "sign_cla", "prepare_ready_for_review"])
def test_unknown_or_unavailable_tools_cannot_dispatch_or_authorize(tmp_path, action):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "看一下现有结果")
    bad = {"reply":"发起操作。", "action":action}
    job, _ = run(db, settings, bad, bad)
    assert job.state == "failed"
    advance_conversations(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert not wb.latest(task, "conversation_decision")
        assert not any(j.kind != "conversation_intent" for j in wb.jobs(task))
        assert session.scalar(select(PublishIntent)) is None


def test_read_scope_and_call_budget_cannot_be_expanded_by_model(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "解释一下文件")
    read = {"reads":[{"name":"repository_file", "path":"/etc/passwd"}]}
    job, runner = run(db, settings, read, read)
    assert job.state == "failed" and len(runner.invocations) == 2
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        result = wb.read(wb.latest(task, "conversation_tool_result").payload["artifact_id"])
        assert result["data"]["available"] is False
        assert not wb.latest(task, "conversation_decision")


def test_stopped_authorization_cannot_dispatch_a_saved_decision(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "按这个要求完善方案")
    job, _ = run(db, settings, {"reply":"我会完善方案。", "action":"discuss_plan"})
    assert job.state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        wb.append(task, "stop_requested", {"job_ids":[]}, key="stop-between-decision-and-dispatch")
    advance_conversations(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert not any(j.kind == "planning_turn" for j in wb.jobs(task))
        assert wb.latest(task, "conversation_finished").payload["status"] == "failed"


def test_cancellation_during_model_call_cannot_persist_decision(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "先解释一下")
    runner = ScriptedRunner(json.dumps({"reply":"解释当前证据。"}))
    original = runner.complete
    async def cancel(invocation):
        with db.session() as session:
            wb = Workbench(session, settings.artifact_root)
            JobService(session).request_cancel(wb.jobs(task)[0].id)
        return await original(invocation)
    runner.complete = cancel
    worker = ConversationIntentWorker(db, settings, runner, _identity(settings.implementation_model), worker_id="cancel-test")
    assert asyncio.run(worker.run_once()).state == "cancelled"
    with db.session() as session:
        assert not Workbench(session, settings.artifact_root).latest(task, "conversation_decision")


def test_legacy_queued_classifier_keeps_its_original_protocol(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        receipt = receive_message(wb, task, text="旧队列问题", key="legacy", settings=settings)
        job = wb.enqueue(task, "conversation_intent", {"message_id":receipt["message_id"], "text":"旧队列问题"}, key="legacy-job")
    result, _ = run(db, settings, {"intent":"question"})
    assert result.id == job.id and result.state == "succeeded"
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.latest(task, "conversation_intent").payload["intent"] == "question"
        assert not wb.latest(task, "conversation_decision")


def test_sensitive_model_reply_is_not_saved_or_included_in_correction_prompt(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "看一下当前结果")
    canary = "ghp_CONVERSATIONREASONINGCANARY1234567890"
    job, runner = run(db, settings, {"reply":canary}, {"reply":"尚无实施结果。"})
    assert job.state == "succeeded"
    assert canary not in runner.invocations[-1].prompt
    with db.session() as session:
        raw = "\n".join(session.connection().connection.driver_connection.iterdump())
        assert canary not in raw


def test_truncated_json_never_becomes_a_workflow_request(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "先给我看看")
    truncated = '{"reply":"开始处理", "action":"discuss_plan"'
    job, _ = run(db, settings, truncated, truncated)
    assert job.state == "failed"
    with db.session() as session:
        assert not Workbench(session, settings.artifact_root).latest(task, "conversation_decision")


def test_new_question_after_stop_is_answered_without_resuming_work(tmp_path):
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "停止", "stop")
    request(db, settings, task, "当前已经保存了哪些成果？", "question")
    job, _ = run(db, settings, {"reply":"已保存读取到的代码，目前没有实施结果。"})
    assert job.state == "succeeded"
    advance_conversations(db, settings)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert conversation_view(wb, task)["paused"]
        assert "没有实施结果" in wb.latest(task, "conversation_result").payload["text"]
        assert not any(j.kind != "conversation_intent" for j in wb.jobs(task))


def test_restart_after_read_reuses_receipt_and_remaining_call_budget(tmp_path, monkeypatch):
    from app import conversation_reasoning as reasoning
    db, settings, task, *_ = seeded(tmp_path)
    request(db, settings, task, "读取 app/plans.py 并解释")
    original = reasoning.reasoning_prompt
    def crash_after_read(wb, task_id, message_id, reads, correction, remaining_calls, **kwargs):
        if reads:
            raise OSError('injected restart before the second model call')
        return original(wb, task_id, message_id, reads, correction, remaining_calls, **kwargs)
    monkeypatch.setattr(reasoning, 'reasoning_prompt', crash_after_read)
    failed, _ = run(db, settings, {"reads":[{"name":"repository_file", "path":"app/plans.py"}]})
    assert failed.state == 'failed'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        artifact = wb.latest(task, 'conversation_tool_result').payload['artifact_id']
        JobService(session).retry(failed.id)
    monkeypatch.setattr(reasoning, 'reasoning_prompt', original)
    resumed, runner = run(db, settings, {"reply":"读取到的缓存内容为 content。", "references":["repository_file"]})
    assert resumed.id == failed.id and resumed.state == 'succeeded'
    assert json.loads(runner.invocations[0].prompt)['remaining_calls'] == 1
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.latest(task, 'conversation_tool_result').payload['artifact_id'] == artifact
        assert len([e for e in wb.history(task) if e.kind == 'model_call']) == 2
        assert len([e for e in wb.history(task) if e.kind == 'conversation_tool_result']) == 1


def test_invalid_reference_shape_retains_diagnostic_and_corrects_within_same_two_calls(tmp_path):
    db, settings, task, _ = preview_fixture(tmp_path)
    request(db, settings, task, '展示刚才的预览')
    bad={'reply':'已有预览。','action':'respond','reads':[],
         'references':[{'name':'publication_preview'}]}
    good={'reply':'这是已有预览。','action':'respond','reads':[], 'references':['publication_preview']}
    job, runner=run(db,settings,json.dumps(bad),good)
    assert job.state=='succeeded'
    correction=json.loads(runner.invocations[-1].prompt)['correction']
    assert correction['diagnostic']['errors'][0]['loc']==['references',0]
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        receipt=wb.read(wb.latest(task,'conversation_decision_rejected').payload['artifact_id'])
        assert json.loads(receipt['raw_output'])==bad and receipt['applied'] is False
        assert len([e for e in wb.history(task) if e.kind=='model_call'])==2


def approved_preview_fixture(tmp_path):
    from tests.test_publication_workbench import ready
    from app.publisher_accounts import append_account
    from app.execution_acceptance import accept_execution, acceptance_view
    db,settings,task,transport,publisher=ready(tmp_path)
    settings=replace(settings,implementation_provider='nvidia_nim')
    assert asyncio.run(publisher.run_once()).state=='succeeded'
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        account=append_account(session,{'state':'connected','login':'contributor','user_id':123,'scopes':['public_repo']},correlation_id='account')
        session.commit()
        accept_execution(wb,task,binding_hash=acceptance_view(wb,task)['binding_hash'],text='确认结果',key='accept')
        old=wb.latest(task,'publication_prepared')
        preview=wb.append(task,'publication_prepared',{**old.payload,'publisher_account_hash':account.event_hash,
            'expires_at':'2000-01-01T00:00:00+00:00'},key='expired-preview')
    return db,settings,task,preview,transport,publisher


@pytest.mark.parametrize('final_action',['respond','prepare_publication'])
def test_expiry_never_regenerates_a_preview_when_preparation_can_reuse_it(tmp_path,final_action):
    db,settings,task,preview,transport,_=approved_preview_fixture(tmp_path)
    with db.session() as session:
        before=[j.id for j in Workbench(session,settings.artifact_root).jobs(task)]
    request(db,settings,task,'把你准备好的发我看下')
    proposed={'action':'prepare_publication','reply':'这是已准备的预览，仍可查看。','reads':[],'references':['publication_preview']}
    final={**proposed,'action':final_action}
    job,runner=run(db,settings,proposed,final)
    assert job.state=='succeeded' and len(runner.invocations)==2
    critique=json.loads(runner.invocations[-1].prompt)['correction']
    assert critique['proposed_action']=='prepare_publication'
    advance_conversations(db,settings)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert wb.latest(task,'publication_prepared').record_hash==preview.record_hash
        assert [j.kind for j in wb.jobs(task) if j.id not in before]==['conversation_intent']
        assert not wb.latest(task,'publication_document_ready')
        assert not wb.latest(task,'publication_confirmed')
        assert wb.latest(task,'conversation_result').payload['references'][0]['source_hash']==preview.record_hash
        if final_action=='prepare_publication': assert wb.latest(task,'conversation_tool_reused')


@pytest.mark.parametrize('text', ['请更新已过期的预览，保持已有标题正文不变', '创建草稿 PR'])
def test_explicit_renewal_reuses_document_and_runs_only_read_only_preview_preparation(tmp_path, text):
    db,settings,task,preview,transport,publisher=approved_preview_fixture(tmp_path)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        document=wb.append(task,'publication_document_ready',{
            'title':preview.payload['title'],'body':preview.payload['body'],
            'review_hash':preview.payload['review_hash'],'requirements_hash':preview.payload['requirements_hash']},key='existing-document')
        before={j.id for j in wb.jobs(task)}
    request(db,settings,task,text)
    decision={'action':'renew_publication_preview','reply':'我会沿用现有文案更新预览，完成后供你确认。',
              'reads':[],'references':['publication_preview']}
    job,_=run(db,settings,decision,decision)
    assert job.state=='succeeded'
    advance_conversations(db,settings)
    assert asyncio.run(publisher.run_once()).state=='succeeded'
    advance_conversations(db,settings)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert [j.kind for j in wb.jobs(task) if j.id not in before]==['conversation_intent','publication_prepare']
        assert wb.latest(task,'publication_document_ready').record_hash==document.record_hash
        assert wb.latest(task,'publication_prepared').record_hash!=preview.record_hash
        assert wb.latest(task,'publication_prepared').payload['title']==preview.payload['title']
        assert wb.latest(task,'publication_prepared').payload['body']==preview.payload['body']
        assert not wb.latest(task,'publication_confirmed')
        assert session.scalar(select(PublishIntent)) is None
    assert all(method=='GET' for method,_,_ in transport.calls)


def test_unread_account_reference_has_actionable_diagnostic_and_corrects_without_dispatch(tmp_path):
    db,settings,task,preview=preview_fixture(tmp_path)
    request(db,settings,task,'创建草稿 PR')
    bad={'reply':'预览已过期，账号已连接。','action':'respond','reads':[],
         'references':['publication_preview','publisher_account']}
    good={**bad,'references':['publication_preview']}
    job,runner=run(db,settings,bad,good)
    assert job.state=='succeeded'
    correction=json.loads(runner.invocations[1].prompt)['correction']
    assert correction['diagnostic']['reference_names_available_now']==['publication_preview']
    assert json.loads(runner.invocations[0].prompt)['reference_names_available_now']==['publication_preview']
    advance_conversations(db,settings)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert not wb.latest(task,'publication_confirmed')
        assert wb.latest(task,'publication_prepared').record_hash==preview.record_hash


def test_unknown_reasoning_version_does_not_fall_back_to_legacy_classifier(tmp_path):
    db,settings,task,*_=seeded(tmp_path)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        receipt=receive_message(wb,task,text='解释一下',key='future',settings=settings)
        wb.enqueue(task,'conversation_intent',{'message_id':receipt['message_id'],'text':'解释一下',
            'reasoning_version':'unsupported-version'},key='unknown-version')
    job,runner=run(db,settings)
    assert job.state=='failed' and job.error_code=='conversation_version'
    assert not runner.invocations
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert not wb.latest(task,'conversation_intent') and not wb.latest(task,'conversation_decision')
