"""Read allowance is visible before inference and enforced without extra tools."""
import json

import pytest

from app.conversation import advance_conversations
from app.conversation_protocol import RUNTIME_REASONING_VERSION, parse_step, step_schema, TurnProtocolError
from app.jobs import JobService
from app.workbench import Workbench
from tests.test_agent_runtime import runtime_fixture, send
from tests.test_conversation_reasoning import run


def read(*names):
    return {'effect': 'observe', 'reads': [{'name': name} for name in names]}


@pytest.mark.parametrize('slots', [0, 1, 2])
def test_schema_and_parser_enforce_current_read_slots(slots):
    schema = step_schema(['respond'], {'task'}, allow_reads=True,
        version=RUNTIME_REASONING_VERSION, read_limit=slots)
    assert schema['properties']['reads']['maxItems'] == slots
    reads = [{'name': name} for name in ('task', 'progress', 'history')[:slots + 1]]
    with pytest.raises(TurnProtocolError) as caught:
        parse_step(json.dumps({'effect': 'observe', 'action': 'respond', 'reply': '',
            'reads': reads, 'references': []}), version=RUNTIME_REASONING_VERSION,
            actions=['respond'], known={'task'}, allow_reads=True, read_limit=slots)
    assert caught.value.code == 'read_budget_exceeded'


def test_five_reads_then_oversized_batch_uses_existing_correction_without_reading(tmp_path):
    db, settings, task, *_ = runtime_fixture(tmp_path)
    send(db, settings, task, '请比较任务事实与仓库元数据后解释，缺少证据要说明。')
    advance_conversations(db, settings)
    job, runner = run(db, settings,
        read('task', 'history', 'environment'), read('progress', 'publisher_account'),
        {'effect': 'observe', 'reads': [{'name': 'repository_file', 'path': 'app/plans.py'}, {'name': 'plan'}]},
        {'effect': 'observe', 'reply': '现有任务事实已核对；缺少的源码证据尚未读取。', 'references': ['task']})
    assert job.state == 'succeeded', (job.error_code, job.error_message)
    assert len(runner.invocations) == 4
    third, fourth = [json.loads(i.prompt) for i in runner.invocations[2:]]
    assert third['remaining_reads'] == fourth['remaining_reads'] == 1
    assert runner.invocations[2].output_schema['properties']['reads']['maxItems'] == 1
    assert fourth['remaining_calls'] == 1
    assert fourth['correction']['diagnostic']['code'] == 'read_budget_exceeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        events = [e for e in wb.history(task) if e.job_id == job.id]
        reads = [e for e in events if e.kind == 'conversation_tool_result']
        assert len(reads) == 5
        assert not any(e.payload['tool']['name'] in {'repository_file', 'plan'} for e in reads)
        assert len([e for e in events if e.kind == 'conversation_decision_rejected']) == 1
        assert not wb.latest(task, 'execution_authorized')
        assert not wb.latest(task, 'publication_confirmed')


def test_six_reads_force_final_before_call_limit_without_expanding_allowance(tmp_path):
    db, settings, task, *_ = runtime_fixture(tmp_path)
    send(db, settings, task, '请比较任务事实与仓库元数据后解释，缺少证据要说明。')
    advance_conversations(db, settings)
    job, runner = run(db, settings, read('task', 'history', 'environment'),
        read('progress', 'publisher_account', 'plan'),
        {'effect': 'observe', 'reply': '现有事实已核对，尚无已保存方案。', 'references': ['task']})
    assert job.state == 'succeeded', (job.error_code, job.error_message)
    assert len(runner.invocations) == 3
    final = runner.invocations[-1]
    assert json.loads(final.prompt)['remaining_calls'] == 2
    assert json.loads(final.prompt)['remaining_reads'] == 0
    assert final.output_schema['properties']['reads']['maxItems'] == 0
    assert final.output_schema['properties']['reply']['minLength'] == 1
    with db.session() as session:
        events = Workbench(session, settings.artifact_root).history(task)
        assert len([e for e in events if e.kind == 'conversation_tool_result' and e.job_id == job.id]) == 6


def test_restart_restores_read_allowance_from_durable_receipts(tmp_path, monkeypatch):
    from app import conversation_reasoning as reasoning
    db, settings, task, *_ = runtime_fixture(tmp_path)
    send(db, settings, task, '请比较任务事实与仓库元数据后解释，缺少证据要说明。')
    advance_conversations(db, settings)
    original = reasoning.reasoning_prompt
    def crash(wb, task_id, message_id, reads, correction, remaining_calls, **kwargs):
        if kwargs.get('remaining_reads') == 1:
            raise OSError('injected restart before third call')
        return original(wb, task_id, message_id, reads, correction, remaining_calls, **kwargs)
    monkeypatch.setattr(reasoning, 'reasoning_prompt', crash)
    failed, first_runner = run(db, settings, read('task', 'history', 'environment'), read('progress', 'publisher_account'))
    assert failed.state == 'failed' and len(first_runner.invocations) == 2
    with db.session() as session:
        JobService(session).retry(failed.id)
    monkeypatch.setattr(reasoning, 'reasoning_prompt', original)
    done, resumed = run(db, settings, {'effect': 'observe', 'reply': '已读取的事实保留；未读取的源码不作结论。', 'references': ['task']})
    assert done.id == failed.id and done.state == 'succeeded'
    prompt = json.loads(resumed.invocations[0].prompt)
    assert prompt['remaining_reads'] == 1 and prompt['remaining_calls'] == 2
    with db.session() as session:
        events = Workbench(session, settings.artifact_root).history(task)
        assert len([e for e in events if e.kind == 'model_call' and e.job_id == done.id]) == 3
        assert len([e for e in events if e.kind == 'conversation_tool_result' and e.job_id == done.id]) == 5
