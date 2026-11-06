"""Offline exported evidence only: synthetic DB/authority, deterministic model/tools.
Run with PYTHONPATH=. .venv/bin/python <this file>. Never touches live tasks.
"""
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from pytest import MonkeyPatch

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import prompt_for
from app.provenance import canonical_json, content_hash
from app.security import ensure_no_sensitive_data
from app.workbench import Workbench
from tests.capacity_fixture import FrozenGateway
from tests.capacity_projection import reconstruction
from tests.test_native_read_batches import setup, drain, saved_response as plan12_response, SAVED as PLAN12
from tests.test_read_capacity import saved_response as plan14_response, SAVED_PATH
from tests.test_missing_source_observations import fixture as missing_fixture, saved_response as plan13_response, SAVED as PLAN13

ROOT = Path(__file__).resolve().parent


def save(name, value):
    ensure_no_sensitive_data(value, context='offline capacity export')
    path = ROOT / name
    path.parent.mkdir(exist_ok=True, parents=True)
    with path.open('w') as out: out.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def artifact(value):
    if isinstance(value, bytes):
        aid = hashlib.sha256(value).hexdigest()
        path = ROOT / 'artifacts' / (aid + '.txt')
        if not path.exists(): path.write_bytes(value)
        return aid
    aid = content_hash(value)
    path = ROOT / 'artifacts' / (aid + '.json')
    encoded = canonical_json(value).encode()
    ensure_no_sensitive_data(value, context='content-addressed offline artifact')
    if path.exists():
        assert path.read_bytes() == encoded
    else:
        path.parent.mkdir(exist_ok=True, parents=True); path.write_bytes(encoded)
    return aid


def snapshot(values, label, response_artifact):
    asyncio.run(values[4].run_once()); drain(values[5])
    with values[0].session() as s:
        j = AgentJournal(Workbench(s, values[1].artifact_root), values[2], values[3])
        cp = j.checkpoint(); assert cp.state == 'ready'
        events = j.events()
        results = [e for e in events if e.kind == 'agent_v2_tool_result']
        receipts = [j.wb.read(e.payload['artifact_id']) for e in results]
        prompt = prompt_for(j)
        assert [r['call_id'] for r in prompt['feedback']] == [r['call_id'] for r in receipts]
        charges = [e for e in events if e.kind == 'agent_v2_output_charge']
        model_bytes = sum(len(j.wb.artifacts.read_bytes(e.payload['artifact_id'])) for e in events if e.kind == 'agent_v2_model_response')
        assert cp.usage['output_bytes'] == model_bytes + sum(e.payload['bytes'] for e in charges)
        report = {'label': label, 'classification': 'saved_provider_envelope_offline_persistent_scheduler_deterministic_tools_synthetic_authority',
            'provider_requests': 0, 'original_response_artifact': response_artifact,
            'historical_approval_reused': False, 'terminal_old_session_resumed': False,
            'child_sizes': [len(e.payload['call_ids']) for e in events if e.kind == 'agent_v2_read_sub_batch_completed'],
            'usage': cp.usage, 'limits': j.spec().limits.model_dump(), 'current_checkpoint': cp.model_dump(),
            'model_visible_statuses': [r['status'] for r in prompt['feedback']], 'prompt_artifact': artifact(prompt),
            'original_ID_mapping': [dict(e.payload) for e in results], 'receipts': receipts,
            'complete_batch_does_not_finish_Coding': True,
            'model_envelope_bytes': model_bytes, 'output_charges': [dict(e.payload) for e in charges],
            'billing_conservation_verified': True,
            'events': [{'kind': e.kind, 'sequence': e.sequence, 'hash': e.record_hash, 'payload': e.payload} for e in events]}
        assert cp.usage['model_calls'] == 1
        for e in events:
            aid = e.payload.get('artifact_id')
            if aid:
                raw = j.wb.artifacts.read_bytes(aid)
                if e.payload.get('role') == 'source_page':
                    p = ROOT / 'artifacts' / (aid + '.txt')
                    if not p.exists():
                        ensure_no_sensitive_data(raw.decode(), context='issued raw source page')
                        p.parent.mkdir(exist_ok=True, parents=True); p.write_bytes(raw)
                else:
                    artifact(json.loads(raw))
        return report, receipts


def main():
    with tempfile.TemporaryDirectory(prefix='offline-capacity-') as temp:
        base = Path(temp); replays = []
        for i in range(3):
            values = setup(base / ('plan12-' + str(i)), plan12_response(i), gateway=FrozenGateway())
            r, _ = snapshot(values, 'Plan12-' + str(i + 1), PLAN12['batches'][i]['response_artifact'])
            assert r['child_sizes'] == [4, 2 if i == 0 else 1]
            replays.append(r)
        with MonkeyPatch.context() as patch:
            values = missing_fixture(base / 'plan13', patch, plan13_response())
            r, _ = snapshot(values, 'Plan13', PLAN13['response_artifact'])
            assert r['model_visible_statuses'] == ['succeeded'] * 7 + ['not_found']
            replays.append(r)
        values = setup(base / 'plan14', plan14_response(), gateway=FrozenGateway())
        r, receipts = snapshot(values, 'Plan14', SAVED_PATH.stem)
        assert r['child_sizes'] == [4, 4]
        replays.append(r)
        coverage, prompt, rendered_artifacts = reconstruction(receipts, output_delta=r['usage']['output_bytes'] - r['model_envelope_bytes'])
        for value in rendered_artifacts.values(): artifact(value)
        coverage['complete_next_native_prompt_artifact'] = artifact(prompt)
        coverage['original_provider_call_ID_mapping'] = r['original_ID_mapping']
        coverage['report_hash'] = content_hash(coverage)
        save('model-visible-source-coverage.json', coverage)
        value = {'classification': 'offline_saved_envelopes_deterministic_tools', 'new_paid_model_requests': 0,
            'source_classification': 'Plan12/14 exact frozen source UTF8; Plan13 deterministic absence fixture (separate real OCI report)',
            'replays': replays}
        value['report_hash'] = content_hash(value); save('offline-replay.json', value)
        print(json.dumps({'replays': [x['label'] for x in replays], 'plan14_usage': r['usage'],
            'inline_bytes': coverage['inline_source_bytes'], 'artifact_recovery_rounds': coverage['focused_artifact_recovery_rounds_total']}))


if __name__ == '__main__': main()
