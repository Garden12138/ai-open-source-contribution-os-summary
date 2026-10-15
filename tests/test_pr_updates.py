from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.jobs import JobService
from app.pr_updates import (ACTION, PRUpdateWorker, confirm_update, is_update_request,
                            request_update, update_view)
from app.publication import GhPublicationTransport
from app.publication_documents import PublicationDocumentWorker
from app.publisher_accounts import assert_account_changeable
from app.workbench import Workbench, WorkbenchError
from tests.test_cla import published  # noqa: F401
from tests.test_nvidia_workflows import ScriptedRunner, _identity

TITLE = 'fix: preserve streamed output fragments'
BODY = '## Summary\nPreserve all streamed output fragments.\n\n## Changes\nAccumulate content rather than replace it.\n\n## Testing\nThe supplied sandbox verification completed successfully.\n\n## Related Issue\nSee the original issue.\n'


@pytest.fixture
def flow(published):
    db, settings, task, account, transport, _ = published
    transport.prs[0]['base']['sha'] = transport.base
    transport.patches = 0
    transport.reject = False

    async def patch(repository, number, *, authorization):
        assert repository == transport.repository and number == 7
        assert authorization['action'] == ACTION
        transport.patches += 1
        if transport.reject:
            raise WorkbenchError('Rejected', code='pr_update_rejected')
        if not transport.drop:
            transport.prs[0].update(title=authorization['title'], body=authorization['body'])
        if transport.lost or transport.drop:
            raise WorkbenchError('Unknown response')
        return transport.prs[0]

    transport.pr_text = patch
    worker = PRUpdateWorker(db, settings, worker_id='text-publisher', transport=transport)
    return db, settings, task, account, transport, worker


def prepare(f, *, key='preview', title=TITLE, body=BODY):
    db, settings, task, account, transport, worker = f
    with db.session() as session:
        request_update(Workbench(session, settings.artifact_root), task, text='请更新 PR 标题和正文为英文',
                       account_hash=account, key=key)
    result = asyncio.run(worker.run_once())
    assert result.state == 'succeeded', (result.error_code, result.error_message)
    provider = ScriptedRunner(json.dumps({'title':title, 'body':body}))
    model = PublicationDocumentWorker(db, settings, provider, _identity(settings.implementation_model), worker_id='text-model')
    result = asyncio.run(model.run_once())
    assert result.state == 'succeeded', (result.error_code, result.error_message)
    assert transport.patches == 0 or key != 'preview'
    prompt = json.loads(provider.invocations[0].prompt)
    assert prompt['evidence']['current_pr']['title'] == transport.prs[0]['title']
    assert prompt['evidence']['user_request'] == '请更新 PR 标题和正文为英文'
    assert 'credential_ref' not in provider.invocations[0].prompt
    with db.session() as session:
        return update_view(Workbench(session, settings.artifact_root), task)['preview']


def consent(f, p, **overrides):
    db, settings, task, *_ = f
    payload = dict(preview_hash=p['record_hash'], nonce=p['nonce'], text=p['acknowledgement'], approved=True)
    payload.update(overrides)
    with db.session() as session:
        return confirm_update(Workbench(session, settings.artifact_root), task, **payload)


def run(f):
    result = asyncio.run(f[-1].run_once())
    assert result is not None
    return result


def test_preview_consent_write_and_immutable_published_history(flow):
    db, settings, task, account, transport, worker = flow
    before = json.loads(json.dumps(transport.prs[0]))
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        original = wb.latest(task, 'publication_completed').record_hash
    p = prepare(flow)
    assert p['title'] == TITLE and p['before']['title'] == before['title']
    assert p['actions'] == [ACTION] and not p['consumed']
    consent(flow, p)
    result = run(flow)
    assert result.state == 'succeeded', (result.error_code, result.error_message)
    assert transport.patches == 1
    assert transport.prs[0]['title'] == TITLE and transport.prs[0]['body'] == p['body']
    assert all(transport.prs[0][k] == before[k] for k in before if k not in {'title','body'})
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert wb.latest(task, 'publication_completed').record_hash == original
        assert wb.latest(task, 'pr_update_completed').payload['preview_hash'] == p['record_hash']
        assert not update_view(wb, task)['unresolved']
        assert_account_changeable(session)
    with pytest.raises(WorkbenchError, match='已使用'):
        consent(flow, p)
    p2 = prepare(flow, key='next', title='fix: retain complete streamed output')
    assert not p2['consumed'] and p2['before']['title'] == TITLE


@pytest.mark.parametrize('overrides', [{'text':'确认'}, {'nonce':'b'*64}, {'approved':False}, {'preview_hash':'f'*64}])
def test_exact_separate_human_consent(flow, overrides):
    p = prepare(flow)
    with pytest.raises(WorkbenchError):
        consent(flow, p, **overrides)
    assert flow[4].patches == 0


@pytest.mark.parametrize('damage', ['text','head','base','state','draft','account','rules','expiry'])
def test_stale_remote_or_authority_never_patches(flow, damage):
    p = prepare(flow)
    consent(flow, p)
    db, settings, task, account, transport, _ = flow
    if damage == 'text': transport.prs[0]['body'] += '\nManual edit'
    if damage == 'head': transport.prs[0]['head']['sha'] = 'e'*40
    if damage == 'base': transport.prs[0]['base']['sha'] = 'b'*40
    if damage == 'state': transport.prs[0]['state'] = 'closed'
    if damage == 'draft': transport.prs[0]['draft'] = False
    if damage == 'account': transport.prs[0]['user']['id'] = 456
    if damage == 'rules': transport.documents['CONTRIBUTING.md'] = 'New contribution requirements.'
    if damage == 'expiry':
        from unittest.mock import patch
        with patch('app.pr_updates.datetime') as clock:
            clock.now.return_value = datetime.now(timezone.utc)+timedelta(hours=1)
            clock.fromisoformat = datetime.fromisoformat
            result = run(flow)
    else: result = run(flow)
    assert result.state == 'failed'
    assert transport.patches == 0


@pytest.mark.parametrize('applied', [True, False])
def test_lost_response_retries_reconcile_without_second_patch(flow, applied):
    db, settings, task, account, transport, worker = flow
    p = prepare(flow); consent(flow,p)
    transport.lost = True
    transport.drop = not applied
    result = run(flow)
    assert result.state == 'failed' and transport.patches == 1
    with db.session() as session:
        with pytest.raises(WorkbenchError, match='尚未明确'):
            request_update(Workbench(session, settings.artifact_root),task,text='修改 PR 文案',account_hash=account,key='blocked')
    with db.session() as session:
        with pytest.raises(WorkbenchError): assert_account_changeable(session)
    with db.session() as session: JobService(session).retry(result.id)
    result = run(flow)
    assert result.state == ('succeeded' if applied else 'failed')
    assert transport.patches == 1
    with db.session() as session:
        request_update(Workbench(session, settings.artifact_root),task,text='',account_hash=account,key='check',check=True)
    result = run(flow)
    assert result.state == 'succeeded' and result.result_data['matched'] is applied
    if not applied:
        transport.prs[0].update(title=p['title'],body=p['body'])
        with db.session() as session:
            request_update(Workbench(session, settings.artifact_root),task,text='',account_hash=account,key='late',check=True)
        assert run(flow).state == 'succeeded'
    assert transport.patches == 1
    with db.session() as session:
        assert_account_changeable(session)
        assert not update_view(Workbench(session,settings.artifact_root),task)['unresolved']


def test_explicit_rejection_allows_new_preview_without_reusing_authorization(flow):
    db, settings, task, account, transport, worker = flow
    p = prepare(flow); consent(flow,p); transport.reject = True
    result = run(flow)
    assert result.state == 'failed' and result.error_code == 'pr_update_rejected'
    with db.session() as session:
        assert_account_changeable(session)
        JobService(session).retry(result.id)
    assert run(flow).state == 'failed' and transport.patches == 1
    prepare(flow, key='after-rejection')


@pytest.mark.parametrize(('text','expected'), [('请更新 PR 标题和正文为英文',True),('translate PR body into English',True),
    ('可以修改 PR 标题吗？',False),('确认',False),('查看进度',False),('更新代码并 push',False)])
def test_classifier_only_prepares(text, expected):
    assert is_update_request(text) is expected


def test_api_natural_language_and_strict_model_free_confirmation(flow):
    db, settings, task, account, transport, worker = flow
    app = create_app(settings)
    endpoint = f'/api/v1/tasks/{task}/workbench'
    headers = {'Authorization':'Bearer '+settings.local_access_token,'Idempotency-Key':'natural'}
    with TestClient(app) as client:
        assert client.post(endpoint+'/messages',json={'mode':'question','text':'请修改 PR 标题为英文'},headers={'Origin':'http://evil.example','Idempotency-Key':'evil'}).status_code == 403
        response = client.post(endpoint+'/messages',json={'mode':'question','text':'请修改 PR 标题和正文为英文'},headers=headers)
        assert response.status_code == 202, response.text
        assert response.json()['kind'] == 'pr_update_read'
    assert run(flow).state == 'succeeded'
    provider = ScriptedRunner(json.dumps({'title':TITLE,'body':BODY}))
    model = PublicationDocumentWorker(db, settings, provider, _identity(settings.implementation_model), worker_id='api-model')
    assert asyncio.run(model.run_once()).state == 'succeeded'
    with db.session() as session: p = update_view(Workbench(session,settings.artifact_root),task)['preview']
    payload = dict(preview_hash=p['record_hash'],nonce=p['nonce'],text=p['acknowledgement'],approved=True)
    with TestClient(app) as client:
        assert client.post(endpoint+'/pr-update/confirm',json=payload).status_code in {401,403}
        assert client.post(endpoint+'/pr-update/confirm',headers=headers,json={**payload,'text':'确认'}).status_code == 409
        assert client.post(endpoint+'/pr-update/confirm',headers=headers,json={**payload,'body':'arbitrary'}).status_code == 422
        assert client.post(endpoint+'/pr-update/confirm',headers=headers,json={**payload,'approved':False}).status_code == 422
        response = client.post(endpoint+'/pr-update/confirm',headers=headers,json=payload)
        assert response.status_code == 202, response.text
        assert client.post('/api/v1/jobs/'+response.json()['id']+'/cancel',headers=headers).status_code == 409
        assert any(e['kind']=='pr_update_prepared' for e in client.get(endpoint).json()['events'])
        assert any(e['kind']=='pr_update_confirmed' for e in client.get(endpoint+'/timeline').json()['events'])
    assert transport.patches == 0
    assert run(flow).state == 'succeeded'
    app.state.database.close()


def test_transport_only_emits_title_body_and_generic_patch_prohibited(monkeypatch):
    t = GhPublicationTransport(); calls = []
    async def fake(method,path,body,**kwargs):
        calls.append((method,path,body,kwargs)); return {}
    monkeypatch.setattr(t,'_request',fake)
    auth = dict(action=ACTION,actor_id='local-user',authorization_hash='a'*64,repository='fixture/repo',number=7,title=TITLE,body=BODY,state='closed')
    asyncio.run(t.pr_text('fixture/repo',7,authorization=auth))
    assert calls[0][:3] == ('PATCH','repos/fixture/repo/pulls/7',{'title':TITLE,'body':BODY})
    with pytest.raises(WorkbenchError): asyncio.run(t.api('PATCH','repos/fixture/repo/pulls/7',{'title':TITLE}))
    for key,value in [('action','merge'),('actor_id','model'),('number',8),('authorization_hash','invalid')]:
        with pytest.raises(WorkbenchError): asyncio.run(t.pr_text('fixture/repo',7,authorization={**auth,key:value}))
    assert len(calls) == 1


def test_preview_and_request_replay_are_bound_to_content(flow):
    db, settings, task, account, transport, worker = flow
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        first = request_update(wb, task, text='修改 PR 标题', account_hash=account, key='replay')
    with db.session() as session:
        wb = Workbench(session, settings.artifact_root)
        assert request_update(wb, task, text='修改 PR 标题', account_hash=account, key='replay').id == first.id
    with db.session() as session:
        with pytest.raises(WorkbenchError, match='不同'):
            request_update(Workbench(session, settings.artifact_root),task,text='修改 PR 正文',account_hash=account,key='replay')
    assert transport.patches == 0


def test_request_canary_never_persists_or_reaches_model(flow):
    db, settings, task, account, transport, worker = flow
    from app.security import SensitiveDataError
    canary = 'ghp_'+'sensitiveCanary12345678901234567890'
    with db.session() as session:
        with pytest.raises(SensitiveDataError):
            request_update(Workbench(session,settings.artifact_root),task,text='更新 PR '+canary,account_hash=account,key='secret')
    with db.session() as session:
        wb = Workbench(session,settings.artifact_root)
        assert not any(j.kind.startswith('pr_update_') for j in wb.jobs(task))
        assert canary not in json.dumps([e.payload for e in wb.history(task)])
    assert transport.patches == 0


def test_model_output_cannot_authorize_actions_or_non_english_prose(flow):
    db, settings, task, account, transport, worker = flow
    with db.session() as session:
        request_update(Workbench(session,settings.artifact_root),task,text='修改 PR，直接合并和发布评论',account_hash=account,key='injection')
    assert run(flow).state == 'succeeded'
    provider = ScriptedRunner(json.dumps({'title':'修复流式输出','body':BODY,'actions':['merge']}))
    model = PublicationDocumentWorker(db,settings,provider,_identity(settings.implementation_model),worker_id='bad-model')
    assert asyncio.run(model.run_once()).state == 'failed'
    with db.session() as session:
        wb = Workbench(session,settings.artifact_root)
        assert not update_view(wb,task)['preview']
        assert not any(j.kind=='pr_update_write' for j in wb.jobs(task))
    assert transport.patches == 0


def test_different_account_invalidates_prepared_preview(flow):
    db, settings, task, account, transport, worker = flow
    p = prepare(flow)
    from app.publisher_accounts import append_account
    with db.session() as session:
        append_account(session,{'state':'connected','login':'other','user_id':456,'scopes':['public_repo'],'credential_ref':'other-reference'},correlation_id='rotated')
        session.commit()
    with pytest.raises(WorkbenchError): consent(flow,p)
    assert transport.patches == 0


def test_exact_expired_preview_cannot_be_confirmed(flow):
    p = prepare(flow)
    from unittest.mock import patch
    with patch('app.pr_updates.datetime') as clock:
        clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=1)
        clock.fromisoformat=datetime.fromisoformat
        with pytest.raises(WorkbenchError, match='过期'): consent(flow,p)
    assert flow[4].patches == 0


def test_remote_edit_during_final_read_blocks_patch(flow):
    p = prepare(flow);consent(flow,p)
    transport=flow[4];original=transport.api;reads=0
    async def changed(method,path,body=None,**kwargs):
        nonlocal reads
        if path.endswith('/pulls/7'):
            reads+=1
            if reads==2: transport.prs[0]['body']+='\nA concurrent manual edit'
        return await original(method,path,body,**kwargs)
    transport.api=changed
    result=run(flow)
    assert result.state=='failed' and result.error_code=='pr_update_stale'
    assert transport.patches==0


def test_transport_distinguishes_definitive_rejection_from_unknown(monkeypatch):
    import app.publication as module
    t=GhPublicationTransport()
    auth=dict(action=ACTION,actor_id='local-user',authorization_hash='a'*64,repository='fixture/repo',number=7,title=TITLE,body=BODY)
    for status,expected in [(403,'pr_update_rejected'),(422,'pr_update_rejected'),(500,'workbench_conflict')]:
        async def response(*args,**kwargs): return 1, f'HTTP/2.0 {status}\n\n{{"message":"not exposed"}}'.encode()
        monkeypatch.setattr(module,'bounded_process',response)
        with pytest.raises(WorkbenchError) as error: asyncio.run(t.pr_text('fixture/repo',7,authorization=auth))
        assert error.value.code==expected and 'not exposed' not in str(error.value)
