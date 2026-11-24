import asyncio
import json
import secrets
import subprocess

import httpx

from contribos.agent.engine import EngineEvent
from contribos.agent.fake import FakeEngine
from contribos.agent.profiles import AgentStatus, PROFILES
from contribos.config import Settings
from contribos.github import GitHubClient
from contribos.sandbox.base import ExecResult
from contribos.tasks.supervisor import TaskSupervisor
from contribos.web.app import create_app
from test_task_supervisor import FakeSandbox, issue_loader, until

ORIGIN = 'http://127.0.0.1:8765'
ISSUE = 'https://github.com/owner/repo/issues/1'


def test_github_snapshot_never_persists_or_returns_host_token(tmp_path, monkeypatch, caplog):
    canary = secrets.token_urlsafe(32)
    settings = Settings(home=tmp_path)
    monkeypatch.delenv('CONTRIBOS_GITHUB_TOKEN', raising=False)
    monkeypatch.setattr(Settings, 'tool', lambda _, name: '/tools/' + name)

    def gh(argv, **kwargs):
        assert argv == ['/tools/gh', 'auth', 'token', '--hostname', 'github.com']
        assert all(key not in kwargs['env'] for key in ('GITHUB_TOKEN', 'GH_TOKEN', 'CONTRIBOS_GITHUB_TOKEN'))
        return subprocess.CompletedProcess(argv, 0, canary + '\n')

    monkeypatch.setattr('contribos.github.subprocess.run', gh)

    def read(request):
        assert request.url.host == 'api.github.com' and request.headers['Authorization'] == 'Bearer ' + canary
        assert canary not in str(request.url)
        if request.url.path.endswith('/issues/1'):
            return httpx.Response(200, json={'title': 'Issue ' + canary, 'body': 'Body ' + canary})
        if request.url.path.endswith('/repos/owner/repo'):
            return httpx.Response(200, json={'default_branch': 'main'})
        return httpx.Response(200, json=[])

    def factory(settings, store):
        engine = FakeEngine()
        return TaskSupervisor(settings, store, github=GitHubClient(settings, transport=httpx.MockTransport(read)),
            sandbox_factories={'host': FakeSandbox}, engine_factory=lambda *_: engine)

    async def check():
        app = create_app(settings, supervisor_factory=factory)
        headers = {'Authorization': 'Bearer ' + settings.local_token_path.read_text().strip()}
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
            response = await client.post('/api/tasks', json={'issue_url': ISSUE, 'isolation': 'host', 'trust_confirmed': True})
            assert response.status_code == 201
            task_id = response.json()['task_id']
            store = app.state.supervisor.store
            await until(lambda: store.get(task_id)['agent_state'] == 'idle' and store.get(task_id)['status'] == 'ready')
            task = await client.get(f'/api/tasks/{task_id}')
            events = await client.get(f'/api/tasks/{task_id}/events')
            assert task.json()['issue_snapshot']['body'] == 'Body [REDACTED]'
            assert canary not in task.text + events.text + '\n'.join(store.db.iterdump()) + caplog.text

    asyncio.run(check())


def application(tmp_path, engine):
    class ViewableSandbox(FakeSandbox):
        async def exec(self, argv, **kwargs):
            assert argv[-1] == 'README.md'
            return ExecResult(0, 'Example content')

    settings = Settings(home=tmp_path)

    def factory(settings, store):
        return TaskSupervisor(settings, store, issue_loader=issue_loader,
            sandbox_factories={'host': ViewableSandbox}, engine_factory=lambda *_: engine)
    app = create_app(settings, supervisor_factory=factory)
    token = settings.local_token_path.read_text().strip()
    return app, {'Authorization': 'Bearer ' + token}


def test_task_routes_identity_creation_and_actions(tmp_path, monkeypatch):
    status = AgentStatus('codex', 'ok', '/path/to/codex', '1.0', 'logged_in', '', '', '')
    monkeypatch.setattr('contribos.web.routes_tasks.agent_statuses', lambda _: [status])
    monkeypatch.setattr('contribos.web.routes_tasks.check_profile', lambda *_: status)

    async def check():
        engine = FakeEngine()
        app, headers = application(tmp_path, engine)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN) as unauthenticated:
            assert (await unauthenticated.get('/api/agents')).status_code == 401
            assert (await unauthenticated.get('/api/tasks')).status_code == 401
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
                supervisor = app.state.supervisor
                profile = (await client.get('/api/agents')).json()['agents'][0]
                assert profile['installed'] and profile['login'] == 'logged_in'
                assert profile['name'] == PROFILES['codex'].name and profile['available_isolation'] == ['host']
                assert (await client.post('/api/agents/codex/check')).json()['version'] == '1.0'
                assert (await client.post('/api/agents/missing/check')).status_code == 404
                new = {'issue_url': ISSUE, 'isolation': 'host'}
                denied = await client.post('/api/tasks', json=new)
                assert denied.status_code == 403 and denied.json()['error']['code'] == 'host_trust_required'
                assert (await client.post('/api/tasks', json={**new, 'trust_confirmed': False})).status_code == 403
                assert (await client.post('/api/tasks', json={**new, 'trust_confirmed': 'true'})).status_code == 422
                assert (await client.post('/api/tasks', json={'issue_url': ISSUE})).json()['error']['code'] == 'sandbox_unavailable'
                assert (await client.post('/api/tasks', json={'issue_url': 'https://untrusted.test'})).status_code == 422
                assert (await client.post('/api/tasks', json={'issue_url': ISSUE, 'opportunity_id': 'x'})).status_code == 422
                assert (await client.get('/api/tasks')).json() == {'tasks': []}
                assert (await client.post('/api/tasks', json={**new, 'trust_owner': True})).status_code == 403
                assert (await client.get('/api/settings')).json()['trusted_owners'] == []
                response = await client.post('/api/tasks', json={**new, 'trust_confirmed': True, 'trust_owner': True})
                assert response.status_code == 201
                assert (await client.get('/api/settings')).json()['trusted_owners'] == ['owner']
                tid = response.json()['task_id']
                base = f'/api/tasks/{tid}'
                await until(lambda: supervisor.store.get(tid)['agent_state'] == 'idle')
                task = (await client.get(base)).json()
                assert task['issue_snapshot']['title'] == 'Test Issue'
                assert task['isolation'] == 'host'
                assert (await client.get('/api/tasks')).json()['tasks'][0]['id'] == tid
                async def refreshed_issue(_):
                    return {'title': 'Updated issue', 'body': 'Updated body'}

                supervisor.issue_loader = refreshed_issue
                assert (await client.post(base + '/issue/refresh')).status_code == 202
                await until(lambda: supervisor.store.get(tid)['title'] == 'Updated issue')
                assert (await client.get(base)).json()['issue_snapshot']['body'] == 'Updated body'
                msg = {'text': 'Implement', 'client_message_id': 'idempotent'}
                accepted = await client.post(base + '/messages', json=msg)
                repeat = await client.post(base + '/messages', json={**msg, 'delivery': 'interrupt'})
                assert accepted.status_code == 202 and repeat.json() == accepted.json()
                await until(lambda: supervisor.store.get(tid)['agent_state'] == 'idle')
                assert (await client.get(base + '/events?limit=1')).json()['events'][0]['type'] == 'task.preparing'
                assert (await client.get(base + '/stream', headers={'Last-Event-ID': '-5'})).status_code == 422
                assert (await client.get(base + '/events?after=9223372036854775808')).status_code == 422
                assert (await client.get(base + '/files', params={'path': 'README.md'})).json()['text'] == 'Example content'
                for path in ('../README.md', '.git/config', '/etc/passwd'):
                    assert (await client.get(base + '/files', params={'path': path})).status_code == 422
                assert (await client.get(base + '/diff')).json()['files'] == []
                engine.hold_turn = True
                running = await client.post(base + '/continue')
                assert running.status_code == 202
                await until(lambda: engine.running)
                assert (await client.post(base + '/interrupt')).json()['interrupted']
                await until(lambda: supervisor.store.get(tid)['agent_state'] == 'idle')
                turns = (await client.get(base)).json()['turns']
                assert len(turns) == 3 and turns[-1]['status'] == 'interrupted'
                assert (await client.delete(base)).status_code == 202
                await until(lambda: not supervisor.store.list_tasks())
                assert (await client.get(base)).status_code == 404
    asyncio.run(check())


def test_trusted_owner_and_opportunity_can_create_host_task(tmp_path):
    async def check():
        app, headers = application(tmp_path, FakeEngine())
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
            db = app.state.db
            db.execute("INSERT INTO settings VALUES ('trusted_owners', '[\"OWNER\"]', '2026')")
            db.execute("INSERT INTO repositories VALUES ('owner/repo', '{}', '2026')")
            db.execute("""INSERT INTO opportunities(id,repo_full_name,issue_number,title,url,state,first_seen_at,last_seen_at)
                VALUES ('opp','owner/repo',1,'Issue',?,'open','2026','2026')""", (ISSUE,))
            response = await client.post('/api/tasks', json={'opportunity_id': 'opp', 'isolation': 'host'})
            assert response.status_code == 201
            tid = response.json()['task_id']
            await until(lambda: app.state.supervisor.store.get(tid)['agent_state'] == 'idle')
            assert (await client.get(f'/api/tasks/{tid}')).json()['opportunity_id'] == 'opp'
    asyncio.run(check())


def test_sse_reconnect_redaction_and_request_responses(tmp_path, monkeypatch):
    canary = 'canary-' + 'meaningful-test-output-297'
    monkeypatch.setenv('M3_CANARY_TOKEN', canary)

    async def check():
        engine = FakeEngine(scripts=[[
            EngineEvent('command.completed', {'output_tail': 'test ' + canary}),
            EngineEvent('message.delta', {'text': 'Streaming\n'}),
            EngineEvent('permission.requested', {'request_id': 'permission-1', 'options': [{'optionId': 'allow'}]}),
            EngineEvent('question.asked', {'request_id': 'question-1', 'message': 'Which test?',
                'schema': {'required': ['scope'], 'properties': {'scope': {'type': 'string', 'enum': ['unit']}}}}),
        ]])
        app, headers = application(tmp_path, engine)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
            result = await client.post('/api/tasks', json={'issue_url': ISSUE, 'isolation': 'host', 'trust_confirmed': True})
            tid = result.json()['task_id']
            base = f'/api/tasks/{tid}'
            runtime = app.state.supervisor
            await until(lambda: 'permission-1' in runtime.events.pending(tid))
            history = (await client.get(base + '/events')).json()['events']
            before = next(event['seq'] for event in history if event['type'] == 'turn.started')
            request = {'type': 'http.request', 'body': b'', 'more_body': False}
            disconnect = asyncio.Event()
            replayed = asyncio.Event()
            live = asyncio.Event()
            frames = []
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'}, 'method': 'GET',
                'scheme': 'http', 'path': base + '/stream', 'query_string': b'',
                'headers': [(b'host', b'127.0.0.1:8765'), (b'authorization', headers['Authorization'].encode()),
                            (b'last-event-id', str(before).encode())],
                'server': ('127.0.0.1', 8765), 'client': ('127.0.0.1', 50000), 'root_path': ''}

            async def receive():
                nonlocal request
                if request is not None:
                    first, request = request, None
                    return first
                await disconnect.wait()
                return {'type': 'http.disconnect'}

            async def send(message):
                if message['type'] == 'http.response.start':
                    assert message['status'] == 200
                if chunk := message.get('body'):
                    frames.append(chunk.decode())
                    if 'event: stream.snapshot' in frames[-1]:
                        replayed.set()
                    if 'event: reasoning.delta' in frames[-1]:
                        live.set()

            stream = asyncio.create_task(app(scope, receive, send))
            try:
                await asyncio.wait_for(replayed.wait(), 3)
                assert not any('event: turn.started' in chunk for chunk in frames)
                assert any(chunk.startswith('id: ') and 'event: command.completed' in chunk for chunk in frames)
                assert canary not in ''.join(frames)
                assert '[REDACTED]' in ''.join(frames)
                engine.queue.put_nowait(EngineEvent('reasoning.delta', {'text': 'live thought\n'}))
                await asyncio.wait_for(live.wait(), 3)
                assert frames[-1].startswith('event: reasoning.delta\n')
            finally:
                disconnect.set()
                await asyncio.wait_for(stream, 3)
            assert runtime.store.get(tid)['agent_state'] == 'running'
            url = base + '/requests/{}/respond'
            assert (await client.post(url.format('permission-1'), json={'option_id': 'bad'})).status_code == 422
            assert (await client.post(url.format('permission-1'), json={'option_id': 'allow'})).status_code == 200
            await until(lambda: 'question-1' in runtime.events.pending(tid))
            assert (await client.post(url.format('question-1'), json={'answers': {'scope': 'wrong'}})).status_code == 422
            assert (await client.post(url.format('question-1'), json={'answers': {'scope': 'unit'}})).status_code == 200
            await until(lambda: runtime.store.get(tid)['agent_state'] == 'idle')
            assert (await client.post(url.format('permission-1'), json={'option_id': 'allow'})).status_code == 409
            assert canary not in (await client.get(base + '/events')).text
            assert canary not in (await client.get(base)).text
            assert canary not in ''.join(row[0] for row in app.state.db.execute('SELECT payload_json FROM task_events'))
    asyncio.run(check())


def test_remembered_permission_is_exact_and_task_scoped(tmp_path):
    def permission(request_id, command):
        return EngineEvent('permission.requested', {
            'request_id': request_id,
            'tool_call': {'toolCallId': request_id, 'kind': 'execute', 'rawInput': {'command': command}},
            'options': [
                {'optionId': 'once', 'kind': 'allow_once'},
                {'optionId': 'always', 'kind': 'allow_always'},
            ],
        })

    async def check():
        engine = FakeEngine(scripts=[
            [permission('first', 'pytest'), permission('same', 'pytest'), permission('different', 'pytest -q')],
        ])
        app, headers = application(tmp_path, engine)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
            supervisor = app.state.supervisor
            body = {'issue_url': ISSUE, 'isolation': 'host', 'trust_confirmed': True}
            tid = (await client.post('/api/tasks', json=body)).json()['task_id']
            await until(lambda: 'first' in supervisor.events.pending(tid))
            assert (await client.post(f'/api/tasks/{tid}/requests/first/respond', json={'option_id': 'always'})).is_success
            await until(lambda: 'different' in supervisor.events.pending(tid))
            history = supervisor.events.history(tid)
            assert any(event.type == 'permission.resolved' and event.payload.get('auto') for event in history)
            assert not any(event.payload.get('option_id') == 'always' for event in history)
            assert (await client.post(f'/api/tasks/{tid}/requests/different/respond', json={'option_id': 'once'})).is_success
            await until(lambda: supervisor.store.get(tid)['agent_state'] == 'idle')
            supervisor.engine_factory = lambda *_: FakeEngine(scripts=[[permission('other', 'pytest')]])
            second = (await client.post('/api/tasks', json=body)).json()['task_id']
            await until(lambda: 'other' in supervisor.events.pending(second))
        app, headers = application(tmp_path, FakeEngine(scripts=[[permission('restart', 'pytest')]]))
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url=ORIGIN, headers=headers) as client:
            assert (await client.post(f'/api/tasks/{tid}/messages', json={
                'text': 'Run this again', 'client_message_id': 'restart-message',
            })).status_code == 202
            supervisor = app.state.supervisor
            await until(lambda: supervisor.store.get(tid)['agent_state'] == 'idle')
            assert sum(event.type == 'permission.resolved' and event.payload.get('auto') is True
                       for event in supervisor.events.history(tid)) == 2
    asyncio.run(check())
