from __future__ import annotations
import asyncio
import json
import pytest
from sqlalchemy import select
from app.coding import _parse_change_set_proposal
from app.proposal_validation import check_proposal_structure
from tests.test_execution_repair import context_for


def append_draft(context, addition, edits=None):
    return {'summary':'Add regression tests','operations':[{
        'path':context.entries[0].path,'kind':'append','expected_prior_hash':context.entries[0].prior_hash,
        'content':addition,'edits':edits,'executable':None}]}


def test_append_preserves_original_bytes_and_allows_exact_import_edits():
    context=context_for('import old\r\n\r\ndef existing():\r\n    return 1\r\n','tests/test_example.py')
    addition='\r\n\r\ndef test_regression():\r\n    assert existing() == 1\r\n'
    draft=append_draft(context,addition,[{'old_text':'import old\r\n','new_text':'import updated\r\n'}])
    checked=check_proposal_structure(json.dumps(draft),context,[context.entries[0].path])
    _,operations=_parse_change_set_proposal(checked,context)
    assert operations[0].content==context.entries[0].content.replace('import old','import updated')+addition
    assert operations[0].expected_prior_hash==context.entries[0].prior_hash
    assert operations[0].executable==context.entries[0].executable
    assert operations[0].kind.value=='write'


@pytest.mark.parametrize('bad',['hash','missing','scope','empty','mode','secret','edit'])
def test_append_cannot_bypass_hash_scope_sensitive_or_exact_edit_guards(bad):
    context=context_for('original\n','tests/test_example.py')
    draft=append_draft(context,'\ndef test_new():\n    pass\n')
    operation=draft['operations'][0]
    if bad=='hash':operation['expected_prior_hash']='f'*64
    if bad=='missing':
        from app.sandbox_worker.coding_context import CodingContext,CodingContextEntry
        data=context.hash_payload();data.pop('version');data['entries']=(CodingContextEntry(operation['path'],None,None,None),)
        context=CodingContext.create(**data);operation['expected_prior_hash']=None
    if bad=='scope':operation['path']='outside.py'
    if bad=='empty':operation['content']=''
    if bad=='mode':operation['executable']=True
    if bad=='secret':operation['content']='ghp_APPENDPROPOSALCANARY1234567890'
    if bad=='edit':operation['edits']=[{'old_text':'invented','new_text':'replacement'}]
    with pytest.raises(ValueError):
        checked=check_proposal_structure(json.dumps(draft),context,['tests/test_example.py'])
        _parse_change_set_proposal(checked,context)


def exhausted_proposal(tmp_path):
    from tests.test_proposal_validation import proposal_fixture
    from app.contribution_workflow import advance_workflows
    db,settings,task,workers,code,review,jid,valid,bad=proposal_fixture(tmp_path)
    for _ in range(2):
        code.contents.append(bad)
        asyncio.run(workers[2].run_once())
    advance_workflows(db,settings)
    return db,settings,task,workers,code,review,jid,valid


def test_local_correction_resumes_same_attempt_without_extending_model_budget(tmp_path):
    from app.proposal_recovery import record_corrected_proposal
    from app.planner import PlanningService
    from app.models import Job,AgentInvocation,ExecutionAttempt
    from app.contribution_workflow import advance_workflows
    db,settings,task,workers,code,review,jid,valid=exhausted_proposal(tmp_path)
    calls=len(code.invocations)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        hashes=[e.record_hash for e in wb.history(task)]
        auth=wb.latest(task,'execution_authorized').record_hash
        rejection=wb.latest(task,'proposal_rejected')
        receipt=record_corrected_proposal(wb,jid,raw=valid,expected_rejection_hash=rejection.record_hash,settings=settings)
        assert record_corrected_proposal(wb,jid,raw=valid,expected_rejection_hash=rejection.record_hash,settings=settings).id==receipt.id
        assert [e.record_hash for e in wb.history(task)[:len(hashes)]]==hashes
        assert wb.active_blocker(task) is None
        assert wb.latest(task,'execution_authorized').record_hash==auth
        assert session.get(Job,jid).state=='failed' and session.get(Job,jid).attempt_count==session.get(Job,jid).max_attempts==3
    for _ in range(8):
        for worker in workers:
            job=asyncio.run(worker.run_once())
            if job:assert job.state=='succeeded',job.error_message
        advance_workflows(db,settings)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert wb.latest(task,'awaiting_acceptance') and not wb.active_blocker(task)
        assert len(list(session.scalars(select(ExecutionAttempt))))==1
        assert len(list(session.scalars(select(AgentInvocation).where(AgentInvocation.job_id==jid))))==3
        assert receipt.payload['source']=='local_correction' and receipt.payload['model_calls_added']==0
        event_count=len(wb.history(task))
        assert record_corrected_proposal(wb,jid,raw=valid,expected_rejection_hash=rejection.record_hash,settings=settings).id==receipt.id
        assert len(wb.history(task))==event_count
        assert wb.read(receipt.payload['proposal_artifact_id'])=={'raw':valid}
    assert len(code.invocations)==calls


@pytest.mark.parametrize('bad',['rejection','hash','scope','secret','stop','changed_conversation','different_replay'])
def test_local_correction_rejects_stale_or_invalid_inputs_and_conflicting_replay(tmp_path,bad):
    from app.proposal_recovery import record_corrected_proposal
    from app.planner import PlanningService
    from app.coding import CodingConversationService
    from app.coding import CodingConflictError
    from app.workbench import WorkbenchError
    from app.models import Job,ChangeSetProposal
    db,settings,task,workers,code,review,jid,valid=exhausted_proposal(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        rejection=wb.latest(task,'proposal_rejected');expected=rejection.record_hash
        if bad=='different_replay':record_corrected_proposal(wb,jid,raw=valid,expected_rejection_hash=expected,settings=settings)
        if bad=='stop':wb.append(task,'stop_requested',{'job_ids':[]},key='stop-local-correction')
        if bad=='rejection':expected='a'*64
        if bad=='changed_conversation':
            CodingConversationService(session,artifact_root=settings.artifact_root).send_message(
                session.get(Job,jid).payload['execution_attempt_id'],content='Changed requirement',idempotency_key='new-requirement')
        draft=json.loads(valid)
        if bad=='hash':draft['operations'][0]['expected_prior_hash']='f'*64
        if bad=='scope':draft['operations'][0]['path']='outside.py'
        if bad=='secret':draft['operations'][0]['content']='ghp_LOCALCORRECTIONCANARY1234567890'
        if bad=='different_replay':draft['operations'][0]['content']+='\n# different\n'
        with pytest.raises((ValueError, WorkbenchError, CodingConflictError)):
            record_corrected_proposal(wb,jid,raw=json.dumps(draft),expected_rejection_hash=expected,settings=settings)
        session.rollback()
        assert bool(wb.latest(task,'proposal_corrected'))==(bad=='different_replay')
        assert session.scalar(select(ChangeSetProposal)) is None
        assert session.get(Job,jid).state=='failed'


def test_retry_feedback_repeats_only_the_failing_authoritative_file_and_explains_append(tmp_path):
    from tests.test_proposal_validation import proposal_fixture
    db,settings,task,workers,code,review,jid,valid,bad=proposal_fixture(tmp_path)
    code.contents.append(valid)
    job=asyncio.run(workers[2].run_once())
    assert job.state=='succeeded'
    feedback=json.loads(code.invocations[-1].prompt.split('CONTRIBOS_PROPOSAL_VALIDATION_FEEDBACK=',1)[1])
    assert feedback['authoritative_file']['content']=='original content for app/plans.py\n'
    assert 'kind=append' in feedback['instruction']


def test_correction_acceptance_rolls_back_stage_when_implement_enqueue_fails(tmp_path,monkeypatch):
    from app.proposal_recovery import record_corrected_proposal
    from app.planner import PlanningService
    from app.executions import ExecutionAttemptService
    from app.execution_control import ExecutionStageControlService
    from app.contribution_workflow import advance_workflows
    from app.workbench import WorkbenchError
    from app.models import Job
    db,settings,task,workers,code,review,jid,valid=exhausted_proposal(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        record_corrected_proposal(wb,jid,raw=valid,expected_rejection_hash=wb.latest(task,'proposal_rejected').record_hash,settings=settings)
        attempt_id=session.get(Job,jid).payload['execution_attempt_id']
        original=ExecutionAttemptService(session).current(attempt_id).record_hash
    def fail(*args,**kwargs):
        raise WorkbenchError('Injected enqueue failure')
    monkeypatch.setattr(ExecutionStageControlService,'schedule_current',fail)
    advance_workflows(db,settings)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert ExecutionAttemptService(session).current(attempt_id).record_hash==original
        assert wb.latest(task,'automatic_change_accepted') is None
        assert wb.latest(task,'proposal_corrected') is not None
