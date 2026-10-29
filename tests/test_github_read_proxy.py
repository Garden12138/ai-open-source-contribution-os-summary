"""Real envelope/ASGI contracts with a deterministic GitHub HTTP fake."""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.github import GitHubAPIError, GitHubClient
from app.github_read_proxy import MAX_BODY, PublisherReadTransport, ReadRequest
from app.provenance import canonical_json
from app.providers.model_secrets import GatewaySecretStore, management_auth
from app.publisher_accounts import append_account
from app.publisher_service import create_publisher_app
from tests.test_model_settings import envelope

TOKEN = "ghp_SharedPublisherReadCanary1234567890"
READ_KEY = bytes.fromhex("bc" * 32)
MANAGEMENT_KEY = bytes.fromhex("ab" * 32)


def setup(tmp_path, handler):
    settings = Settings(database_url=f"sqlite:///{tmp_path/'business.db'}", artifact_root=str(tmp_path/'artifacts'),
        github_read_proxy_url="http://publisher:8002", github_read_proxy_key=READ_KEY,
        publisher_management_key=MANAGEMENT_KEY, publisher_mode="gh", github_max_retries=0)
    db = Database(settings.database_url); db.create_schema()
    vault = GatewaySecretStore(str(tmp_path/'publisher-private'))
    grant = vault.grant("shared")
    reference = vault.save("shared", envelope(vault, grant, TOKEN))["credential_ref"]
    with db.session() as session:
        append_account(session, {"state":"connected", "credential_ref":reference, "login":"contributor",
            "scopes":["public_repo"]}, correlation_id="connected")
        session.commit()
    app = create_publisher_app(settings, db, vault, run_workers=False, read_transport=httpx.MockTransport(handler))
    return settings, db, vault, app


def request(client, payload, key=READ_KEY):
    body = canonical_json(payload).encode()
    return client.post('/internal/github-read', content=body, headers={'X-GitHub-Read':management_auth(key, body)})


def packet(path, **kwargs):
    return {'version':'github-public-read-v1','path':path,**kwargs}


def test_shared_token_stays_in_publisher_and_worker_reads_public_data(tmp_path, caplog):
    calls=[]; envelopes=[]
    def upstream(r):
        calls.append(r); assert r.headers['authorization']=='Bearer '+TOKEN
        if r.url.path=='/search/issues':
            assert r.url.params['q'].endswith(' is:public')
            return httpx.Response(200,json={'items':[{'number':7}]},headers={'X-RateLimit-Remaining':'4998','set-cookie':'not-forwarded'})
        if r.url.path=='/repos/example/project': return httpx.Response(200,json={'private':False,'default_branch':'main'})
        if r.url.path=='/repos/example/project/commits/main': return httpx.Response(200,json={'sha':'a'*40})
        raise AssertionError(str(r.url))
    settings,db,vault,app=setup(tmp_path,upstream)
    async def scenario():
        class Inspect(httpx.ASGITransport):
            async def handle_async_request(self, r):
                envelopes.append(await r.aread())
                return await super().handle_async_request(r)
        async with GitHubClient(settings,publisher_transport=Inspect(app=app)) as github:
            assert 'authorization' not in github.client.headers
            assert await github.search_issues('repo:example/project is:open',5)==[{'number':7}]
            assert github.rate_limit_remaining==4998
            assert await github.resolve_repository_head('example/project')==('main','a'*40)
    asyncio.run(scenario())
    assert all(r.method=='GET' and r.url.host=='api.github.com' for r in calls)
    assert all(TOKEN.encode() not in b for b in envelopes)
    assert TOKEN not in caplog.text
    with db.session() as session:
        assert TOKEN not in '\n'.join(session.connection().connection.driver_connection.iterdump())
    assert TOKEN.encode() not in vault.path.read_bytes()


@pytest.mark.parametrize('path',[
    '/user','/user/repos','/repos/example/project/forks','/repos/example/project/pulls',
    '/repos/example/project/contents/file','https://api.github.com/repos/example/project',
    '//evil.test/repos/example/project','/repos/example/project/../forks',
    '/repos/example/project/commits/%252e%252e','/repos/example/project/commits/x%3Ftoken=x',
    '/repos/example/project/tarball/main',
])
def test_read_endpoint_allowlist_has_no_generic_escape(tmp_path,path):
    calls=[]
    _,_,_,app=setup(tmp_path,lambda r:calls.append(r))
    with TestClient(app) as client:
        assert request(client,packet(path)).status_code==502
    assert calls==[]


def test_separate_auth_rejects_management_key_write_fields_and_missing_auth(tmp_path):
    calls=[]
    _,_,_,app=setup(tmp_path,lambda r:calls.append(r))
    with TestClient(app) as client:
        assert request(client,packet('/repos/example/project'),MANAGEMENT_KEY).status_code==403
        body=canonical_json({'operation':'grant','connection_id':'forged'}).encode()
        assert client.post('/internal/settings',content=body,headers={'X-Publisher-Management':management_auth(READ_KEY,body)}).status_code==403
        assert client.post('/internal/github-read',json=packet('/repos/example/project')).status_code==403
        assert request(client,packet('/repos/example/project',method='POST')).status_code==502
        assert request(client,packet('/repos/example/project',headers={'Authorization':TOKEN})).status_code==502
    assert calls==[]


@pytest.mark.parametrize('mode',['private','oversized','token_echo','redirect','rotated'])
def test_private_repo_limits_token_leaks_and_changed_account_fail_closed(tmp_path,mode):
    calls=[]; db_ref=[]
    def upstream(r):
        calls.append(r)
        if mode=='private': return httpx.Response(200,json={'private':True})
        if mode=='oversized': return httpx.Response(200,content=b'x'*(MAX_BODY+1))
        if mode=='token_echo': return httpx.Response(200,json={'private':False,'oops':TOKEN})
        if mode=='redirect': return httpx.Response(302,headers={'location':'https://evil.test/steal'})
        with db_ref[0].session() as session:
            append_account(session,{'state':'disconnected'},correlation_id='rotated');session.commit()
        return httpx.Response(200,json={'private':False})
    _,db,_,app=setup(tmp_path,upstream);db_ref.append(db)
    with TestClient(app) as client:
        r=request(client,packet('/repos/example/project'))
        if mode=='redirect':
            assert r.status_code==200 and r.json()['status']==302
        else: assert r.status_code in {409,502}
        assert TOKEN not in r.text
    assert len(calls)==1


def test_api_redirect_never_sends_token_outside_publisher_and_archive_is_unauthenticated(tmp_path):
    def upstream(r):
        if r.url.path=='/repos/example/project':return httpx.Response(200,json={'private':False})
        return httpx.Response(302,headers={'location':'https://codeload.github.com/example/project/legacy.tar.gz/'+'a'*40})
    settings,_,_,app=setup(tmp_path,upstream)
    def download(r):
        assert r.url.host=='codeload.github.com' and 'authorization' not in r.headers
        return httpx.Response(200,content=b'\x1f\x8bpayload')
    async def scenario():
        async with GitHubClient(settings,transport=httpx.MockTransport(download),publisher_transport=httpx.ASGITransport(app=app)) as github:
            dest=tmp_path/'archive.tar.gz'
            assert await github.download_repository_archive('example/project','a'*40,dest)==9
            assert dest.read_bytes()==b'\x1f\x8bpayload'
            with pytest.raises(GitHubAPIError,match='cross-origin'):
                await github._get('/repos/example/project/community/profile')
    asyncio.run(scenario())


def test_proxy_configuration_and_metadata_do_not_grant_publisher_management(tmp_path):
    with pytest.raises(ValueError):Settings(github_read_proxy_url='https://evil.test',github_read_proxy_key=READ_KEY)
    with pytest.raises(ValueError):Settings(github_read_proxy_key=MANAGEMENT_KEY,publisher_management_key=MANAGEMENT_KEY)
    with pytest.raises(ValueError):Settings(github_read_proxy_url='http://publisher:8002')
    settings,_,_,_=setup(tmp_path,lambda r:httpx.Response(200,json={}))
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/v1/meta').json()['token_configured'] is True
        assert not client.get('/api/v1/meta').json()['csrf_token']==READ_KEY.hex()


def test_transport_rejects_write_or_bearer_before_contacting_publisher(tmp_path):
    calls=[]
    settings,_,_,_=setup(tmp_path,lambda r:httpx.Response(200,json={}))
    async def scenario():
        proxy=PublisherReadTransport(settings,transport=httpx.MockTransport(lambda r:calls.append(r)))
        try:
            for r in [httpx.Request('POST','https://api.github.com/repos/example/project'),
                    httpx.Request('GET','https://api.github.com/user'),
                    httpx.Request('GET','https://api.github.com/repos/example/project',headers={'Authorization':'Bearer '+TOKEN})]:
                with pytest.raises((httpx.TransportError,ValueError)):await proxy.handle_async_request(r)
        finally:await proxy.aclose()
    asyncio.run(scenario());assert calls==[]
