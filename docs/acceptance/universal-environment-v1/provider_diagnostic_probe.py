"""Explicitly approved read-only model diagnostic; never resumes native work."""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import httpx
from app.agent_v2.adapter import StructuredModelAdapter
from app.agent_v2.provider_failures import provider_failure_receipt
from app.config import Settings
from app.database import Database
from app.model_settings import ModelSettingsService
from app.providers.contracts import ProviderRunError
from app.providers.studio import runner_for_profile, profile_identity
from app.provenance import canonical_json, content_hash
from app.security import ensure_no_sensitive_data


async def main() -> None:
    records=Path(__file__).resolve().parent
    preview=json.loads((records/'provider-readonly-diagnostic-proposal.json').read_text())
    approval=json.loads((records/'provider-readonly-diagnostic-approval.json').read_text())
    assert approval['approved_proposal_hash']==preview['proposal_hash']
    assert approval['decision']=='approved' and approval['actor']=='human'
    assert content_hash({k:v for k,v in preview.items() if k!='proposal_hash'})==preview['proposal_hash']
    assert content_hash({k:v for k,v in approval.items() if k!='approval_hash'})==approval['approval_hash']
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==preview['probe_script_sha256']
    out=records/'provider-readonly-diagnostic.json'
    assert not out.exists(), 'Reconcile an existing probe; never repeat it automatically'
    for name,digest in preview['tested_production_files'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest()==digest
    root=Path('/tmp/upeve-isolated-20261029-v2')
    with sqlite3.connect('file:'+str(root/'business/state.db')+'?mode=ro',uri=True) as db:
        before=dict(db.execute('SELECT id,record_hash FROM workbench_events WHERE task_id=?',(preview['task_id'],)))
        assert before=={e['id']:e['record_hash'] for e in preview['native_journal']}
        assert db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','leased','running')").fetchone()[0]==0
        row=db.execute('SELECT storage_key FROM artifacts WHERE id=?',(preview['original_prompt_artifact'],)).fetchone()
        path=(root/'business/artifacts'/row[0]).resolve()
        assert path.is_relative_to((root/'business/artifacts').resolve())
        raw=path.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==preview['original_prompt_artifact']
        prompt=json.loads(raw)
        assert len(canonical_json(prompt).encode())<=preview['max_input_prompt_bytes']
    database=Database('sqlite:///'+str(root/'business/state.db'))
    with database.session() as session:
        profile=ModelSettingsService(session).profile(preview['profile_id'])
        assert profile['record_hash']==preview['profile_hash']
    database.close()
    settings=Settings(model_gateway_base_url='http://127.0.0.1:18091/v1',
        model_gateway_signing_key=bytes.fromhex((root/'gateway-signing-key').read_text().strip()))
    result=dict(version='upeve-readonly-provider-diagnostic-v1',recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        classification='controlled_live_readonly_diagnostic_not_native_execution_or_success',proposal_hash=preview['proposal_hash'],
        approval_hash=approval['approval_hash'],profile_id=preview['profile_id'],profile_hash=preview['profile_hash'],
        original_prompt_artifact=preview['original_prompt_artifact'],native_runtime_state_changed=False,
        native_checkpoint_or_budget_changed=False,tool_calls_executed=0,third_party_writes=0,
        status='single_request_about_to_be_issued',max_completion_tokens=4096,max_model_requests=1)
    ensure_no_sensitive_data(result,context='before controlled diagnostic request')
    with out.open('x') as file:file.write(json.dumps(result,indent=2)+'\n')
    async with httpx.AsyncClient(timeout=315,trust_env=False) as client:
        runner=runner_for_profile(settings,profile,client=client,paired_analysis=False)
        assert runner.broker is not None
        runner.broker.max_requests=1
        adapter=StructuredModelAdapter(runner,profile_identity(profile),preview['attempt_id'])
        try:
            decision,usage=await adapter.decide(prompt,request_id=preview['diagnostic_request_id'],max_output_tokens=4096)
            bounded=usage['input_tokens']<=preview['original_input_reservation'] and usage['output_tokens']<=4096
            result.update(status='valid_structured_decision_observed_NOT_executed' if bounded else 'provider_usage_exceeds_diagnostic_bound',reported_usage=usage,
                decision_outcome=decision.outcome,proposed_tool_names=[c.name for c in decision.calls],
                result_does_not_reconstruct_old_response=True)
        except ProviderRunError as exc:
            result.update(status='provider_failure_observed',safe_reason_code=exc.code,
                receipt=provider_failure_receipt(exc,input_bound=preview['original_input_reservation'],output_limit=4096))
        except Exception as exc:
            result.update(status='diagnostic_terminal_failure',safe_exception_type=type(exc).__name__)
    with sqlite3.connect('file:'+str(root/'business/state.db')+'?mode=ro',uri=True) as db:
        assert dict(db.execute('SELECT id,record_hash FROM workbench_events WHERE task_id=?',(preview['task_id'],)))==before
    ensure_no_sensitive_data(result,context='controlled live diagnostic result')
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    asyncio.run(main())
