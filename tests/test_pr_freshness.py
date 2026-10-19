"""Live PR queries cannot be fulfilled from yesterday's conversation snapshot."""
import asyncio
import json
from dataclasses import replace

import pytest

from app.conversation import advance_conversations, conversation_view, receive_message
from app.pr_freshness import current_status_requested
from app.pr_lifecycle import FOLLOWUP, request_lifecycle
from app.workbench import Workbench, WorkbenchError
from tests.test_conversation_reasoning import run as reason
from tests.test_pr_lifecycle import lifecycle, flow, published  # noqa: F401


@pytest.mark.parametrize('text',[
    '根据下现在pr情况','查看pr现在情况','现在 PR 合并了吗？','跟进一下 PR',
    '看看 PR 的最新状态','check the current PR status','查询当前 CI 结果',
])
def test_unambiguous_current_queries_request_remote_reads(text):
    assert current_status_requested(text)


@pytest.mark.parametrize('text',[
    '查看上次保存的 PR 状态','不要刷新 PR 状态','当前 PR 链接是什么？',
    '给我看当前审核预览','请修改 PR 标题','检查没问题，请完成 ready for review',
])
def test_history_artifacts_and_write_requests_keep_their_own_routes(text):
    assert not current_status_requested(text)


def old_observation(f):
    db,s,task,account,t,worker=f
    t.prs[0]['draft']=False
    with db.session() as session:
        request_lifecycle(Workbench(session,s.artifact_root),task,kind=FOLLOWUP,account_hash=account,key='old-observation')
    assert asyncio.run(worker.run_once()).state=='succeeded'
    with db.session() as session:
        return Workbench(session,s.artifact_root).latest(task,'pr_followup_observed')


@pytest.mark.parametrize('text',['根据下现在pr情况','现在 PR 合并了吗？','check the latest PR status'])
def test_current_query_refreshes_after_message_without_model_and_synchronizes_merge(lifecycle,text):
    db,s,task,account,t,worker=lifecycle
    s=replace(s,implementation_provider='nvidia_nim')
    old=old_observation(lifecycle)
    t.prs[0].update(state='closed',merged=True,merged_at='2026-10-18T08:00:00Z',
                   closed_at='2026-10-18T08:00:00Z',merge_commit_sha='c'*40)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);before=[e.record_hash for e in w.history(task)]
        received=receive_message(w,task,text=text,key='current',settings=s)
        assert receive_message(w,task,text=text,key='current',settings=s)==received
    for _ in range(3):advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert not any(j.kind=='conversation_intent' for j in w.jobs(task))
        assert len([j for j in w.jobs(task) if j.kind==FOLLOWUP])==2
    assert asyncio.run(worker.run_once()).state=='succeeded'
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);fresh=w.latest(task,'pr_followup_observed')
        message=next(e for e in w.history(task) if e.id==received['message_id'])
        assert fresh.sequence>message.sequence and fresh.record_hash!=old.record_hash
        assert fresh.payload['stage']=='merged' and fresh.payload['pr']['state']=='closed'
        assert fresh.payload['pr']['merged_at']=='2026-10-18T08:00:00Z'
        assert fresh.payload['pr']['merge_commit_sha']=='c'*40
        assert [e.record_hash for e in w.history(task)[:len(before)]]==before
        view=conversation_view(w,task)
        assert view['user_state']['label']=='PR 已合并' and view['pending_action'] is None
        assert view['pending_count']==0 and view['messages'][-1]['receipt']['status']=='completed'
        assert 'PR 已合并' in view['results'][-1]['text']
    assert t.posts==0 and t.ready_calls==0


def test_cached_model_answer_is_replaced_with_live_read_even_without_keyword_shortcut(lifecycle):
    db,s,task,account,t,worker=lifecycle
    s=replace(s,implementation_provider='nvidia_nim')
    old_observation(lifecycle)
    t.prs[0].update(state='closed',merged=True)
    text='这条贡献后来怎么样了'
    assert not current_status_requested(text)
    with db.session() as session:receive_message(Workbench(session,s.artifact_root),task,text=text,key='ambiguous',settings=s)
    advance_conversations(db,s)
    stale='当前仍未合并，请等待维护者审核。'
    job,runner=reason(db,s,{'reply':stale,'references':['pull_request']})
    assert job.state=='succeeded'
    evidence=json.loads(runner.invocations[0].prompt)['tool_results'][0]['data']
    assert evidence['live'] is False and evidence['source']=='saved_observation'
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,'conversation_refresh_requested').payload['cached_reply_displayed'] is False
        assert all(stale not in r['text'] for r in conversation_view(w,task)['results'])
    assert asyncio.run(worker.run_once()).state=='succeeded'
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert 'PR 已合并' in conversation_view(w,task)['results'][-1]['text']
        assert len([e for e in w.history(task) if e.kind=='model_call'])==1


@pytest.mark.parametrize('text',['上次保存的 PR 状态是什么','不要刷新PR，只解释保存的状态'])
def test_explicit_history_can_be_read_without_refreshing_the_remote(lifecycle,text):
    db,s,task,account,t,worker=lifecycle
    s=replace(s,implementation_provider='nvidia_nim')
    old=old_observation(lifecycle)
    with db.session() as session:
        receive_message(Workbench(session,s.artifact_root),task,text=text,key='saved',settings=s)
    advance_conversations(db,s)
    job,_=reason(db,s,{'reply':'这是上次核对时保存的状态。','references':['pull_request']})
    assert job.state=='succeeded'
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        assert w.latest(task,'pr_followup_observed').record_hash==old.record_hash
        assert len([j for j in w.jobs(task) if j.kind==FOLLOWUP])==1


def test_remote_failure_keeps_last_observation_and_does_not_claim_current_success(lifecycle):
    db,s,task,account,t,worker=lifecycle
    old=old_observation(lifecycle)
    async def unavailable(*args,**kwargs):
        raise WorkbenchError('GitHub 当前不可读取',code='github_unavailable')
    t.api=unavailable
    with db.session() as session:receive_message(Workbench(session,s.artifact_root),task,text='查看现在 PR 情况',key='unavailable',settings=s)
    advance_conversations(db,s)
    assert asyncio.run(worker.run_once()).state=='failed'
    advance_conversations(db,s)
    with db.session() as session:
        w=Workbench(session,s.artifact_root);view=conversation_view(w,task)
        assert w.latest(task,'pr_followup_observed').record_hash==old.record_hash
        assert view['messages'][-1]['receipt']['status']=='failed'
        assert '未完成' in view['results'][-1]['text']


def test_confirmed_latest_preview_never_resurrects_an_older_unconsumed_preview(lifecycle):
    db,s,task,account,t,worker=lifecycle
    with db.session() as session:
        w=Workbench(session,s.artifact_root)
        w.append(task,'conversation_started',{'version':'conversation-v1'},key='started')
        first=w.append(task,'pr_ready_prepared',{'target':{'pr_url':'https://github.com/fixture/repo/pull/7'}},key='old-ready')
        latest=w.append(task,'pr_ready_prepared',{'target':first.payload['target']},key='new-ready')
        w.append(task,'pr_ready_confirmed',{'preview_hash':latest.record_hash},key='confirmed-ready')
        assert conversation_view(w,task)['pending_action'] is None
