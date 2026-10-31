"""Long planning histories remain retrievable without crowding out new reads."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import func, select

from app.context_compaction import ContextRead, compact_prompt, read_context
from app.database import Database
from app.models import Artifact
from app.planner import PlanningService
from app.planning_budget import planning_usage
from app.planning_delivery import fit_context_pages
from app.provenance import canonical_json
from tests.test_context_compaction import packet
from tests.test_workbench import seeded


def discussion(wb, task, count):
    return [wb.append(task, 'assistant_message', {
        'reply': f'Unverified historical assertion {i}: 检查尚无证据。',
        'questions': [], 'plan_id': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}},
        key=f'discussion-{i}', actor='planner') for i in range(count)]


@pytest.mark.parametrize('paged,count,grouped', [(False, 40, False), (True, 22, False), (True, 23, True)])
def test_grouping_keeps_small_and_legacy_history_contracts(tmp_path, paged, count, grouped):
    db, settings, task, _, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        events = discussion(wb, task, count)
        prompt, cp = compact_prompt(wb, task, packet(wb, task), scope='planning', page_source_files=paged)
        items = json.loads(prompt)['task_memory']['older_discussion']
        assert sum(x.get('message_count', 1) for x in items) == count - 6
        assert len(items) == ((count - 6 + 15) // 16 if grouped else count - 6)
        if grouped:
            assert all(x['partial'] is True and x['message_count'] <= 16 for x in items)
            assert items[0]['from_sequence'] == events[0].sequence
            assert items[-1]['through_sequence'] == events[-7].sequence
        else:
            assert [x['source_hash'] for x in items] == [e.record_hash for e in events[:-6]]
        assert cp['instruction_count'] == 0 and wb.latest(task, 'execution_authorized') is None
    db.close()


def test_grouped_originals_roundtrip_after_restart_with_source_labels_and_no_invented_summary(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        events = discussion(wb, task, 40)
        wb.append(task, 'user_message', {'text': 'Keep all four approved checks.'}, key='human-scope', actor='local-user')
        expected = [{'event_id': e.id, 'sequence': e.sequence, 'source_hash': e.record_hash,
            'proposal': {k: e.payload[k] for k in ('reply', 'questions', 'plan_error', 'plan_id') if k in e.payload}}
            for e in events[:-6]]
        payload = packet(wb, task)
        payload.update(current_plan=content.hash_payload(), conversation=[{'role': 'assistant', 'text': 'CURRENT_DIALOGUE'}])
        usage = planning_usage(wb, task)
        history = [(e.id, e.record_hash) for e in wb.history(task)]
        prompt, cp = compact_prompt(wb, task, payload, scope='planning', page_source_files=True)
        body = json.loads(prompt)
        for key in ('rules', 'request', 'current_plan', 'conversation'):
            assert body[key] == payload[key]
        assert body['task_memory']['user_instructions'][0]['text'] == 'Keep all four approved checks.'
        groups = body['task_memory']['older_discussion']
        assert planning_usage(wb, task) == usage
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(history)]] == history
        session.commit()
    db.close()
    db = Database(settings.database_url)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        recovered = []
        for group in groups:
            chunks, offset = [], 0
            while True:
                page = read_context(wb, task, cp['artifact_id'], [ContextRead(reference_id=group['reference_id'],
                    offset=offset, max_bytes=257)])[0]
                chunks.append(page['text'])
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
            recovered.extend(json.loads(''.join(chunks)))
        assert recovered == expected
        assert wb.latest(task, 'execution_authorized') is None
    db.close()


def test_history_groups_leave_room_for_four_new_pages_without_changing_fixed_inputs(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        discussion(wb, task, 40)
        payload = packet(wb, task)
        payload.update(rules=['FROZEN_RULE_' + 'r' * 82000], current_plan=content.hash_payload(),
            context_reads=[{'text': 'NEW_SOURCE_源🙂' * 300, 'offset': 0, 'partial': False,
                'source_complete': True, 'next_offset': None} for _ in range(4)])
        before = deepcopy(payload)
        artifacts = session.scalar(select(func.count()).select_from(Artifact))
        history = [(e.id, e.record_hash) for e in wb.history(task)]
        def preview(pages):
            return compact_prompt(wb, task, {**payload, 'context_reads': pages}, scope='planning',
                page_source_files=True, record_checkpoint=False)[0]
        delivered, receipt = fit_context_pages(payload['context_reads'], preview)
        assert receipt['limited'] and receipt['original_prompt_bytes'] > 96000 >= receipt['prompt_bytes']
        assert session.scalar(select(func.count()).select_from(Artifact)) == artifacts
        assert [(e.id, e.record_hash) for e in wb.history(task)] == history
        body = json.loads(preview(delivered))
        assert body['context_reads'] == delivered
        assert body['rules'] == payload['rules'] and body['current_plan'] == payload['current_plan']
        assert all(page['partial'] and not page['source_complete'] for page in delivered)
        assert canonical_json(payload) == canonical_json(before)
        prompt, cp = compact_prompt(wb, task, {**payload, 'context_reads': delivered}, scope='planning', page_source_files=True)
        assert prompt == preview(delivered) and cp['prompt_bytes'] == receipt['prompt_bytes']
        assert wb.latest(task, 'execution_authorized') is None
    db.close()
