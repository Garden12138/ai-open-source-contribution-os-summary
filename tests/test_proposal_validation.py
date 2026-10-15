"""Validation failures are corrected as durable jobs without bypassing exact edits."""
import asyncio
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.artifacts import ArtifactStore
from app.models import AgentInvocation, ChangeSetProposal, ExecutionAttempt, Job
from app.planner import PlanningService
from app.proposal_validation import ProposalValidationError
from app.coding import _parse_change_set_proposal,verify_agent_invocation
from app.contribution_workflow import advance_workflows
from tests.test_execution_repair import context_for,edit_proposal
from tests.test_workbench import automatic_fixture,populate_round,drive_round


def proposal_fixture(tmp_path):
    db,settings,task,workers,code,review=automatic_fixture(tmp_path)
    populate_round(code,review)
    valid=code.contents[1]
    bad=json.dumps(edit_proposal(context_for('original content for app/plans.py\n','app/plans.py'),'invented source','replacement'))
    code.contents[1]=bad
    for _ in range(8):
        for worker in workers:
            completed=asyncio.run(worker.run_once())
            if completed and completed.kind=='change_set_proposal':
                assert completed.state=='queued' and completed.attempt_count==1
                return db,settings,task,workers,code,review,completed.id,valid,bad
        advance_workflows(db,settings)
    pytest.fail('Proposal was not reached')


@pytest.mark.parametrize('source,old,code,count',[
    ('the actual source\n','invented source','edit_anchor_missing',0),
    ('same\nsame\n','same','edit_anchor_ambiguous',2),
])
def test_exact_anchor_error_identifies_location_without_relaxing_match(source,old,code,count):
    context=context_for(source,'source.py')
    with pytest.raises(ProposalValidationError) as failure:
        _parse_change_set_proposal(json.dumps(edit_proposal(context,old,'new')),context)
    assert failure.value.code==code
    assert failure.value.diagnostic['path']=='source.py'
    assert failure.value.diagnostic['edit_index']==0
    assert failure.value.diagnostic['match_count']==count
    assert context.entries[0].content==source


def test_invalid_draft_is_recorded_and_corrected_with_same_authorization_and_budget(tmp_path):
    db,settings,task,workers,code,review,job_id,valid,bad=proposal_fixture(tmp_path)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        authorization=wb.latest(task,'execution_authorized').record_hash
        rejection=wb.latest(task,'proposal_rejected')
        receipt=wb.read(rejection.payload['result_hash'])
        assert receipt['raw_proposal']==bad and receipt['applied'] is False
        assert receipt['diagnostic']['path']=='app/plans.py'
        invocation=verify_agent_invocation(session.get(AgentInvocation,rejection.payload['agent_invocation_id']))
        assert invocation.output_hash==rejection.payload['result_hash'] and invocation.output_tokens==30
        assert session.scalar(select(ChangeSetProposal)) is None
        assert wb.active_blocker(task) is None
    code.contents.append(valid)
    drive_round(db,settings,task,workers)
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert wb.latest(task,'awaiting_acceptance')
        assert wb.latest(task,'execution_authorized').record_hash==authorization
        assert len(list(session.scalars(select(ExecutionAttempt))))==1
        assert session.get(Job,job_id).attempt_count==2
        assert len(list(session.scalars(select(AgentInvocation).where(AgentInvocation.job_id==job_id))))==2
        assert 'CONTRIBOS_PROPOSAL_VALIDATION_FEEDBACK=' in code.invocations[-1].prompt
        assert 'No edit described by a prior assistant message has been applied' in code.invocations[-1].prompt


def test_repeated_anchor_errors_stop_at_original_job_attempt_limit(tmp_path):
    db,settings,task,workers,code,review,job_id,valid,bad=proposal_fixture(tmp_path)
    for count in (2,3):
        code.contents.append(bad)
        job=asyncio.run(workers[2].run_once())
        assert job.attempt_count==count and job.state==('queued' if count==2 else 'failed')
        advance_workflows(db,settings)
    assert len(code.invocations)==4  # one reasoning turn and three proposals
    assert asyncio.run(workers[2].run_once()) is None
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert wb.active_blocker(task)
        assert len([e for e in wb.history(task) if e.kind=='proposal_rejected'])==3
        assert session.scalar(select(ChangeSetProposal)) is None


@pytest.mark.parametrize('action',['stop','conversation','cancel'])
def test_changed_or_stopped_inputs_cannot_commit_a_rejection_or_retry(tmp_path,action):
    from app.coding import CodingConversationService
    from app.jobs import JobService
    db,settings,task,workers,code,review,job_id,valid,bad=proposal_fixture(tmp_path)
    original=code.complete
    async def change(invocation):
        with db.session() as session:
            wb=PlanningService(session,settings.artifact_root)
            if action=='stop': wb.append(task,'stop_requested',{'job_ids':[job_id]},key='stop-during-call')
            elif action=='conversation':
                attempt=session.get(Job,job_id).payload['execution_attempt_id']
                CodingConversationService(session,artifact_root=settings.artifact_root).send_message(attempt,content='补充要求',idempotency_key='during-call')
            else: JobService(session).request_cancel(job_id)
        return await original(invocation)
    code.complete=change
    code.contents.append(bad)
    result=asyncio.run(workers[2].run_once())
    assert result.state in {'failed','cancelled'}
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        assert len([e for e in wb.history(task) if e.kind=='proposal_rejected'])==1
        assert session.scalar(select(ChangeSetProposal)) is None


def test_credential_like_draft_never_enters_artifacts_or_errors(tmp_path):
    db,settings,task,workers,code,review,job_id,valid,bad=proposal_fixture(tmp_path)
    canary='ghp_PROPOSALVALIDATIONCANARY123456789'
    code.contents.append(bad.replace('replacement',canary))
    result=asyncio.run(workers[2].run_once())
    assert result.state=='failed' and result.error_code=='proposal_sensitive_output'
    with db.session() as session:
        wb=PlanningService(session,settings.artifact_root)
        receipt=wb.read(wb.latest(task,'proposal_rejected').payload['result_hash'])
        assert receipt['raw_proposal'] is None
        assert canary not in json.dumps(receipt) and canary not in result.error_message
        assert session.scalar(select(ChangeSetProposal)) is None
        from app.jobs import JobService
        JobService(session).retry(job_id)
    code.contents.append(valid)
    result=asyncio.run(workers[2].run_once())
    assert result.state=='succeeded'
    assert canary not in code.invocations[-1].prompt


def test_one_invalid_operation_rejects_entire_draft_instead_of_applying_subset():
    from app.sandbox_worker.coding_context import CodingContextEntry,CodingContext
    from app.proposal_validation import check_proposal_structure
    context=context_for('original\n','source.py')
    values=context.hash_payload(); values.pop('version')
    values['entries']=(*context.entries,CodingContextEntry('z-new.md',None,None,None))
    context=CodingContext.create(**values)
    draft=edit_proposal(context,'original','fixed')
    draft['operations'].append({'path':'z-new.md','kind':'write','expected_prior_hash':None,
        'content':'x'*1_000_001,'executable':False,'edits':None})
    checked=check_proposal_structure(json.dumps(draft),context,('source.py','z-new.md'))
    with pytest.raises(ValueError): _parse_change_set_proposal(checked,context)
    draft['operations'][-1].update(content='English entry\n',executable=None)
    assert check_proposal_structure(json.dumps(draft),context,('source.py','z-new.md'))
    assert len(_parse_change_set_proposal(json.dumps(draft),context)[1])==2
