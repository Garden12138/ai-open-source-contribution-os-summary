"""Expired draft recovery is an authenticated, idempotent read-only preparation."""
import asyncio
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.api import create_app
from app.models import PublishIntent
from app.workbench import Workbench
from tests.test_conversation_reasoning import approved_preview_fixture


def test_renewal_api_rechecks_exact_text_preserves_acceptance_and_never_authorizes_write(tmp_path):
    db,settings,task,expired,transport,publisher=approved_preview_fixture(tmp_path)
    settings=replace(settings,local_access_token='renewal-test-access',publisher_management_key=b'a'*32)
    route=f'/api/v1/tasks/{task}/workbench'
    payload={'preview_hash':expired.record_hash}
    headers={'X-ContribOS-Token':'renewal-test-access','Idempotency-Key':'renewal'}
    with TestClient(create_app(settings)) as client:
        assert client.post(route+'/publication/renew',json=payload).status_code==401
        assert client.post(route+'/publication/renew',json=payload,headers={**headers,'Origin':'http://testserver'}).status_code==403
        assert client.post(route+'/publication/renew',json={**payload,'title':'Changed'},headers=headers).status_code==422
        assert client.post(route+'/publication/renew',json={'preview_hash':'0'*64},headers=headers).status_code==409
        response=client.post(route+'/publication/renew',json=payload,headers=headers)
        assert response.status_code==202,response.text
        assert response.json()['kind']=='publication_prepare'
        job_id=response.json()['id']
        assert client.post(route+'/publication/renew',json=payload,headers=headers).json()['id']==job_id
        assert asyncio.run(publisher.run_once()).state=='succeeded'
        # Lost acknowledgements may safely replay the same request even after a
        # new preview exists; a fresh request against the old source is stale.
        assert client.post(route+'/publication/renew',json=payload,headers=headers).json()['id']==job_id
        assert client.post(route+'/publication/renew',json=payload,headers={**headers,'Idempotency-Key':'stale'}).status_code==409
        assert client.post(route+'/publication/renew',json={'preview_hash':'0'*64},headers=headers).status_code==409
        assert client.post(route+'/publication/confirm',json={'intent_hash':expired.record_hash,'nonce':expired.payload['nonce']},headers=headers).status_code==409
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        preview=wb.latest(task,'publication_prepared')
        assert preview.record_hash!=expired.record_hash
        assert preview.payload['nonce']!=expired.payload['nonce']
        for field in ['title','body','review_hash','tests_hash','diff_hash','plan_hash','publisher_account_hash']:
            assert preview.payload[field]==expired.payload[field]
        assert wb.latest(task,'execution_accepted')
        assert not wb.latest(task,'publication_document_ready')
        assert not wb.latest(task,'publication_confirmed')
        assert session.scalar(select(PublishIntent)) is None
        assert not any(j.kind=='publication_write' for j in wb.jobs(task))
    assert all(method=='GET' for method,_,_ in transport.calls)


@pytest.mark.parametrize('blocker',['pending','paused','account'])
def test_direct_renewal_rejects_unsettled_conversation_and_changed_account(tmp_path,blocker):
    db,settings,task,expired,_,_=approved_preview_fixture(tmp_path)
    settings=replace(settings,local_access_token='renewal-test-access',publisher_management_key=b'a'*32)
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        if blocker=='pending':
            from app.conversation import receive_message
            receive_message(wb,task,text='先检查正文',key='pending',settings=settings)
        elif blocker=='paused':
            wb.append(task,'conversation_paused',{'version':'conversation-v1'},key='paused')
        else:
            from app.publisher_accounts import append_account
            append_account(session,{'state':'connected','login':'different','user_id':456,'scopes':['public_repo']},correlation_id='changed')
            session.commit()
        before={j.id for j in wb.jobs(task)}
    with TestClient(create_app(settings)) as client:
        response=client.post(f'/api/v1/tasks/{task}/workbench/publication/renew',
            json={'preview_hash':expired.record_hash},
            headers={'X-ContribOS-Token':'renewal-test-access','Idempotency-Key':'blocked'})
        assert response.status_code==409,response.text
    with db.session() as session:
        wb=Workbench(session,settings.artifact_root)
        assert {j.id for j in wb.jobs(task)}==before
        assert wb.latest(task,'publication_prepared').record_hash==expired.record_hash
        assert not wb.latest(task,'publication_confirmed')
