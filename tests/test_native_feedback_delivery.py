"""Every result in a native batch must reach the next bounded observation."""
import asyncio
import copy
import pytest

from app.agent_v2.contracts import ToolResult
from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.agent_v2.native_tools import native_input_bound, project_native_prompt
from app.provenance import canonical_json, content_hash
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_prompt_projection import task_input_prompt
from tests.test_native_protocol_repairs import fixture


def receipt(i, text, *, offset=0, total=None):
    size = len(text.encode())
    total = total if total is not None else offset + size
    return ToolResult(call_id='batch-' + str(i), input_hash='a' * 64,
        source_hash='b' * 64, output_source_hash='b' * 64, status='succeeded',
        data={'tool': 'read_file', 'path': f'src/file{i}.ts', 'content_hash': 'c' * 64,
            'execution_backend': 'docker', 'offset': offset, 'text': text,
            'next_offset': offset + size if offset + size < total else None,
            'total_bytes': total}).model_dump()


def test_all_four_batch_results_are_observed_including_first_requested_file(tmp_path):
    db, settings, task, sid, *_ = fixture(tmp_path, response('list_paths', {}))
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        journal = AgentJournal(wb, task, sid)
        old = wb.artifact(receipt(99, 'old receipt'))
        journal.append('tool_result', {'call_id': 'old', 'artifact_id': old, 'name': 'read_file'}, key='old')
        # The immutable turn identifies the batch even after a protocol rejection.
        journal.append('turn', {'decision': {'reply': '', 'outcome': 'tools',
            'calls': [{'call_id': f'batch-{i}', 'name': 'read_file',
                'arguments': {'path': f'src/file{i}.ts'}} for i in range(4)]},
            'usage': {'input_tokens': 1, 'output_tokens': 1}}, key='batch')
        refs = []
        for i in range(4):
            result = receipt(i, f'body-{i} ' * 100)
            aid = wb.artifact(result)
            refs.append(aid)
            journal.append('tool_result', {'call_id': result['call_id'], 'artifact_id': aid,
                'name': 'read_file'}, key=f'result-{i}')
        journal.append('model_rejected', {'failure': {'safe_reason_code': 'agent_native_call_count_invalid'}}, key='rejection')
        prompt = prompt_for(journal)
        assert [r['data']['path'] for r in prompt['feedback']] == [f'src/file{i}.ts' for i in range(4)]
        assert [r['receipt_artifact'] for r in prompt['feedback']] == refs
        assert all(f'body-{i}' in r['data']['text'] for i, r in enumerate(prompt['feedback']))
        assert native_input_bound(prompt) <= 32000


def test_full_task_and_four_unicode_pages_share_the_existing_cap_without_lost_offsets():
    prompt, spec, inputs = task_input_prompt('complete task requirement ' * 340)
    raw = [receipt(i, ('page ' + str(i) + ' 中文🙂 ') * 500, offset=123, total=30000) for i in range(4)]
    prompt['feedback'] = [{**r, 'receipt_artifact': content_hash(r)} for r in raw]
    before = canonical_json(prompt)
    projected = project_native_prompt(prompt, spec, task_input=inputs)
    assert projected['approved_task_input'] == inputs
    assert len(projected['feedback']) == 4 and native_input_bound(projected) <= 32000
    for original, result in zip(raw, projected['feedback'], strict=True):
        data = result['data']
        assert data['text'] and original['data']['text'].startswith(data['text'])
        assert data['next_offset'] == 123 + len(data['text'].encode())
        assert data['total_bytes'] == 30000 and data['content_hash'] == 'c' * 64
        assert result['receipt_artifact'] == content_hash(original)
        assert result['inline_projection']['partial'] is True
        assert result['inline_projection']['original_next_offset'] == original['data']['next_offset']
    assert canonical_json(prompt) == before


def test_non_page_failure_keeps_failure_and_recoverable_exact_evidence():
    prompt, spec, inputs = task_input_prompt('requirement ' * 700)
    result = receipt(0, '')
    result.update(status='failed', failure={'category': 'code', 'safe_reason_code': 'assertion_failed',
        'retryable': True, 'remaining_budget': {'checks': 5}, 'next_allowed_action': 'inspect_failure',
        'blocking_scope': 'session'})
    result['data'] = {'tool': 'run_approved_checks', 'outcome': 'failed',
        'checks': [{'name': 'all-approved', 'diagnostic': 'x' * 25000}]}
    aid = content_hash(result)
    prompt['feedback'] = [{**result, 'receipt_artifact': aid}]
    projected = project_native_prompt(prompt, spec, task_input=inputs)
    feedback = projected['feedback'][0]
    assert projected['approved_task_input'] == inputs and native_input_bound(projected) <= 32000
    assert feedback['failure'] == result['failure'] and feedback['status'] == 'failed'
    assert feedback['data'] == {'tool': 'run_approved_checks', 'outcome': 'failed'}
    page = feedback['evidence_page']
    original = canonical_json(result).encode()
    assert original.startswith(page['text'].encode())
    assert page['artifact_id'] == aid and page['next_offset'] == len(page['text'].encode())
    assert page['total_bytes'] == len(original) and page['partial'] is True


def test_native_index_preserves_actual_read_ranges_and_source_revisions(tmp_path):
    db, settings, task, sid, *_ = fixture(tmp_path, response('list_paths', {}))
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        journal = AgentJournal(wb, task, sid)
        result = receipt(0, 'exact page', offset=57, total=100)
        aid = wb.artifact(result)
        journal.append('tool_result', {'artifact_id': aid, 'call_id': result['call_id'], 'name': 'read_file'}, key='range')
        row = prompt_for(journal)['history_index'][0]
        assert row['offset'] == 57 and row['next_offset'] == 67 and row['total_bytes'] == 100
        assert row['source_hash'] == result['source_hash'] and row['output_source_hash'] == result['output_source_hash']
        assert row['artifact_id'] == aid


def test_real_worker_batch_survives_restart_without_reexecution_or_dropped_results(tmp_path):
    # Deterministic worker integration, not a live model/delivery claim.
    batch = response('list_paths', {})
    call = batch['choices'][0]['message']['tool_calls'][0]
    batch['choices'][0]['message']['tool_calls'] = [{**copy.deepcopy(call), 'id': str(i)} for i in range(4)]
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, batch, response('list_paths', {}))
    assert asyncio.run(model.run_once()).state == 'succeeded'
    for _ in range(4):
        assert asyncio.run(tool.run_once()).state == 'succeeded'
    from app.agent_v2.loop import AgentModelWorker
    resumed = AgentModelWorker(db, settings, model.adapter_factory, worker_id='native-feedback-restart')
    assert asyncio.run(resumed.run_once()).state == 'succeeded'
    assert len(gateway.calls) == 4
    assert len(runner.prompts[-1]['feedback']) == 4
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        journal = AgentJournal(wb, task, sid)
        assert journal.checkpoint().usage['reads'] == 4 and journal.checkpoint().usage['model_calls'] == 2
        results = [e.payload['artifact_id'] for e in journal.events() if e.kind == 'agent_v2_tool_result']
        assert [r['receipt_artifact'] for r in runner.prompts[-1]['feedback']] == results
        assert all(wb.read(aid)['status'] == 'succeeded' for aid in results)


def test_feedback_hash_mismatch_fails_closed():
    prompt, spec, inputs = task_input_prompt('task')
    prompt['feedback'] = [{**receipt(0, 'source'), 'receipt_artifact': 'f' * 64}]
    with pytest.raises(ValueError, match='native_feedback_reference_mismatch'):
        project_native_prompt(prompt, spec, task_input=inputs)


def test_complete_page_retains_terminal_offset_and_failure_free_receipt():
    prompt, spec, inputs = task_input_prompt('task')
    result = receipt(0, 'last 中文🙂 page', offset=50)
    prompt['feedback'] = [{**result, 'receipt_artifact': content_hash(result)}]
    projected = project_native_prompt(prompt, spec, task_input=inputs)
    assert projected['feedback'] == prompt['feedback']
    assert projected['feedback'][0]['data']['next_offset'] is None
    assert 'inline_projection' not in projected['feedback'][0]
