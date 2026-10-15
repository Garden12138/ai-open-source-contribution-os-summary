from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.audit import AuditService
from app.cla import (ACTION, SIGNATURE, SUPPORTED_ACTION, CLAWorker, agreement, cla_view, confirm_cla,
                     request_cla, unresolved_signature)
from app.jobs import JobService
from app.publication import GhPublicationTransport, confirm_publication
from app.publisher_accounts import append_account, assert_account_changeable
from app.workbench import Workbench, WorkbenchError
from tests.test_publication_workbench import FakeTransport, ready


class Transport(FakeTransport):
    def __init__(self, source):
        self.__dict__.update(source.__dict__)
        self.comments = []
        self.posts = 0
        self.lost = False
        self.drop = False
        self.passed = False
        self.prs[0].update(state='open', merged=False, user={'login':'contributor', 'id':123})
        self.documents['.github/workflows/cla.yaml'] = (
            'on:\n  issue_comment:\n    types: [created]\njobs:\n  cla:\n    steps:\n'
            '      - uses: contributor-assistant/github-action@' + SUPPORTED_ACTION + '\n'
            '        if: github.event.comment.body == "' + SIGNATURE + '"\n'
            '        with:\n          path-to-document: "https://github.com/' + self.repository + '/blob/main/CLA.md"\n')
        self.documents['CLA.md'] = '# Contributor License Agreement\n\nYou grant permission to use your contribution.\n'

    async def api(self, method, path, body=None, *, missing=False):
        if '/comments?' in path:
            return self.comments
        if '/check-runs?' in path:
            return {'total_count':1, 'check_runs':[{'id':12, 'name':'CLAAssistant',
                'app':{'slug':'github-actions'}, 'status':'completed', 'conclusion':'success' if self.passed else 'failure'}]}
        if path.endswith('/pulls/7'):
            return self.prs[0]
        return await super().api(method, path, body, missing=missing)

    async def cla_comment(self, repository, number, *, authorization):
        assert repository == self.repository and number == 7
        assert authorization['comment'] == SIGNATURE and authorization['action'] == ACTION
        self.posts += 1
        value = {'id':19, 'html_url':f'https://github.com/{repository}/pull/7#issuecomment-19',
                 'body':SIGNATURE, 'user':{'login':'contributor', 'id':123}}
        if not self.drop:
            self.comments.append(value)
        if self.lost or self.drop:
            raise WorkbenchError('Response unavailable')
        return value


@pytest.fixture
def published(tmp_path):
    db, settings, task, source, publisher = ready(tmp_path)
    assert asyncio.run(publisher.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        p = wb.latest(task, 'publication_prepared')
        confirm_publication(wb, task, intent_hash=p.record_hash, nonce=p.payload['nonce'])
    assert asyncio.run(publisher.run_once()).state == 'succeeded'
    settings = replace(settings, publisher_management_key=bytes.fromhex('ac'*32), local_access_token='local-cla-test')
    transport = Transport(source)
    with db.session() as session:
        account = append_account(session, {'state':'connected', 'login':'contributor', 'user_id':123,
            'scopes':['public_repo'], 'credential_ref':'private-reference'}, correlation_id='account')
        account_hash = account.event_hash
        session.commit()
    worker = CLAWorker(db, settings, worker_id='cla-test', transport=transport)
    yield db, settings, task, account_hash, transport, worker
    db.close()


def prepare(f):
    db, settings, task, account, _, worker = f
    with db.session() as session:
        request_cla(Workbench(session, settings.artifact_root), task, account_hash=account, key='preview')
    result = asyncio.run(worker.run_once())
    assert result.state == 'succeeded', (result.error_code, result.error_message)
    with db.session() as session:
        return cla_view(Workbench(session, settings.artifact_root), task)['preview']


def consent(f, p, **overrides):
    db, settings, task, *_ = f
    payload = dict(preview_hash=p['record_hash'], nonce=p['nonce'], agreement_hash=p['agreement_hash'],
                   acknowledgement=p['acknowledgement'], read_and_agree=True)
    payload.update(overrides)
    with db.session() as session:
        return confirm_cla(Workbench(session, settings.artifact_root), task, **payload)


def test_read_consent_exactly_once_and_remote_status_distinct(published):
    db, settings, task, account, transport, worker = published
    p = prepare(published)
    assert p['agreement_text'] == transport.documents['CLA.md']
    assert transport.posts == 0 and p['status'] == 'failed'
    consent(published, p)
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    assert transport.posts == 1
    with pytest.raises(WorkbenchError):
        consent(published, p)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        view = cla_view(wb, task)
        assert view['submitted']['comment_id'] == 19 and view['status']['status'] == 'failed'
        assert not unresolved_signature(wb, task)
        assert AuditService(session).verify().valid
        request_cla(wb, task, account_hash=account, key='check', check=True)
    transport.passed = True
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    with db.session() as session:
        assert cla_view(Workbench(session, settings.artifact_root), task)['status']['status'] == 'passed'
    assert transport.posts == 1


@pytest.mark.parametrize('change', [dict(read_and_agree=False), dict(acknowledgement='确认'),
    dict(nonce='0'*64), dict(agreement_hash='0'*64), dict(preview_hash='0'*64)])
def test_plain_chat_or_wrong_bound_consent_never_signs(published, change):
    p = prepare(published)
    with pytest.raises(WorkbenchError):
        consent(published, p, **change)
    assert published[4].posts == 0


@pytest.mark.parametrize('mutation', ['document', 'workflow', 'head', 'closed', 'ready', 'author', 'account'])
def test_stale_remote_or_account_stops_before_write(published, mutation):
    db, settings, task, _, transport, worker = published
    p = prepare(published)
    consent(published, p)
    if mutation == 'document': transport.documents['CLA.md'] += 'New obligation.\n'
    elif mutation == 'workflow': transport.documents['.github/workflows/cla.yaml'] += '# updated\n'
    elif mutation == 'head': transport.prs[0]['head']['sha'] = 'f'*40
    elif mutation == 'closed': transport.prs[0]['state'] = 'closed'
    elif mutation == 'ready': transport.prs[0]['draft'] = False
    elif mutation == 'author': transport.prs[0]['user']['id'] = 999
    else:
        with db.session() as session:
            append_account(session, {'state':'connected','login':'someone','user_id':999}, correlation_id='changed')
            session.commit()
    result = asyncio.run(worker.run_once())
    assert result.state == 'failed' and transport.posts == 0


@pytest.mark.parametrize('lost', [True, False])
def test_unknown_write_reconciles_without_duplicate_post(published, lost):
    db, settings, task, account, transport, worker = published
    p = prepare(published)
    job = consent(published, p)
    transport.lost, transport.drop = lost, not lost
    assert asyncio.run(worker.run_once()).state == 'failed'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert unresolved_signature(wb, task)
        with pytest.raises(WorkbenchError): assert_account_changeable(session)
        with pytest.raises(WorkbenchError): request_cla(wb, task, account_hash=account, key='new')
        session.rollback()
        JobService(session).retry(job.id)
    result = asyncio.run(worker.run_once())
    assert result.state == ('succeeded' if lost else 'failed')
    assert transport.posts == 1
    if not lost:
        # An eventually visible comment is reconciled by a separate GET-only check.
        transport.comments.append({'id':19, 'html_url':p['pr_url']+'#issuecomment-19',
            'body':SIGNATURE,'user':{'login':'contributor','id':123}})
        with db.session() as session:
            request_cla(Workbench(session, settings.artifact_root), task, account_hash=account, key='reconcile', check=True)
        assert asyncio.run(worker.run_once()).state == 'succeeded'
    with db.session() as session:
        assert not unresolved_signature(Workbench(session, settings.artifact_root), task)
        assert_account_changeable(session)
    assert transport.posts == 1


def test_unsupported_external_legal_portal_never_automated(published):
    transport = published[4]
    transport.documents['.github/workflows/cla.yaml'] = transport.documents['.github/workflows/cla.yaml'].replace(
        'https://github.com/'+transport.repository+'/blob/main/CLA.md', 'https://external.example/cla')
    with pytest.raises(WorkbenchError, match='外部页面'):
        asyncio.run(agreement(transport, transport.repository))
    assert transport.posts == 0


def test_expired_preview_and_write_before_send_require_new_consent(published):
    db, settings, task, _, transport, worker = published
    p = prepare(published)
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        previous = wb.latest(task, 'cla_prepared')
        wb.append(task, 'cla_prepared', {**previous.payload,
            'expires_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}, key='expired')
        expired = cla_view(wb, task)['preview']
    with pytest.raises(WorkbenchError, match='过期'): consent(published, expired)
    with pytest.raises(WorkbenchError, match='变化'): consent(published, p)
    assert transport.posts == 0


def test_corrupt_agreement_and_remote_blob_fail_closed(published):
    from pathlib import Path
    from app.models import Artifact
    from app.artifacts import ArtifactIntegrityError
    db, settings, task, _, transport, _ = published
    p = prepare(published)
    with db.session() as session:
        artifact = session.get(Artifact, p['agreement_artifact'])
        (Path(settings.artifact_root)/artifact.storage_key).write_bytes(b'Changed agreement')
    with pytest.raises(ArtifactIntegrityError): consent(published, p)
    assert transport.posts == 0


def test_unknown_comment_reconciliation_survives_pr_head_moving(published):
    db, settings, task, account, transport, worker = published
    p = prepare(published)
    consent(published, p)
    transport.lost = True
    assert asyncio.run(worker.run_once()).state == 'failed'
    transport.prs[0]['head']['sha'] = 'f'*40
    transport.prs[0]['state'] = 'closed'
    original = transport.api
    async def checks_unavailable(method,path,body=None,*,missing=False):
        if '/check-runs?' in path: raise WorkbenchError('Checks temporarily unavailable')
        return await original(method,path,body,missing=missing)
    transport.api = checks_unavailable
    with db.session() as session:
        request_cla(Workbench(session, settings.artifact_root), task, account_hash=account, key='moved-check', check=True)
    assert asyncio.run(worker.run_once()).state == 'succeeded'
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert not unresolved_signature(wb, task)
        assert cla_view(wb, task)['status']['status'] == 'unavailable'
    assert transport.posts == 1


def test_comment_scan_budget_and_blob_tampering_prevent_signing(published):
    from app.cla import existing_signature, read_file
    transport = published[4]
    transport.comments = [{'id':i,'body':'discussion','user':{'login':'other','id':999}} for i in range(100)]
    with pytest.raises(WorkbenchError, match='预算'):
        asyncio.run(existing_signature(transport, {'repository':transport.repository,'number':7}, 'contributor',123))
    original = transport.api
    async def tampered(method,path,body=None,*,missing=False):
        value = await original(method,path,body,missing=missing)
        if '/contents/CLA.md' in path: value['sha'] = '0'*40
        return value
    transport.api = tampered
    with pytest.raises(WorkbenchError, match='哈希'):
        asyncio.run(read_file(transport,transport.repository,'CLA.md','a'*40))
    assert transport.posts == 0


def test_confirmed_write_cannot_be_stopped_or_archived(published):
    from app.contribution_workflow import stop_workflow
    from app.task_visibility import TaskVisibilityService
    from app.planning import ContributionTaskConflictError
    db, settings, task, _, transport, worker = published
    consent(published, prepare(published))
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        with pytest.raises(WorkbenchError, match='不能中途取消'): stop_workflow(wb,task,key='stop-cla')
    transport.drop = True
    assert asyncio.run(worker.run_once()).state == 'failed'
    with db.session() as session:
        with pytest.raises(ContributionTaskConflictError, match='CLA'):
            TaskVisibilityService(session).change(task,target='archived',expected_sequence=0)


def test_generic_comments_denied_dedicated_transport_only_fixed_signature(monkeypatch):
    async def process(argv, **kwargs):
        assert argv[argv.index('--method')+1] == 'POST'
        assert json.loads(kwargs['stdin']) == {'body':SIGNATURE}
        return 0, b'HTTP/2.0 201 Created\n\n{"id":19}'
    monkeypatch.setattr('app.publication.bounded_process', process)
    transport = GhPublicationTransport()
    with pytest.raises(WorkbenchError): asyncio.run(transport.api('POST', 'repos/owner/repo/issues/7/comments', {'body':SIGNATURE}))
    authorization = {'action':ACTION, 'repository':'owner/repo','number':7, 'comment':SIGNATURE,
                     'actor_id':'local-user','authorization_hash':'a'*64}
    assert asyncio.run(transport.cla_comment('owner/repo',7,authorization=authorization)) == {'id':19}
    with pytest.raises(WorkbenchError):
        asyncio.run(transport.cla_comment('owner/repo',7,authorization={**authorization,'comment':'recheck'}))


def test_api_no_model_needed_and_exact_human_payload_required(published):
    db, settings, task, account, transport, worker = published
    app = create_app(settings)
    endpoint = f'/api/v1/tasks/{task}/workbench'
    with TestClient(app) as client:
        # Existing API auth protects both read-job enqueue and legal consent.
        assert client.post(endpoint+'/cla/prepare',headers={'Idempotency-Key':'api'}).status_code in {401,403}
        assert client.post(endpoint+'/cla/prepare',headers={'Origin':'http://evil.example','Idempotency-Key':'api'}).status_code == 403
        headers = {'Authorization':'Bearer '+settings.local_access_token,'Idempotency-Key':'api'}
        response = client.post(endpoint+'/cla/prepare',headers=headers,json={})
        assert response.status_code == 202, response.text
        assert asyncio.run(worker.run_once()).state == 'succeeded'
        p = client.get(endpoint).json()['cla']['preview']
        payload = {'preview_hash':p['record_hash'],'nonce':p['nonce'],'agreement_hash':p['agreement_hash'],
            'acknowledgement':p['acknowledgement'],'read_and_agree':True}
        assert client.post(endpoint+'/cla/confirm',headers=headers,json={**payload,'read_and_agree':False}).status_code == 422
        assert client.post(endpoint+'/cla/confirm',headers=headers,json={**payload,'comment':'other'}).status_code == 422
        response = client.post(endpoint+'/cla/confirm',headers=headers,json=payload)
        assert response.status_code == 202, response.text
        history = client.get(endpoint).json()
        assert [e['payload']['acknowledgement'] for e in history['events'] if e['kind']=='cla_confirmed'] == [p['acknowledgement']]
        assert client.post('/api/v1/jobs/'+response.json()['id']+'/cancel',headers=headers).status_code == 409
    assert transport.posts == 0


def test_check_receipt_is_exposed_in_conversation_and_timeline(published):
    db, settings, task, account, transport, worker = published
    prepare(published)
    transport.passed = True
    original = transport.api
    url = f'https://github.com/{transport.repository}/actions/runs/42/job/12'
    async def with_url(method,path,body=None,*,missing=False):
        value = await original(method,path,body,missing=missing)
        if '/check-runs?' in path: value['check_runs'][0]['details_url'] = url
        return value
    transport.api = with_url
    app = create_app(settings)
    endpoint = f'/api/v1/tasks/{task}/workbench'
    with TestClient(app) as client:
        before = client.get(endpoint).json()
        response = client.post(endpoint+'/cla/check',json={},headers={
            'Authorization':'Bearer '+settings.local_access_token,'Idempotency-Key':'result'})
        assert response.status_code == 202
        identifier = response.json()['id']
        assert asyncio.run(worker.run_once()).state == 'succeeded'
        after = client.get(endpoint).json()
        assert after['execution']['revision'] != before['execution']['revision']
        result = next(e for e in after['events'] if e['kind']=='cla_status_observed' and e['job_id']==identifier)
        assert result['payload']['status']=='passed' and result['payload']['check_url']==url
        timeline = client.get(endpoint+'/timeline',params={'after':before['execution']['event_cursor']}).json()
        assert result['id'] in [e['id'] for e in timeline['events']]
        assert timeline['execution']['revision'] == after['execution']['revision']
        assert after['cla']['status']['observed_at'] == result['payload']['observed_at']
    assert transport.posts == 0


@pytest.mark.parametrize('url,allowed', [
    ('https://github.com/owner/repo/actions/runs/42/job/12',True),
    ('https://github.com/other/repo/actions/runs/42/job/12',False),
    ('javascript:alert(1)',False)])
def test_check_result_link_is_bound_to_github_repository(url,allowed):
    from app.cla import check_status
    class Checks:
        async def api(self,*args,**kwargs):
            return {'total_count':1,'check_runs':[{'id':12,'name':'CLAAssistant',
                'status':'completed','conclusion':'success','app':{'slug':'github-actions'},'details_url':url}]}
    result=asyncio.run(check_status(Checks(),{'repository':'owner/repo','head_sha':'a'*40}))
    assert result['status']=='passed'
    assert result.get('check_url') == (url if allowed else None)
