"""Large status graphs are retrievable evidence; frozen intent and facts stay exact."""
from copy import deepcopy
import json

import pytest

from app.context_compaction import ContextRead, compact_prompt, read_context
from app.environment_engine.resolver import resolve_contract
from app.provenance import canonical_json, content_hash
from app.security import SensitiveDataError
from tests.test_context_compaction import packet
from tests.test_environment_engine_resolution import binding, evidence
from tests.test_workbench import seeded
from app.planner import PlanningService


def graph():
    files = {'package.json': json.dumps({'scripts': {'check': 'tsc --noEmit'}}),
             **{f'packages/p{i}/package.json': json.dumps({'name': f'fixture-{i}'}) for i in range(28)}}
    commands = [{'command_id': f'check-{i}', 'argv': ['pnpm', 'run', 'check'],
                 'working_directory': '.'} for i in range(4)]
    value = resolve_contract(binding(commands), commands, {'ecosystem': 'node',
        'project_directory': '.', 'manifests': sorted(files), 'node_environment': {'package_roots': ['.']}},
        evidence(files))
    assert value.admission == 'static_admitted'
    return value.model_dump(mode='json')


def context_packet(wb, task, content):
    contract = graph()
    value = packet(wb, task)
    value.update(rules=['FROZEN_RULE_' + 'r' * 46000], current_plan=content.hash_payload(),
        context_reads=[{'text': 'ACTUAL_RETURNED_SOURCE', 'source_complete': False, 'partial': True}],
        execution_status={'environment': {'status': 'supported', 'verification_contract': contract,
            'verification_contract_hash': content_hash(contract)},
            'preflight': {'status': 'ready', 'report_hash': 'e' * 64,
                'environment': {'verification_contract': contract, 'verification_contract_hash': content_hash(contract)}},
            'environment_engine': {'stages': {'preparation': 'unverified', 'baseline': 'unverified'},
                'contract_hash': content_hash(contract), 'commands': contract['commands']},
            'baseline': {'outcome': 'environment_failure', 'result_hash': 'f' * 64}})
    return value, contract


def test_paged_graph_projection_fits_without_changing_plan_current_reads_or_readiness(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload, contract = context_packet(wb, task, content)
        before = canonical_json(payload)
        history = [(e.id, e.record_hash) for e in wb.history(task)]
        prompt, cp = compact_prompt(wb, task, payload, scope='planning', page_source_files=True,
            target_bytes=48000, trigger_bytes=48000)
        assert cp['original_bytes'] > 96000 and len(prompt.encode()) <= 96000
        actual = json.loads(prompt)
        for key in ('rules', 'request', 'current_plan', 'context_reads'):
            assert actual[key] == payload[key]
        status = actual['execution_status']
        assert status['preflight']['status'] == 'ready'
        assert status['environment_engine']['stages'] == {'preparation': 'unverified', 'baseline': 'unverified'}
        assert status['baseline'] == payload['execution_status']['baseline']
        refs = wb.read(cp['artifact_id'])['references']
        contract_refs = [r for r in refs if r['label'] == 'execution verification contract']
        assert len(contract_refs) == 1
        summary = status['environment']['verification_contract']
        assert summary['reference_id'] == status['preflight']['environment']['verification_contract']['reference_id']
        assert summary['contract_hash'] == content_hash(contract) and summary['partial'] is True
        assert summary['admission'] == 'static_admitted'
        assert [(n['command_id'], n['argv'], n['working_directory']) for n in status['environment_engine']['commands']] == [
            (n['command_id'], n['argv'], n['working_directory']) for n in contract['commands']]
        assert canonical_json(payload) == before
        assert [(e.id, e.record_hash) for e in wb.history(task)[:len(history)]] == history
        assert wb.latest(task, 'execution_authorized') is None
    db.close()


def test_indexed_contract_and_full_command_graph_roundtrip_original_bytes_after_restart(tmp_path):
    from app.database import Database
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload, contract = context_packet(wb, task, content)
        prompt, cp = compact_prompt(wb, task, payload, scope='planning', page_source_files=True,
            target_bytes=48000, trigger_bytes=48000)
        refs = wb.read(cp['artifact_id'])['references']
        selected = [(r['reference_id'], contract if r['label'] == 'execution verification contract' else contract['commands'])
            for r in refs if r['label'] in {'execution verification contract', 'execution verification command graph'}]
        assert len(selected) == 2
        session.commit()
    db.close()
    db = Database(settings.database_url)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        for ref, original in selected:
            parts, offset = [], 0
            while True:
                result = read_context(wb, task, cp['artifact_id'], [ContextRead(reference_id=ref,
                    offset=offset, max_bytes=16000)])[0]
                parts.append(result['text'])
                if result['next_offset'] is None:
                    break
                offset = result['next_offset']
            assert ''.join(parts) == canonical_json(original)
    db.close()


def test_legacy_status_graph_and_current_plan_are_not_projected(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload, _ = context_packet(wb, task, content)
        prompt, _ = compact_prompt(wb, task, payload, scope='planning', page_source_files=False,
            target_bytes=48000, trigger_bytes=48000)
        assert json.loads(prompt)['execution_status'] == payload['execution_status']
        assert json.loads(prompt)['current_plan'] == payload['current_plan']
    db.close()


def test_graph_indexing_does_not_hide_canaries_or_relax_irreducible_pinned_limits(tmp_path):
    db, settings, task, content, _ = seeded(tmp_path)
    with db.session() as session:
        wb = PlanningService(session, settings.artifact_root)
        payload, _ = context_packet(wb, task, content)
        poisoned = deepcopy(payload)
        poisoned['execution_status']['environment']['verification_contract']['private'] = 'ghp_graphcanary12345678901234567890123456789'
        old = [(e.id, e.record_hash) for e in wb.history(task)]
        with pytest.raises(SensitiveDataError):
            compact_prompt(wb, task, poisoned, scope='planning', page_source_files=True)
        assert [(e.id, e.record_hash) for e in wb.history(task)] == old
        payload['rules'] = ['PINNED_' + 'r' * 97000]
        prompt, _ = compact_prompt(wb, task, payload, scope='planning', page_source_files=True,
            target_bytes=48000, trigger_bytes=48000)
        assert len(prompt.encode()) > 96000 and json.loads(prompt)['rules'] == payload['rules']
    db.close()
