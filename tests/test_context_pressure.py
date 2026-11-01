"""Pressure paging preserves original facts, human text and scoped retrieval."""
from copy import deepcopy
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.context_compaction import ContextRead, compact_prompt, read_context
from app.models import Artifact
from app.planner import PlanningService
from app.provenance import canonical_json
from app.security import SensitiveDataError
from tests.test_context_compaction import packet
from tests.test_workbench import seeded


def pressure_packet(wb, task, content):
    value = packet(wb, task)
    env = {'status': 'unsupported', 'ecosystem': 'node', 'reason': 'Missing declared tool',
        'declarations': [{'path': f'package-{i}/package.json', 'value': 'x' * 100} for i in range(20)]}
    value.update(rules=['PINNED_RULE_' + 'r' * 84000], current_plan=content.hash_payload(),
        context_reads=[{'text': 'EXACT_CURRENT_SOURCE', 'source_complete': False}],
        execution_status={'environment': env, 'preflight': {'status': 'blocked', 'environment': deepcopy(env)},
            'repository_snapshot': {'base_sha': 'a' * 40, 'archive_downloaded': True,
                'files': [{'path': f'source/{i}.py', 'status': 'read'} for i in range(100)]},
            'actions': {'can_execute': False}, 'runtime': {'environment_prepared': False},
            'verification': {'status': 'not_run', 'passed': 0, 'total': 0}},
        conversation=[{'role': 'user', 'content': {'text': 'Older exact request ' + 'q' * 900}} for _ in range(4)]
            + [{'role': 'assistant', 'content': {'reply': 'LATEST_REPLY'}}],
        context_read_history=[{'text': 'EXACT_HISTORICAL_TOOL_TEXT_' + 'x' * 8000, 'offset': 0, 'next_offset': 8000,
            'partial': True, 'source_complete': False, 'reference_id': 'b' * 64,
            'read_request': {'reference_id': 'b' * 64, 'query': 'literal selector', 'start_line': 20, 'max_lines': 80},
            'delivery': {'limited': True, 'delivered_bytes': 8000, 'continuation': {'offset': 8000, 'query': 'literal selector'}}}])
    return value


def test_pressure_pages_originals_without_promoting_readiness_or_touching_current_inputs(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload = pressure_packet(wb, task, content);before = canonical_json(payload)
        prompt, cp = compact_prompt(wb, task, payload, scope='planning', page_source_files=True,
            target_bytes=48000, trigger_bytes=48000)
        body = json.loads(prompt)
        assert len(prompt.encode()) <= 96000
        for key in ('rules', 'request', 'current_plan', 'context_reads'):
            assert body[key] == payload[key]
        status = body['execution_status']
        assert status['actions'] == {'can_execute': False}
        assert status['runtime'] == {'environment_prepared': False}
        assert status['verification'] == payload['execution_status']['verification']
        assert status['environment']['status'] == 'unsupported'
        assert status['preflight']['status'] == 'blocked'
        assert status['environment']['partial'] is True
        assert status['repository_snapshot']['files_indexed'] is True
        assert body['conversation'][-2:] == payload['conversation'][-2:]
        assert body['context_read_history'][0]['source_complete'] is False
        assert body['context_read_history'][0]['read_request'] == payload['context_read_history'][0]['read_request']
        refs = wb.read(cp['artifact_id'])['references']
        expected = {'execution environment': payload['execution_status']['environment'],
            'execution repository snapshot': payload['execution_status']['repository_snapshot'],
            'historical conversation': payload['conversation'][0], 'previous context read': payload['context_read_history'][0]}
        for label, original in expected.items():
            ref = next(x['reference_id'] for x in refs if x['label'] == label)
            parts, offset = [], 0
            while True:
                page = read_context(wb, task, cp['artifact_id'], [ContextRead(reference_id=ref, offset=offset, max_bytes=1024)])[0]
                parts.append(page['text'])
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
            assert json.loads(''.join(parts)) == original
        assert canonical_json(payload) == before and wb.latest(task, 'execution_authorized') is None
    db.close()


def test_repeated_complete_human_text_keeps_first_literal_and_every_source_position(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    same = 'Keep the declared scope and all checks. ' * 18
    different = same + 'Only the last instruction adds this sentence.'
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        events = [wb.append(task, 'user_message', {'text': text}, key=f'human-{i}', actor='local-user')
            for i, text in enumerate((same, same, different, same))]
        payload = pressure_packet(wb, task, content)
        prompt, cp = compact_prompt(wb, task, payload, scope='planning', page_source_files=True,
            target_bytes=48000, trigger_bytes=48000)
        rows = json.loads(prompt)['task_memory']['user_instructions']
        assert [x['event_id'] for x in rows] == [e.id for e in events]
        assert [x['source_hash'] for x in rows] == [e.record_hash for e in events]
        assert rows[0]['text'] == same and rows[2]['text'] == different
        for index in (1, 3):
            assert rows[index]['same_text_as_event_id'] == events[0].id and rows[index]['text_indexed']
            actual = read_context(wb, task, cp['artifact_id'], [ContextRead(reference_id=rows[index]['reference_id'])])[0]
            assert json.loads(actual['text']) == {'text': same}
        assert rows[1]['reference_id'] == rows[3]['reference_id']
    db.close()


@pytest.mark.parametrize('paged', [False, True])
def test_small_or_legacy_status_and_repository_instructions_stay_exact(tmp_path, paged):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root);payload = pressure_packet(wb, task, content)
        payload['rules'] = ['Exact policy'];payload['context_read_history'] = []
        text = 'REPOSITORY_RULES\n' * 150
        payload['evidence']['entries'] = [{'path': 'CONTRIBUTING.rst', 'content': text,
            'prior_hash': hashlib.sha256(text.encode()).hexdigest(), 'executable': False}]
        prompt, _ = compact_prompt(wb, task, payload, scope='planning', page_source_files=paged)
        body = json.loads(prompt)
        assert body['execution_status'] == payload['execution_status']
        assert body['conversation'] == payload['conversation']
        assert body['evidence']['entries'][0]['content'] == text
    db.close()


def test_pressure_cannot_hide_canaries_in_background_metadata(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root);payload = pressure_packet(wb, task, content)
        payload['execution_status']['environment']['secret'] = 'ghp_pressurecanary12345678901234567890123456789'
        count = session.scalar(select(func.count()).select_from(Artifact));history = len(wb.history(task))
        with pytest.raises(SensitiveDataError):
            compact_prompt(wb, task, payload, scope='planning', page_source_files=True)
        assert session.scalar(select(func.count()).select_from(Artifact)) == count
        assert len(wb.history(task)) == history
    db.close()
