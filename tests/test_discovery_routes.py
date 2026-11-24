import asyncio

import httpx

from contribos.agent.fake import FakeEngine
from test_task_routes import ORIGIN, application
from test_discovery_service import FakeGitHub


def test_scan_routes_progress_filters_marks_and_task(tmp_path):
    async def run():
        app, headers = application(tmp_path, FakeEngine())
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
            service = app.state.supervisor.discovery
            service.github = FakeGitHub()
            assert (await client.post('/api/scans')).status_code == 401
            client.headers.update(headers)
            assert (await client.get('/api/scans/latest')).json() is None
            assert (await client.post('/api/scans', json={'repository': 'repo:evil'})).status_code == 422
            response = await client.post('/api/scans')
            assert response.status_code == 202
            path = '/api/scans/' + response.json()['id']
            assert response.json()['status'] == 'running'
            await service.job
            assert (await client.get(path)).json()['status'] == 'completed'
            assert 'completed' in (await client.get(path + '/stream')).text
            assert (await client.get('/api/scans/missing')).status_code == 404
            url = '/api/opportunities'
            async def items(**params):
                return (await client.get(url, params=params)).json()['opportunities']
            assert len(await items(view='daily')) == 3
            assert len(await items(lang='python', label='Good First Issue', bounty=False, small=True)) == 3
            assert await items(bounty=True) == await items(small=True, lang='Rust') == []
            assert [r['id'] for r in await items(q='small', sort='updated')] == ['2']
            assert (await client.get(url, params={'limit': 1})).json()['more']
            path = url + '/2/mark'
            for mark in ('starred', 'ignored', None):
                response = await client.put(path, json={'mark': mark})
                assert response.status_code == 200 and response.json()['mark'] == mark
                if mark:
                    assert [r['id'] for r in await items(view=mark)] == ['2']
                assert len(await items(view='daily')) == (2 if mark == 'ignored' else 3)
            assert (await client.get(url + '/missing')).status_code == 404
            assert (await client.get(url + '/2')).json()['body']
            created = await client.post('/api/tasks', json={'opportunity_id': '2', 'isolation': 'host',
                                        'trust_confirmed': True})
            assert created.status_code == 201
            task = (await client.get('/api/tasks/' + created.json()['task_id'])).json()
            assert (task['repo_full_name'], task['issue_number']) == ('acme/sample', 2)
    asyncio.run(run())


def test_scan_redacts_host_secrets_in_rows_and_http(tmp_path, monkeypatch):
    import secrets
    value = secrets.token_urlsafe(32)
    monkeypatch.setenv('DISCOVERY_TEST_SECRET', value)

    async def run():
        app, headers = application(tmp_path, FakeEngine())
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
            service = app.state.supervisor.discovery
            service.github = FakeGitHub()
            service.github.title = value
            await client.post('/api/scans')
            await service.job
            assert (await client.get('/api/opportunities/2')).json()['title'] == '[REDACTED]'
            assert value not in (await client.get('/api/opportunities')).text
            assert value not in '\n'.join(service.db.iterdump())
    asyncio.run(run())
