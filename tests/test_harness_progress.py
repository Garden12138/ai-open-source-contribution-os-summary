"""Harness regressions using deterministic tools; no real acceptance claim."""
import asyncio
import hashlib

from app.agent_v2.journal import AgentJournal
from app.agent_v2.loop import AgentToolWorker
from app.workbench import Workbench
from tests.test_agent_native_tools import response
from tests.test_native_read_progress import setup, run_round, results


def progress(db, settings, task, sid):
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        rows = [e for e in j.events() if e.kind == 'agent_v2_progress']
        assert rows, 'Persistent implementation progress is missing'
        return rows[-1].payload['progress'], rows


def test_exact_duplicate_is_reused_without_second_gateway_read(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {})
    run_round(model, tool)
    run_round(model, tool)
    cp, receipts = results(db, settings, task, sid)
    assert len(gateway.calls) == 1
    assert receipts[1]['data']['reused_receipt']
    assert receipts[0]['call_id'] != receipts[1]['call_id']
    assert cp.usage['reads'] == 2  # Requests remain charged; reuse grants no extra budget.
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['no_progress_streak'] == 1


def test_legal_pagination_records_new_evidence_and_does_not_trigger_guard(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {'offset': 4096})
    run_round(model, tool)
    run_round(model, tool)
    assert len(gateway.calls) == 2
    ledger, rows = progress(db, settings, task, sid)
    assert ledger['no_progress_streak'] == 0
    assert ledger['source_hash'] == results(db, settings, task, sid)[0].source_hash
    assert ledger['revision'] == len(rows)


def test_no_progress_corrects_once_then_blocks_without_using_remaining_budget(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {}, {}, {})
    for _ in range(3):
        run_round(model, tool)
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['no_progress_streak'] == 2 and ledger['correction_issued']
    run_round(model, tool)
    cp, receipts = results(db, settings, task, sid)
    assert cp.state == 'blocked' and cp.usage['model_calls'] == 4
    assert len(gateway.calls) == 1
    delivered = runner.prompts[-1]['implementation_progress']
    assert delivered['correction_issued'] and delivered['next_action'] == 'use_existing_evidence_or_implement'
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['blocker'] == 'agent_no_progress'
    assert asyncio.run(model.run_once()) is None


def test_checkpoint_restart_recovers_ledger_and_reuses_completed_read(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = setup(tmp_path, {}, {})
    run_round(model, tool)
    first, rows = progress(db, settings, task, sid)
    restarted = AgentToolWorker(db, settings, gateway, worker_id='harness-restarted-tool')
    run_round(model, restarted)
    latest, rows = progress(db, settings, task, sid)
    assert latest['revision'] > first['revision'] and len(gateway.calls) == 1
    assert len({e.record_hash for e in rows}) == len(rows)


def test_changed_source_after_patch_does_not_reuse_previous_read(tmp_path):
    # Fixture only: exercises worker source-version invalidation, not Docker proof.
    from tests.test_native_protocol_repairs import fixture
    from tests.test_agent_v2_loop import FixtureGateway
    values = fixture(tmp_path, response('read_file', {'path': 'calc.py'}),
        response('apply_patch', {'path': 'calc.py', 'expected_hash': hashlib.sha256(b'content').hexdigest(),
            'old_text': 'content', 'new_text': 'changed'}), response('read_file', {'path': 'calc.py'}))
    db, settings, task, sid, model, tool, runner, gateway = values
    with db.session() as session:
        path = AgentJournal(Workbench(session, settings.artifact_root), task, sid).spec().write_paths[0]
    for value in runner.responses:
        call = value['choices'][0]['message']['tool_calls'][0]
        import json
        args = json.loads(call['function']['arguments'])
        args['path'] = path
        call['function']['arguments'] = json.dumps(args)
    for _ in range(3):
        run_round(model, tool)
    assert [c.name for c in gateway.calls] == ['read_file', 'apply_patch', 'read_file']
    ledger, _ = progress(db, settings, task, sid)
    assert ledger['phase'] == 'implementing' and ledger['no_progress_streak'] == 0
    assert ledger['source_hash'] == results(db, settings, task, sid)[0].source_hash


def test_checks_on_old_source_cannot_finish_after_another_patch(tmp_path):
    import json
    from tests.test_native_protocol_repairs import fixture
    from app.agent_v2.native_tools import FINISH
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path)
    with db.session() as session:
        path = AgentJournal(Workbench(session, settings.artifact_root), task, sid).spec().write_paths[0]
    def patch(old, new):
        return response('apply_patch', {'path': path, 'expected_hash': hashlib.sha256(old.encode()).hexdigest(),
            'old_text': old, 'new_text': new})
    runner.responses.extend([patch('content', 'first'), response('run_approved_checks', {}),
        patch('first', 'second'), response(FINISH, {})])
    for _ in range(3):
        run_round(model, tool)
    assert asyncio.run(model.run_once()).state == 'succeeded'
    cp, _ = results(db, settings, task, sid)
    assert cp.state != 'finished' and cp.usage['protocol_repairs'] == 1
    with db.session() as session:
        j = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        rejection = next(e for e in reversed(j.events()) if e.kind == 'agent_v2_model_rejected')
        assert rejection.payload['failure']['safe_reason_code'] == 'agent_tests_missing'
