"""Background path indexes must not prevent delivery of current source pages."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.context_compaction import ContextRead, compact_prompt, read_context
from app.database import Database
from app.planner import PlanningService
from app.planning_delivery import fit_context_pages
from app.provenance import canonical_json
from tests.test_context_compaction import packet
from tests.test_workbench import seeded


def inventory_packet(wb, task, content, *, pinned_bytes):
    value = packet(wb, task)
    entries = [{'path': f'source/captured_module_{i:03}.py', 'content': f'VALUE = {i}\n',
        'prior_hash': hashlib.sha256(f'VALUE = {i}\n'.encode()).hexdigest(), 'executable': False} for i in range(20)]
    inventory = [e['path'] for e in entries] + [f'source/unread_module_{i:03}.py' for i in range(150)]
    value.update(rules=['FROZEN_RULE_' + 'x' * pinned_bytes], current_plan=content.hash_payload(),
        evidence={'entries': entries, 'inventory': inventory},
        context_reads=[{'text': 'ACTUAL_SOURCE_源🙂' * 200, 'offset': 0, 'next_offset': None,
            'source_complete': True, 'partial': False} for _ in range(4)],
        context_read_history=[{'text': 'PREVIOUS_ACTUAL_READ', 'source_complete': False}])
    return value


def test_pressure_indexes_only_unread_inventory_and_preserves_all_frozen_inputs(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload = inventory_packet(wb, task, content, pinned_bytes=84000)
        before = canonical_json(payload)
        focused = {'source/unread_module_149.py'}
        def preview(pages):
            return compact_prompt(wb, task, {**payload, 'context_reads': pages}, scope='planning',
                page_source_files=True, focus_paths=focused, record_checkpoint=False)[0]
        delivered, receipt = fit_context_pages(payload['context_reads'], preview)
        assert receipt['limited'] and receipt['original_prompt_bytes'] > 96000 >= receipt['prompt_bytes']
        actual = json.loads(preview(delivered));evidence = actual['evidence']
        assert evidence['inventory_partial'] is True
        assert evidence['inventory_count'] == len(payload['evidence']['inventory'])
        assert set(evidence['inventory']) == {e['path'] for e in evidence['entries']} | focused
        for key in ('rules', 'request', 'current_plan'):
            assert actual[key] == payload[key]
        assert actual['context_reads'] == delivered
        # Existing history text may be indexed by the established paging rule;
        # its source-completeness fact must not change.
        assert actual['context_read_history'][0]['source_complete'] is False
        assert canonical_json(payload) == before and wb.latest(task, 'execution_authorized') is None
    db.close()


def test_pressure_inventory_roundtrips_and_finds_unread_paths_after_restart(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload = inventory_packet(wb, task, content, pinned_bytes=84000)
        payload['context_reads'] = []
        # Preserve pressure while avoiding any dependency on page-size fitting.
        payload['rules'][0] += 'y' * 8000
        prompt, cp = compact_prompt(wb, task, payload, scope='planning', page_source_files=True)
        ref = json.loads(prompt)['evidence']['inventory_reference']
        original = payload['evidence']['inventory']
        session.commit()
    db.close();db = Database(settings.database_url)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        parts, offset = [], 0
        while True:
            page = read_context(wb, task, cp['artifact_id'], [ContextRead(reference_id=ref, offset=offset, max_bytes=1024)])[0]
            parts.append(page['text'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        assert json.loads(''.join(parts)) == original
        match = read_context(wb, task, cp['artifact_id'], [ContextRead(reference_id=ref, query='unread_module_149')])[0]
        assert 'source/unread_module_149.py' in match['text']
    db.close()


@pytest.mark.parametrize('paged', [False, True])
def test_hints_are_shared_only_for_v2_and_small_inventory_is_not_needlessly_indexed(tmp_path, paged):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload = inventory_packet(wb, task, content, pinned_bytes=0)
        # A previous projection can already contain a tiny indexed marker.
        entry = payload['evidence']['entries'][0]
        entry.update(content='[Source indexed]', skeleton=True, partial=True,
            context_reference='a' * 64, read_hint='Search this reference using query, then read exact source.')
        original = deepcopy(payload)
        prompt, _ = compact_prompt(wb, task, payload, scope='planning', page_source_files=paged)
        actual = json.loads(prompt)
        assert actual['evidence']['inventory'] == original['evidence']['inventory']
        assert 'inventory_partial' not in actual['evidence']
        assert ('read_hint' in actual['evidence']['entries'][0]) == (not paged)
        assert 'read_context' in actual['context_management']['read_rule']
        assert payload == original
    db.close()
