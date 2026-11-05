"""Terminal checkpoint failures must stop the durable semantic progress too."""
import asyncio

from app.agent_v2.journal import AgentJournal
from app.agent_v2.progress import current_progress
from app.workbench import Workbench
from tests.test_native_protocol_repairs import fixture, overflow


def test_protocol_quota_block_is_durable_in_progress_without_restarting_model(tmp_path):
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path,
        overflow(6), overflow(5), overflow(5))
    for _ in range(3):
        assert asyncio.run(model.run_once()).state == 'succeeded'
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        cp = journal.checkpoint()
        progress = current_progress(journal)
        assert cp.state == 'blocked'
        assert progress.phase == 'blocked'
        assert progress.blocker == 'agent_native_call_count_invalid'
        assert progress.next_action == 'inspect_diagnostic'
        step = next(s for s in progress.steps if s.step_id == 'investigate')
        assert step.status == 'blocked' and step.blocker == progress.blocker
        rejected = next(e for e in reversed(journal.events()) if e.kind == 'agent_v2_model_rejected')
        assert rejected.record_hash in step.evidence_refs
        assert rejected.payload['response_artifact'] in step.evidence_refs
        assert progress.source_hash == cp.source_hash
        assert cp.usage['model_calls'] == 3 and cp.usage['protocol_repairs'] == 2
        revision = progress.revision
        from app.agent_v2.progress import record_checkpoint_blocked
        record_checkpoint_blocked(journal, cp)
        assert current_progress(journal).revision == revision
    assert asyncio.run(model.run_once()) is None
    assert asyncio.run(tool.run_once()) is None
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert current_progress(journal).revision == revision
        assert journal.checkpoint() == cp
    assert len(runner.prompts) == 3 and gateway.calls == []


def test_response_budget_failure_updates_progress_without_any_tool(tmp_path):
    import json
    value = overflow()
    value['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = json.dumps({'query': 'x' * 500000})
    db, settings, task, sid, model, tool, runner, gateway = fixture(tmp_path, value)
    assert asyncio.run(model.run_once()).state == 'failed'
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        progress = current_progress(journal)
        assert progress.phase == 'blocked' and progress.blocker == 'agent_budget_exceeded'
        assert progress.next_action == 'inspect_response_evidence_budget'
        assert journal.checkpoint().usage['model_calls'] == 1
    assert not gateway.calls and asyncio.run(tool.run_once()) is None


def test_checkpoint_and_terminal_progress_rollback_together(tmp_path):
    db, settings, task, sid, *_ = fixture(tmp_path)
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        cp = journal.checkpoint()
        old = current_progress(journal)
        journal.transition(cp, state='blocked', key='rollback-terminal')
        assert current_progress(journal).phase == 'blocked'
        session.rollback()
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        assert journal.checkpoint() == cp and current_progress(journal) == old


def test_legacy_terminal_checkpoint_does_not_backfill_missing_progress(tmp_path):
    from tests.test_agent_v2_loop import fixture as legacy_fixture
    db, settings, task, *rest = legacy_fixture(tmp_path, {'outcome': 'blocked'})
    sid = rest[-1]
    with db.session() as session:
        journal = AgentJournal(Workbench(session, settings.artifact_root), task, sid)
        journal.transition(journal.checkpoint(), state='blocked', key='legacy-terminal')
        assert current_progress(journal) is None
