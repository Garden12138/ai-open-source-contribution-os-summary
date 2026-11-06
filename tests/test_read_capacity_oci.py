"""Actual OCI reads of the saved Plan14 envelope; no provider or live authority."""
import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from app.agent_v2.contracts import ToolCall, ToolResult
from app.agent_v2.read_batches import parse_preflight
from app.agent_v2.read_capacity import certificate, size, validate_claim, observation_matches
from app.agent_v2.read_progress import ReadProgressNote
from app.agent_v2.tools import RestrictedToolRegistry
from app.provenance import canonical_json, content_hash
from tests.capacity_fixture import CONTENTS, SOURCE
from tests.test_missing_source_oci import frozen_gateway
from tests.test_read_capacity import SAVED_PATH

pytestmark = pytest.mark.skipif(os.getenv('CONTRIBOS_RUN_DOCKER_ACCEPTANCE') != '1',
    reason='Requires real OCI, frozen archive and fixed image; not deterministic/fake acceptance')


def test_real_oci_saved_plan14_strict_capacity_and_original_pages(tmp_path):
    spec, gateway, _ = frozen_gateway(tmp_path)
    raw = json.loads(SAVED_PATH.read_text()); assert content_hash(raw) == SAVED_PATH.stem
    original = raw['choices'][0]['tool_calls']
    calls = [ToolCall(call_id=c['id'], name=c['function']['name'], arguments=json.loads(c['function']['arguments'])) for c in original]
    async def scenario():
        registry = RestrictedToolRegistry()
        proof = parse_preflight(await gateway.preflight_read_batch(spec, calls,
            source_hash=spec.binding.archive_hash, snapshot=None, input_hash='a' * 64))
        assert proof['status'] == 'succeeded' and proof['source_hash'] == SOURCE
        assert proof['execution_backend'] == 'docker'
        batch = {'binding_hash': content_hash(spec.binding.model_dump()),
            'response_artifact': SAVED_PATH.stem, 'source_hash': SOURCE,
            'calls': [c.model_dump() for c in calls], 'sub_batches': [
                {'id': content_hash([SAVED_PATH.stem, i]), 'index': i // 4,
                 'call_ids': [c.call_id for c in calls[i:i+4]]} for i in (0, 4)]}
        remaining = 464474 - size(proof) - size(batch)
        cert = certificate(batch, proof, batch_artifact=content_hash(batch), proof_artifact=content_hash(proof), remaining=remaining)
        assert cert['status'] == 'succeeded' and cert['total_bytes_bound'] <= remaining
        results, raw_pages = [], {}
        for position, call in enumerate(calls):
            args = registry.validate(call, spec)
            validate_claim(proof['checked'][position], args.model_dump(), spec, SOURCE)
            value = await gateway.execute(spec, call, args, source_hash=SOURCE, snapshot=None,
                input_hash=content_hash([spec.binding.model_dump(), call.model_dump()]))
            result = ToolResult.model_validate(value['result'])
            assert result.status == 'succeeded' and result.call_id == call.call_id
            assert result.output_source_hash == SOURCE
            assert observation_matches(result, cert['calls'][position])
            data = result.data; page = data['text'].encode()
            assert page == CONTENTS[args.path].encode()[args.offset:args.offset + len(page)]
            raw_id = hashlib.sha256(page).hexdigest(); raw_pages[raw_id] = page
            note = ReadProgressNote(range_key='f' * 64, max_bytes=args.max_bytes, prior_same_range_reads=0, prior_receipt=None, warning_receipts_seen=[], visible_bytes=0, visible_next_offset=None)
            final = result.model_copy(update={'data': {**data, 'source_page_artifact': raw_id,
                'source_read_progress': note.model_dump()}}).model_dump()
            assert size(final) <= cert['calls'][position]['result_bytes_bound']
            assert size(final) + len(page) <= cert['calls'][position]['output_bytes_bound']
            results.append(final)
        assert [r['call_id'] for r in results] == [c['id'] for c in original]
        assert sum(len(r['data']['text'].encode()) for r in results) == 68338
        output = os.environ.get('CONTRIBOS_CAPACITY_OCI_EVIDENCE')
        if output:
            evidence = {'classification': 'saved_Plan14_envelope_real_frozen_archive_real_OCI_synthetic_authority',
                'provider_requests': 0, 'historical_approval_reused': False, 'new_Plan_created': False,
                'source_hash': SOURCE, 'archive_hash': spec.binding.archive_hash, 'runner_image': spec.binding.image,
                'original_response_artifact': SAVED_PATH.stem, 'calls': [c.model_dump() for c in calls],
                'child_sizes': [4, 4], 'proof': proof, 'certificate': cert, 'results': results,
                'result_progress_classification': 'bounded_platform_note_fixture; OCI page observations are real',
                'serialized_results_bytes': sum(size(r) for r in results), 'raw_source_artifact_bytes': sum(map(len,raw_pages.values())),
                'source_page_hashes': list(raw_pages), 'source_page_bytes': 68338, 'output_limit': spec.limits.output_bytes}
            evidence['report_hash'] = content_hash(evidence)
            from app.security import ensure_no_sensitive_data
            ensure_no_sensitive_data(evidence, context='capacity OCI evidence')
            with Path(output).open('x') as target: target.write(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    asyncio.run(scenario())
